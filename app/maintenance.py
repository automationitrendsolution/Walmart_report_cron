"""Housekeeping for files this project created.

Only failure screenshots are pruned.  Downloaded exports are the product of the
run and are never deleted automatically.
"""

from __future__ import annotations

import re

from .config import Settings
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("maintenance")

_OWNED_SCREENSHOT = re.compile(r"^\d{8}T\d{6}Z_.+\.png$", re.IGNORECASE)


def cleanup_old_screenshots(settings: Settings) -> int:
    cutoff = now_utc().timestamp() - settings.screenshot_retention_days * 86_400
    removed = 0
    if not settings.screenshot_dir.is_dir():
        return 0
    for path in settings.screenshot_dir.iterdir():
        if not (path.is_file() and _OWNED_SCREENSHOT.match(path.name)):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Could not remove %s: %s", path, exc)
    if removed:
        logger.debug("Removed %d screenshot(s) older than %d day(s)", removed, settings.screenshot_retention_days)
    return removed
