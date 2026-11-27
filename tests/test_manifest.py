from datetime import date

from app.manifest import Manifest, needs_download
from app.periods import Period

LAST = date(2026, 11, 25)


def _day(d):
    return Period(d, d, "day")


def _file(tmp_path):
    f = tmp_path / "x.xlsx"
    f.write_bytes(b"data")
    return f


def test_new_failed_and_done(tmp_path):
    m = Manifest.load(tmp_path / "m.json")
    old = _day(date(2026, 1, 10))
    assert needs_download(old, m.get(old), last_day=LAST, refresh_recent_days=3) == "new"

    m.mark_failed(old, "boom")
    assert needs_download(old, m.get(old), last_day=LAST, refresh_recent_days=3) == "retry"

    m.mark_done(old, _file(tmp_path), 4)
    assert needs_download(old, m.get(old), last_day=LAST, refresh_recent_days=3) is None
    assert Manifest.load(tmp_path / "m.json").get(old)["status"] == "done"


def test_recent_periods_are_refreshed(tmp_path):
    m = Manifest.load(tmp_path / "m.json")
    f = _file(tmp_path)
    days = (date(2026, 11, 25), date(2026, 11, 23), date(2026, 11, 22))
    for d in days:
        m.mark_done(_day(d), f, 4)
    reasons = {d: needs_download(_day(d), m.get(_day(d)), last_day=LAST, refresh_recent_days=3) for d in days}
    assert reasons == {days[0]: "recent refresh", days[1]: "recent refresh", days[2]: None}


def test_grown_period_and_missing_file(tmp_path):
    m = Manifest.load(tmp_path / "m.json")
    f = _file(tmp_path)
    partial = Period(date(2026, 10, 1), date(2026, 10, 20), "month")
    m.mark_done(partial, f, 4)
    full = Period(date(2026, 10, 1), date(2026, 10, 31), "month")
    assert needs_download(full, m.get(full), last_day=LAST, refresh_recent_days=0) == "grown"

    f.unlink()
    assert needs_download(partial, m.get(partial), last_day=LAST, refresh_recent_days=0) == "file missing"


def test_failed_refresh_keeps_done_status(tmp_path):
    m = Manifest.load(tmp_path / "m.json")
    p = _day(date(2026, 11, 25))
    m.mark_done(p, _file(tmp_path), 4)
    m.mark_failed(p, "timeout")
    assert m.get(p)["status"] == "done"
    assert m.get(p)["failures"] == 1


def test_corrupt_manifest_is_set_aside(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json")
    assert Manifest.load(path).entries == {}
    assert (tmp_path / "m.corrupt.json").exists()
