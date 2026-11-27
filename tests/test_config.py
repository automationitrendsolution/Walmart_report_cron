from datetime import date

import pytest

from app.config import load_settings
from app.exceptions import ConfigurationError


def test_defaults(settings):
    assert settings.marketplace == "walmart"
    assert settings.backfill_start_date == date(2025, 1, 1)
    assert settings.granularity == "day"
    assert not settings.has_credentials


def test_database_required_unless_disabled(monkeypatch):
    with pytest.raises(ConfigurationError, match="DB_HOST"):
        load_settings()
    for k, v in {"DB_HOST": "db", "DB_NAME": "sales", "DB_USER": "u", "DB_PASSWORD": "p@ss:word"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert s.database.table == "sellerboard_walmart_daily"
    assert "p@ss:word" not in repr(s) and "p@ss:word" not in s.database.safe_url
    assert "p%40ss%3Aword" in s.database.url
    monkeypatch.setenv("PERIOD_GRANULARITY", "month")
    assert load_settings().database.table == "sellerboard_walmart_monthly"


def test_email_without_password_is_rejected(monkeypatch):
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv("SELLERBOARD_EMAIL", "a@b.com")
    with pytest.raises(ConfigurationError, match="SELLERBOARD_PASSWORD"):
        load_settings()


def test_password_not_in_repr_or_describe(monkeypatch):
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv("SELLERBOARD_EMAIL", "a@b.com")
    monkeypatch.setenv("SELLERBOARD_PASSWORD", "hunter2-secret")
    s = load_settings()
    assert "hunter2-secret" not in repr(s)
    assert "hunter2-secret" not in str(s.describe())


@pytest.mark.parametrize("name,value", [
    ("PERIOD_GRANULARITY", "year"),
    ("BACKFILL_START_DATE", "01-01-2025"),
    ("REPORT_TIMEZONE", "Mars/Olympus"),
    ("SELLERBOARD_BASE_URL", "http://app.sellerboard.com"),
    ("MAX_RETRIES", "0"),
])
def test_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        load_settings()
