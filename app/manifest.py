"""Download state, so a backfill can stop and resume anywhere.

One JSON file per marketplace and granularity, written atomically after every
period.  A killed run loses at most the period it was working on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .periods import Period
from .timeutils import now_utc

logger = get_logger("manifest")

STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Manifest:
    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.is_file():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Do not silently start over: that would re-download a whole year.
            backup = path.with_suffix(".corrupt.json")
            path.replace(backup)
            logger.error("Manifest %s was unreadable (%s); moved to %s", path, exc, backup)
            return cls(path=path)
        return cls(path=path, entries=dict(raw.get("periods", {})))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"updated_at": now_utc().isoformat(), "periods": dict(sorted(self.entries.items(), reverse=True))}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, period: Period) -> dict[str, Any] | None:
        return self.entries.get(period.key)

    def mark_done(self, period: Period, file: Path, size_bytes: int) -> None:
        self.entries[period.key] = {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "status": STATUS_DONE,
            "file": str(file),
            "bytes": size_bytes,
            "downloaded_at": now_utc().isoformat(timespec="seconds"),
        }
        self.save()

    def mark_loaded(self, period: Period, table: str, rows: int) -> None:
        """Record that the current file of ``period`` is in the database."""
        entry = self.entries[period.key]
        entry["db_table"] = table
        entry["db_rows"] = rows
        # Ties the load to this exact download; a refresh makes it pending again.
        entry["db_loaded_download"] = entry.get("downloaded_at")
        entry["db_loaded_at"] = now_utc().isoformat(timespec="seconds")
        entry.pop("db_error", None)
        self.save()

    def mark_load_failed(self, period: Period, error: str) -> None:
        entry = self.entries[period.key]
        entry["db_error"] = error[:500]
        self.save()

    def mark_failed(self, period: Period, error: str) -> None:
        previous = self.entries.get(period.key) or {}
        entry = dict(previous)
        entry.update(
            {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "last_error": error[:500],
                "failed_at": now_utc().isoformat(timespec="seconds"),
                "failures": int(previous.get("failures", 0)) + 1,
            }
        )
        # A refresh that fails keeps the earlier good file marked done.
        if previous.get("status") != STATUS_DONE:
            entry["status"] = STATUS_FAILED
        self.entries[period.key] = entry
        self.save()


def needs_download(
    period: Period,
    entry: dict[str, Any] | None,
    *,
    last_day: date,
    refresh_recent_days: int,
) -> str | None:
    """Why ``period`` should be downloaded on this run, or None to skip it."""
    if entry is None:
        return "new"
    if entry.get("status") != STATUS_DONE:
        return "retry"
    if entry.get("end") != period.end.isoformat():
        return "grown"
    if not Path(str(entry.get("file", ""))).is_file():
        return "file missing"
    if refresh_recent_days > 0 and period.end > last_day - timedelta(days=refresh_recent_days):
        return "recent refresh"
    return None


def needs_load(entry: dict[str, Any] | None, table: str) -> bool:
    """Whether a downloaded file has not yet been loaded (or was re-downloaded)."""
    if not entry or entry.get("status") != STATUS_DONE:
        return False
    if not Path(str(entry.get("file", ""))).is_file():
        return False
    return entry.get("db_table") != table or entry.get("db_loaded_download") != entry.get("downloaded_at")
