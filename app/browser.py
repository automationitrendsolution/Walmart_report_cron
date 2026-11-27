"""Playwright browser lifecycle.

A *persistent* Chromium profile keeps the Sellerboard session (cookies with
"keep me logged in"), so most runs never see the login form.  One browser is
shared across all periods in a run; each period reuses the same page.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .config import Settings
from .exceptions import NavigationError
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("browser")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_url(url: str) -> str:
    """Scheme, host and path only -- query strings can carry tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme and not parts.netloc:
        return url.split("?", 1)[0]
    trimmed = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{trimmed}?<query>" if parts.query else trimmed


class BrowserSession:
    """Owns the Playwright driver, the persistent context and the page.

    Always use it as a context manager so Chromium is closed even on failure.
    """

    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None and not isinstance(exc, KeyboardInterrupt):
            self.capture_failure("unhandled-error")
        self.close()

    def start(self) -> None:
        self.settings.session_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Launching Chromium (headless=%s, timezone=%s)",
            self.headless,
            self.settings.report_timezone.key,
        )
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.session_dir),
                headless=self.headless,
                accept_downloads=True,
                args=list(self.settings.browser_args),
                slow_mo=self.settings.slow_mo_ms or 0,
                user_agent=self.settings.user_agent,
                viewport={
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                locale="en-US",
                timezone_id=self.settings.report_timezone.key,
            )
        except Exception as exc:  # pragma: no cover - environment specific
            self.close()
            raise NavigationError(f"Failed to launch Chromium: {exc}") from exc

        self._context.set_default_timeout(self.settings.browser_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        """Close the context and stop the driver.  Never raises."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error closing browser context: %s", exc)
            finally:
                self._context = None
                self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error stopping Playwright: %s", exc)
            finally:
                self._playwright = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise NavigationError("Browser session is not started")
        return self._page

    def screenshot(self, label: str) -> Path | None:
        if self._page is None:
            return None
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        safe = _SAFE_NAME.sub("-", label).strip("-") or "screenshot"
        path = self.settings.screenshot_dir / f"{stamp}_{safe}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=True)
            logger.debug("Saved screenshot %s", path)
            return path
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Could not capture screenshot %s: %s", label, exc)
            return None

    def capture_failure(self, label: str) -> dict[str, str]:
        """Screenshot plus URL (query stripped) and title.  Never dumps HTML."""
        details: dict[str, str] = {}
        if self._page is None:
            return details
        try:
            details["url"] = safe_url(self._page.url)
            details["title"] = self._page.title()
        except Exception:  # pragma: no cover - page may be dead
            pass
        shot = self.screenshot(label)
        if shot is not None:
            details["screenshot"] = str(shot)
        if details:
            logger.debug("Browser failure context (%s): %s", label, details)
        return details
