"""Report periods and the dashboard URLs that select them.

Sellerboard encodes a custom range in the dashboard URL as::

    periodsPreset=custom-range:c|<start>-<end>

where ``start`` and ``end`` are Unix seconds for **UTC midnight** of the first
and last day, both inclusive.  For example ``1788220800-1793404800`` is
2026-09-01 .. 2026-10-31.  So any period can be opened directly by URL, with
no date-picker clicking.

Periods are produced **newest first**, so the backfill walks back in time and
the most recent data, which matters most, lands first.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

from .config import UTC


@dataclass(frozen=True, order=True)
class Period:
    start: date
    end: date
    granularity: str

    @property
    def key(self) -> str:
        """Stable identity in the manifest.  Keyed on the start date only, so
        a period that is still growing (the current week or month) keeps one
        entry and its file is replaced, not duplicated."""
        return self.start.isoformat()

    @property
    def label(self) -> str:
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()}..{self.end.isoformat()}"

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def file_stem(self, marketplace: str) -> str:
        if self.granularity == "day":
            return f"sellerboard_{marketplace}_{self.start.isoformat()}"
        return f"sellerboard_{marketplace}_{self.granularity}_{self.start.isoformat()}"


def utc_midnight_epoch(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp())


def dashboard_url(base_url: str, marketplace: str, period: Period) -> str:
    """The dashboard URL with ``period`` preselected."""
    preset = f"custom-range:c|{utc_midnight_epoch(period.start)}-{utc_midnight_epoch(period.end)}"
    return (
        f"{base_url}/en/{marketplace}/dashboard"
        f"?periodsPreset={quote(preset, safe='')}&panelsPeriodKey=custom&compare=none"
    )


def last_reportable_day(today: date, *, include_today: bool) -> date:
    """Newest day the run should cover.  Today is partial, so off by default."""
    return today if include_today else today - timedelta(days=1)


def build_periods(first_day: date, last_day: date, granularity: str) -> list[Period]:
    """All periods covering ``first_day..last_day``, newest first.

    Week periods run Monday to Sunday and month periods follow the calendar.
    The newest and oldest periods are clipped to the range, so a period's
    ``end`` can move forward on later runs while its ``key`` stays the same.
    """
    if last_day < first_day:
        return []

    periods: list[Period] = []
    cursor = last_day
    while cursor >= first_day:
        if granularity == "day":
            start = cursor
        elif granularity == "week":
            start = cursor - timedelta(days=cursor.weekday())
        elif granularity == "month":
            start = cursor.replace(day=1)
        else:  # pragma: no cover - validated in config
            raise ValueError(f"unknown granularity {granularity!r}")

        natural_end = _natural_end(start, granularity)
        periods.append(
            Period(start=max(start, first_day), end=min(natural_end, last_day), granularity=granularity)
        )
        cursor = start - timedelta(days=1)
    return periods


def _natural_end(start: date, granularity: str) -> date:
    if granularity == "day":
        return start
    if granularity == "week":
        return start + timedelta(days=6)
    return start.replace(day=calendar.monthrange(start.year, start.month)[1])
