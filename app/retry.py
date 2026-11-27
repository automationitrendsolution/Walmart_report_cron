"""Retry with exponential backoff, restricted to transient failures.

Authentication, CAPTCHA, MFA and configuration errors are deliberately excluded:
hammering a security challenge is both futile and hostile to the service.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError

from .exceptions import SellerboardError
from .logging_config import get_logger, short_error

T = TypeVar("T")
logger = get_logger("retry")


def is_transient(error: BaseException) -> bool:
    """Whether ``error`` is worth retrying."""
    if isinstance(error, SellerboardError):
        return error.transient
    # Timeouts, but also a crashed page or browser ("Target page, context or
    # browser has been closed"): each attempt starts a fresh Chromium, so any
    # Playwright failure is worth another try.
    if isinstance(error, PlaywrightError):
        return True
    return isinstance(error, (ConnectionError, TimeoutError, OSError))


def compute_delay(attempt: int, base: float, maximum: float) -> float:
    """Exponential backoff with full jitter, capped at ``maximum``."""
    if base <= 0:
        return 0.0
    exponential = min(base * (2 ** (attempt - 1)), maximum if maximum > 0 else base)
    return random.uniform(0, exponential)


def retry_call(
    operation: Callable[[], T],
    *,
    description: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying transient failures up to ``attempts`` times.

    Raises:
        The last exception raised by ``operation`` once attempts are exhausted,
        or immediately if the failure is not transient.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: BaseException
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last_error = exc
            # Full error (Playwright call log etc.) goes to the log file only.
            logger.debug("%s attempt %d/%d failed", description, attempt, attempts, exc_info=True)
            if not is_transient(exc) or attempt >= attempts:
                raise
            delay = compute_delay(attempt, base_delay, max_delay)
            logger.warning(
                "%s: try %d of %d failed (%s); trying again in %.0fs",
                description,
                attempt,
                attempts,
                short_error(exc),
                delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc)
            if delay:
                sleep(delay)
    raise last_error  # pragma: no cover - loop always returns or raises
