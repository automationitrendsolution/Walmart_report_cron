from datetime import date

import pytest

from app.downloader import target_path, validate_download
from app.exceptions import FileValidationError
from app.periods import Period


def test_xlsx_and_csv_accepted(tmp_path):
    x = tmp_path / "a.xlsx"
    x.write_bytes(b"PK\x03\x04" + b"\0" * 100)
    assert validate_download(x) == 104
    c = tmp_path / "a.csv"
    c.write_text("Date,Sales,Units\n2026-11-25,10,1\n")
    assert validate_download(c) > 0


@pytest.mark.parametrize("content", [
    b"<!DOCTYPE html><html><body>Login | sellerboard</body></html>" * 2,
    b"tiny",
    b"just some words with no separators at all in the first line\n",
])
def test_bad_files_rejected(tmp_path, content):
    f = tmp_path / "bad.xlsx"
    f.write_bytes(content)
    with pytest.raises(FileValidationError):
        validate_download(f)


def test_target_path_layout(settings):
    day = Period(date(2025, 3, 7), date(2025, 3, 7), "day")
    assert target_path(settings, day, "Dashboard export.xlsx") == (
        settings.download_dir / "walmart" / "2025" / "sellerboard_walmart_2025-03-07.xlsx"
    )
    month = Period(date(2025, 3, 1), date(2025, 3, 31), "month")
    p = target_path(settings, month, "weird.bin")
    assert p.parent.name == "month" and p.name == "sellerboard_walmart_month_2025-03-01.xlsx"


def test_no_sales_day_saves_headers_only_file(settings):
    from app.downloader import _save_empty_export
    from app.file_reader import read_export

    period = Period(date(2025, 5, 18), date(2025, 5, 18), "day")
    result = _save_empty_export(settings, period)
    assert result.path.name == "sellerboard_walmart_2025-05-18.xlsx"
    [sheet] = read_export(result.path)
    assert sheet.rows == []
    assert {"units", "sales", "net_profit"} <= set(sheet.columns)
