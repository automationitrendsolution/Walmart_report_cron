"""Shared fixtures.  Nothing here touches Sellerboard."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ignore the developer's .env and exported settings."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_a, **_k: False)
    prefixes = ("SELLERBOARD_", "BACKFILL_", "PERIOD_", "INCLUDE_", "REFRESH_", "MAX_", "PAUSE_",
                "REPORT_", "DOWNLOAD_", "SCREENSHOT_", "STATE_", "LOG_", "SESSION_", "LOCK_",
                "HEADLESS", "BROWSER_", "PAGE_", "SLOW_MO", "VIEWPORT_", "RETRY_", "DB_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, name in (("DOWNLOAD_DIR", "downloads"), ("SCREENSHOT_DIR", "screenshots"),
                      ("STATE_DIR", "state"), ("LOG_DIR", "logs"), ("SESSION_DIR", "session")):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setenv("DB_ENABLED", "false")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded
