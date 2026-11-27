from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from app.database import Repository
from app.exceptions import DatabaseError
from app.file_reader import Sheet
from app.periods import Period

DAY = Period(date(2026, 11, 24), date(2026, 11, 24), "day")


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    r = Repository(engine, "sellerboard_walmart_daily")
    r.ensure_table()
    yield r
    r.dispose()


def _sheet(rows, name="Dashboard"):
    cols = list(rows[0].keys())
    return Sheet(name=name, columns=cols, headers={c: c for c in cols}, rows=rows)


def _rows(repo):
    t = repo.table
    with repo.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(t).order_by(t.c.period_start, t.c.row_num))]


def test_creates_columns_and_inserts(repo):
    result = repo.replace_period(DAY, "walmart", [_sheet([
        {"product": "Widget", "sales": Decimal("10.5"), "units": Decimal("2")},
        {"product": "Gadget", "sales": Decimal("3"), "units": None},
    ])], "f.xlsx")
    assert (result.inserted, result.deleted) == (2, 0)
    assert set(result.added_columns) == {"product", "sales", "units"}
    rows = _rows(repo)
    assert rows[0]["product"] == "Widget" and rows[0]["sales"] == Decimal("10.5")
    assert rows[0]["period_start"] == date(2026, 11, 24) and rows[0]["marketplace"] == "walmart"


def test_reload_replaces_only_that_period(repo):
    other = Period(date(2026, 11, 23), date(2026, 11, 23), "day")
    repo.replace_period(other, "walmart", [_sheet([{"sales": Decimal("1")}])], "a.xlsx")
    repo.replace_period(DAY, "walmart", [_sheet([{"sales": Decimal("1")}, {"sales": Decimal("2")}])], "b.xlsx")
    result = repo.replace_period(DAY, "walmart", [_sheet([{"sales": Decimal("5")}])], "c.xlsx")
    assert (result.deleted, result.inserted) == (2, 1)
    rows = _rows(repo)
    assert [(r["period_start"], r["sales"]) for r in rows] == [
        (date(2026, 11, 23), Decimal("1")), (date(2026, 11, 24), Decimal("5"))]


def test_new_header_adds_column_and_text_widens(repo):
    repo.replace_period(DAY, "walmart", [_sheet([{"sales": Decimal("1")}])], "a.xlsx")
    result = repo.replace_period(DAY, "walmart", [_sheet([
        {"sales": "n/a yet", "refunds": Decimal("2"), "when": datetime(2026, 11, 24, 8)}])], "b.xlsx")
    assert set(result.added_columns) == {"refunds", "when"}
    [row] = _rows(repo)
    assert row["sales"] == "n/a yet" and row["refunds"] == Decimal("2")


def test_failed_insert_rolls_back_the_delete(repo):
    repo.replace_period(DAY, "walmart", [_sheet([{"sales": Decimal("1")}])], "a.xlsx")
    with repo.engine.begin() as c:  # a trigger that makes every insert fail
        c.execute(text("CREATE TRIGGER boom BEFORE INSERT ON sellerboard_walmart_daily "
                       "BEGIN SELECT RAISE(ABORT, 'boom'); END"))
    with pytest.raises(DatabaseError):
        repo.replace_period(DAY, "walmart", [_sheet([{"sales": Decimal("9")}])], "b.xlsx")
    [row] = _rows(repo)
    assert row["sales"] == Decimal("1")
