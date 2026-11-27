"""Sellerboard Walmart export.

Playwright opens the Sellerboard Walmart dashboard for one period at a time
(by URL), exports it, and stores one file per period -- newest first, back to
BACKFILL_START_DATE.  A manifest makes the backfill resumable, and recent
periods are refreshed on every run because Sellerboard keeps revising them.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
