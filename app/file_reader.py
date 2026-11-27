"""Read a Sellerboard export (xlsx or csv) into typed rows.

The exact layout of Sellerboard's export is not hard-coded.  For each sheet the
header row is detected (exports often start with a title line), headers become
safe MySQL column names, and values are cleaned:

    "$1,234.56" -> 1234.56     "12.5%" -> 12.5     "(45.10)" -> -45.10
    "" / "-" / "N/A" -> NULL   dates stay dates     everything else stays text
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .exceptions import DataError
from .logging_config import get_logger

logger = get_logger("reader")

#: Columns the loader adds itself; a header with the same name is prefixed.
RESERVED_COLUMNS = frozenset(
    {"id", "marketplace", "granularity", "period_start", "period_end", "sheet_name",
     "row_num", "source_file", "loaded_at"}
)
MAX_COLUMN_NAME = 60
_HEADER_SCAN_ROWS = 25
_NULL_TOKENS = {"", "-", "--", "—", "n/a", "na", "null", "none", "nan"}
_NUMBER = re.compile(r"^\(?\s*-?\s*[$€£₹]?\s*-?\s*(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?\s*%?\s*\)?$")


@dataclass
class Sheet:
    name: str
    columns: list[str]                      # safe column names, in file order
    headers: dict[str, str]                 # safe name -> original header
    rows: list[dict[str, Any]] = field(default_factory=list)


def column_name(header: str) -> str:
    """``"Gross profit, $"`` -> ``gross_profit``."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", str(header).strip().lower()).strip("_")
    name = name or "column"
    if name[0].isdigit():
        name = f"c_{name}"
    if name in RESERVED_COLUMNS:
        name = f"x_{name}"
    return name[:MAX_COLUMN_NAME].rstrip("_")


def clean_value(value: Any) -> Any:
    """Normalize one cell to None / Decimal / datetime / date / str."""
    if value is None:
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, (datetime, date)):
        return value
    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None
    if _NUMBER.match(text) and any(ch.isdigit() for ch in text):
        negative = text.startswith("(") and text.endswith(")") or "-" in text
        digits = re.sub(r"[^0-9.]", "", text)
        try:
            number = Decimal(digits)
        except InvalidOperation:
            return text
        return -number if negative else number
    return text


def read_export(path: Path) -> list[Sheet]:
    """Every non-empty sheet of an export.

    Raises:
        DataError: The file cannot be parsed or holds no data at all.
    """
    try:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            raw = _read_xlsx(path)
        elif path.suffix.lower() == ".csv":
            raw = [("data", _read_csv(path))]
        else:
            raise DataError(f"Unsupported export type {path.suffix!r}: {path}")
    except DataError:
        raise
    except Exception as exc:
        raise DataError(f"Could not read {path}: {exc}") from exc

    sheets = [s for s in (_to_sheet(name, rows) for name, rows in raw) if s is not None]
    if not sheets:
        raise DataError(f"{path} contains no tabular data")
    for s in sheets:
        logger.debug("Read sheet %r from %s: %d row(s), %d column(s)", s.name, path.name, len(s.rows), len(s.columns))
    return sheets


def _read_xlsx(path: Path) -> list[tuple[str, list[tuple]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return [(ws.title, list(ws.iter_rows(values_only=True))) for ws in wb.worksheets]
    finally:
        wb.close()


def _read_csv(path: Path) -> list[tuple]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [tuple(r) for r in csv.reader(io.StringIO(text), dialect)]


def _filled(row: tuple) -> int:
    return sum(1 for v in row if v is not None and str(v).strip() != "")


def _to_sheet(name: str, rows: list[tuple]) -> Sheet | None:
    rows = [r for r in rows if r and _filled(r) > 0]
    if not rows:
        return None

    # Header = the first of the widest rows near the top (skips title lines).
    window = rows[:_HEADER_SCAN_ROWS]
    widest = max(_filled(r) for r in window)
    if widest < 2:
        return None
    header_index = next(i for i, r in enumerate(window) if _filled(r) == widest)
    header_row = rows[header_index]

    positions: list[int] = []
    columns: list[str] = []
    headers: dict[str, str] = {}
    for pos, raw in enumerate(header_row):
        if raw is None or str(raw).strip() == "":
            continue
        base = column_name(str(raw))
        candidate, n = base, 2
        while candidate in headers:
            candidate = f"{base[:MAX_COLUMN_NAME - 4]}_{n}"
            n += 1
        positions.append(pos)
        columns.append(candidate)
        headers[candidate] = str(raw).strip()

    sheet = Sheet(name=str(name)[:64], columns=columns, headers=headers)
    for raw_row in rows[header_index + 1:]:
        record = {
            col: clean_value(raw_row[pos] if pos < len(raw_row) else None)
            for col, pos in zip(columns, positions)
        }
        if any(v is not None for v in record.values()):
            sheet.rows.append(record)
    return sheet
