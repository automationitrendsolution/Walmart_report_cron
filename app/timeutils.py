"""Timezone-aware time handling.

The server's local clock zone is never consulted: "today" is defined in
``REPORT_TIMEZONE`` (the Sellerboard account's zone), so the same code gives
the same dates on a UTC Linux box and an IST Windows laptop.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import UTC


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def today_in(tz: ZoneInfo, *, reference: datetime | None = None) -> date:
    return (reference or now_utc()).astimezone(tz).date()
