from datetime import date
from urllib.parse import parse_qs, urlsplit

from app.periods import Period, build_periods, dashboard_url, last_reportable_day, utc_midnight_epoch


def test_epochs_match_the_shared_url():
    # periodsPreset=custom-range:c|1788220800-1793404800 is Sep 1 .. Oct 31 2026
    assert utc_midnight_epoch(date(2026, 9, 1)) == 1788220800
    assert utc_midnight_epoch(date(2026, 10, 31)) == 1793404800


def test_dashboard_url_reproduces_the_shared_format():
    url = dashboard_url("https://app.sellerboard.com", "walmart",
                        Period(date(2026, 9, 1), date(2026, 10, 31), "month"))
    assert url == ("https://app.sellerboard.com/en/walmart/dashboard?periodsPreset="
                   "custom-range%3Ac%7C1788220800-1793404800&panelsPeriodKey=custom&compare=none")
    assert parse_qs(urlsplit(url).query)["periodsPreset"] == ["custom-range:c|1788220800-1793404800"]


def test_daily_periods_are_newest_first_and_complete():
    periods = build_periods(date(2025, 1, 1), date(2026, 11, 25), "day")
    assert periods[0].start == date(2026, 11, 25)
    assert periods[-1].start == date(2025, 1, 1)
    assert len(periods) == 365 + 329
    assert all(p.start == p.end for p in periods)
    assert [p.start for p in periods] == sorted((p.start for p in periods), reverse=True)


def test_month_periods_clip_to_window():
    periods = build_periods(date(2025, 1, 15), date(2026, 11, 25), "month")
    assert periods[0] == Period(date(2026, 11, 1), date(2026, 11, 25), "month")
    assert periods[1] == Period(date(2026, 10, 1), date(2026, 10, 31), "month")
    assert periods[-1] == Period(date(2025, 1, 15), date(2025, 1, 31), "month")
    assert len(periods) == 23


def test_week_periods_run_monday_to_sunday():
    periods = build_periods(date(2026, 11, 1), date(2026, 11, 25), "week")
    assert periods[0] == Period(date(2026, 11, 23), date(2026, 11, 25), "week")  # Mon..Wed
    assert periods[1] == Period(date(2026, 11, 16), date(2026, 11, 22), "week")
    assert periods[-1] == Period(date(2026, 11, 1), date(2026, 11, 1), "week")  # a Sunday


def test_empty_window():
    assert build_periods(date(2026, 2, 1), date(2026, 1, 1), "day") == []


def test_today_excluded_by_default():
    assert last_reportable_day(date(2026, 11, 26), include_today=False) == date(2026, 11, 25)
    assert last_reportable_day(date(2026, 11, 26), include_today=True) == date(2026, 11, 26)
