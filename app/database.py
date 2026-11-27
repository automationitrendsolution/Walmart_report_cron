"""MySQL storage for the exports.

The tool owns its table (default ``sellerboard_walmart_daily``):

* created on first use, with fixed key columns plus one column per export
  header (see :mod:`app.file_reader`);
* a header that appears later is added with ``ALTER TABLE ... ADD COLUMN``;
  a numeric/date column that later receives text is widened to ``TEXT``;
* each period is written as ``DELETE`` (that period) + ``INSERT`` in **one
  transaction**, so a re-download replaces the period and a failed write
  rolls back, leaving the previous rows intact.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    delete,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from .config import DatabaseConfig
from .exceptions import DatabaseError
from .file_reader import Sheet
from .logging_config import get_logger
from .periods import Period
from .timeutils import now_utc

logger = get_logger("database")

NUMBER = "number"
DATETIME = "datetime"
TEXT = "text"


@dataclass(frozen=True)
class LoadResult:
    table: str
    deleted: int
    inserted: int
    added_columns: tuple[str, ...]


def value_kind(values: list[Any]) -> str | None:
    """The column kind a list of cleaned values needs; None if all NULL."""
    kinds = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, Decimal):
            kinds.add(NUMBER)
        elif isinstance(v, (datetime, date)):
            kinds.add(DATETIME)
        else:
            return TEXT
    if not kinds:
        return None
    return kinds.pop() if len(kinds) == 1 else TEXT


def _sql_type(kind: str | None):
    if kind == NUMBER:
        return Numeric(20, 4)
    if kind == DATETIME:
        return DateTime()
    return Text()


def _column_kind(column: Column) -> str:
    if isinstance(column.type, Numeric):
        return NUMBER
    if isinstance(column.type, (DateTime, Date)):
        return DATETIME
    return TEXT


def _base_columns() -> list[Column]:
    return [
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
        Column("marketplace", String(32), nullable=False),
        Column("granularity", String(8), nullable=False),
        Column("period_start", Date, nullable=False),
        Column("period_end", Date, nullable=False),
        Column("sheet_name", String(64), nullable=False),
        Column("row_num", Integer, nullable=False),
        Column("source_file", String(255)),
        Column("loaded_at", DateTime, nullable=False),
    ]


class Repository:
    def __init__(self, engine: Engine, table_name: str) -> None:
        self.engine = engine
        self.table_name = table_name
        self.table: Table | None = None

    # -- schema --------------------------------------------------------------

    def ensure_table(self) -> Table:
        """Create the table if absent, then reflect it."""
        try:
            if not inspect(self.engine).has_table(self.table_name):
                metadata = MetaData()
                table = Table(self.table_name, metadata, *_base_columns(),
                              mysql_charset="utf8mb4", mysql_engine="InnoDB")
                Index(f"ix_{self.table_name}_period"[:64], table.c.marketplace,
                      table.c.granularity, table.c.period_start)
                metadata.create_all(self.engine)
                logger.info("Created table %s", self.table_name)
            self._reflect()
        except SQLAlchemyError as exc:
            raise _wrap(exc, f"Could not prepare table {self.table_name}") from exc
        assert self.table is not None
        return self.table

    def _reflect(self) -> None:
        metadata = MetaData()
        self.table = Table(self.table_name, metadata, autoload_with=self.engine)

    def _sync_columns(self, sheets: list[Sheet]) -> tuple[str, ...]:
        """Add missing columns and widen ones whose values no longer fit."""
        assert self.table is not None
        needed: dict[str, str | None] = {}
        for sheet in sheets:
            for col in sheet.columns:
                kind = value_kind([r.get(col) for r in sheet.rows])
                previous = needed.get(col, None)
                if col not in needed:
                    needed[col] = kind
                elif kind is not None and previous is not None and kind != previous:
                    needed[col] = TEXT
                elif previous is None:
                    needed[col] = kind

        added: list[str] = []
        statements: list[str] = []
        quote = self.engine.dialect.identifier_preparer.quote
        type_sql = lambda kind: _sql_type(kind).compile(dialect=self.engine.dialect)  # noqa: E731
        is_mysql = self.engine.dialect.name == "mysql"

        for col, kind in needed.items():
            if col not in self.table.c:
                statements.append(
                    f"ALTER TABLE {quote(self.table_name)} ADD COLUMN {quote(col)} {type_sql(kind)} NULL"
                )
                added.append(col)
            elif kind is not None and _column_kind(self.table.c[col]) != TEXT and kind != _column_kind(self.table.c[col]):
                logger.warning("Column %s now holds %s values; widening it to TEXT", col, kind)
                if is_mysql:
                    statements.append(
                        f"ALTER TABLE {quote(self.table_name)} MODIFY COLUMN {quote(col)} TEXT NULL"
                    )
                else:  # SQLite (tests) is dynamically typed; only the bind type changes
                    self.table.c[col].type = Text()

        if statements:
            # DDL commits implicitly in MySQL, so it runs before the data transaction.
            with self.engine.begin() as conn:
                for sql in statements:
                    conn.execute(text(sql))
            if added:
                logger.info("Added column(s) to %s: %s", self.table_name, ", ".join(added))
            if is_mysql or added:
                widened = {c: self.table.c[c].type for c in self.table.c.keys()
                           if isinstance(self.table.c[c].type, Text)}
                self._reflect()
                for c, t in widened.items():
                    if c in self.table.c:
                        self.table.c[c].type = t
        return tuple(added)

    # -- data ----------------------------------------------------------------

    def replace_period(
        self, period: Period, marketplace: str, sheets: list[Sheet], source_file: str
    ) -> LoadResult:
        """Replace ``period``'s rows with ``sheets`` in one transaction."""
        if self.table is None:
            self.ensure_table()
        try:
            added = self._sync_columns(sheets)
            assert self.table is not None
            loaded_at = now_utc().replace(tzinfo=None, microsecond=0)
            records: list[dict[str, Any]] = []
            for sheet in sheets:
                for index, row in enumerate(sheet.rows, start=1):
                    record = {
                        "marketplace": marketplace,
                        "granularity": period.granularity,
                        "period_start": period.start,
                        "period_end": period.end,
                        "sheet_name": sheet.name,
                        "row_num": index,
                        "source_file": source_file[-255:],
                        "loaded_at": loaded_at,
                    }
                    for col, value in row.items():
                        if isinstance(self.table.c[col].type, Text) and value is not None and not isinstance(value, str):
                            value = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
                        record[col] = value
                    records.append(record)

            t = self.table
            with self.engine.begin() as conn:
                deleted = conn.execute(
                    delete(t).where(
                        t.c.marketplace == marketplace,
                        t.c.granularity == period.granularity,
                        t.c.period_start == period.start,
                    )
                ).rowcount or 0
                if records:
                    conn.execute(t.insert(), records)
        except SQLAlchemyError as exc:
            raise _wrap(exc, f"Loading {period.label} into {self.table_name} failed (rolled back)") from exc

        logger.debug("%s %s: %d deleted, %d inserted", self.table_name, period.label, deleted, len(records))
        return LoadResult(self.table_name, deleted, len(records), added)

    def dispose(self) -> None:
        self.engine.dispose()


def _wrap(exc: SQLAlchemyError, message: str) -> DatabaseError:
    transient = isinstance(exc, OperationalError) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    )
    return DatabaseError(f"{message}: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                         transient=transient)


def build_repository(config: DatabaseConfig) -> Repository:
    """Connect to MySQL and make sure the table exists."""
    engine = create_engine(
        config.url,
        pool_pre_ping=True,
        pool_recycle=config.pool_recycle_seconds,
        connect_args=config.connect_args(),
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        engine.dispose()
        raise DatabaseError(
            f"Cannot connect to {config.safe_url}: {str(exc).splitlines()[0][:300]}",
            transient=True,
        ) from exc
    logger.debug("Connected to %s", config.safe_url)
    repo = Repository(engine, config.table)
    repo.ensure_table()
    return repo


RepositoryFactory = Callable[[], Repository]
