from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.exceptions import DataError
from app.file_reader import clean_value, column_name, read_export


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.56", Decimal("1234.56")),
    ("12.5%", Decimal("12.5")),
    ("(45.10)", Decimal("-45.10")),
    ("-$3.00", Decimal("-3.00")),
    ("1,204", Decimal("1204")),
    (7, Decimal("7")),
    (2.5, Decimal("2.5")),
    ("", None),
    ("-", None),
    ("N/A", None),
    ("SP | 50% off", "SP | 50% off"),
    ("Walmart US", "Walmart US"),
])
def test_clean_value(raw, expected):
    assert clean_value(raw) == expected


@pytest.mark.parametrize("header,expected", [
    ("Gross profit, $", "gross_profit"),
    ("Net margin %", "net_margin"),
    ("Units", "units"),
    ("2025 Sales", "c_2025_sales"),
    ("id", "x_id"),
    ("Period start", "x_period_start"),
])
def test_column_name(header, expected):
    assert column_name(header) == expected


def test_xlsx_with_title_row(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Dashboard"
    ws.append(["Sellerboard dashboard 24.11.2026"])
    ws.append([])
    ws.append(["Product", "SKU", "Sales", "Units", "Margin", "Date"])
    ws.append(["Widget", "W-1", "$1,000.50", 10, "25%", datetime(2026, 11, 24)])
    ws.append(["Gadget", "G-2", 99.5, 1, "-", datetime(2026, 11, 24)])
    ws.append([None, None, None])
    path = tmp_path / "x.xlsx"
    wb.save(path)

    [sheet] = read_export(path)
    assert sheet.name == "Dashboard"
    assert sheet.columns == ["product", "sku", "sales", "units", "margin", "date"]
    assert sheet.headers["sales"] == "Sales"
    assert len(sheet.rows) == 2
    assert sheet.rows[0]["sales"] == Decimal("1000.50")
    assert sheet.rows[0]["margin"] == Decimal("25")
    assert sheet.rows[1]["margin"] is None
    assert sheet.rows[0]["date"] == datetime(2026, 11, 24)


def test_csv_and_duplicate_headers(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("Metric;Value;Value\nSales;$10;1\nOrders;3;2\n", encoding="utf-8")
    [sheet] = read_export(path)
    assert sheet.columns == ["metric", "value", "value_2"]
    assert sheet.rows[0] == {"metric": "Sales", "value": Decimal("10"), "value_2": Decimal("1")}


def test_unreadable_files(tmp_path):
    bad = tmp_path / "x.xlsx"
    bad.write_bytes(b"PK\x03\x04 not really a workbook")
    with pytest.raises(DataError):
        read_export(bad)
    empty = tmp_path / "e.csv"
    empty.write_text("only-one-column\nvalue\n")
    with pytest.raises(DataError):
        read_export(empty)
