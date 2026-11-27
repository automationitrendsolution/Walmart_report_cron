"""Sellerboard dashboard navigation.

Each period is opened directly by URL (see :mod:`app.periods`), so no
date-picker interaction is needed.  When the saved session has expired the
dashboard redirects to the login page; this module signs in and reloads.
"""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .auth import assert_not_in_maintenance, is_login_page, sign_in
from .browser import BrowserSession, safe_url
from .config import Settings
from .exceptions import AuthenticationError, NavigationError
from .logging_config import get_logger
from .periods import Period, dashboard_url
from .selectors import find_first

logger = get_logger("dashboard")

_INSPECT_JS = """
() => {
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = [];
  for (const e of document.querySelectorAll(
      "button, a, [role=button], [role=menuitem], [title], [data-original-title], i[class*=download], i[class*=export]")) {
    if (!visible(e)) continue;
    const text = (e.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 60);
    const title = e.getAttribute("title") || e.getAttribute("data-original-title") || e.getAttribute("aria-label") || "";
    if (!text && !title) continue;
    out.push({tag: e.tagName.toLowerCase(), id: e.id || "", cls: (e.className && e.className.baseVal === undefined ? e.className : "").toString().slice(0, 80), text, title});
  }
  return out;
}
"""


class Dashboard:
    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @property
    def page(self):
        return self.session.page

    def open_period(self, period: Period) -> None:
        """Load the dashboard with ``period`` selected, signing in if needed."""
        url = dashboard_url(self.settings.base_url, self.settings.marketplace, period)
        self._goto(url)
        if is_login_page(self.page):
            logger.info("Sellerboard session expired; signing in again")
            sign_in(self.session, self.settings)
            self._goto(url)
            if is_login_page(self.page):
                assert_not_in_maintenance(self.session)
                self.session.capture_failure("still-on-login")
                raise AuthenticationError("Still on the login page after signing in.")

        if f"/{self.settings.marketplace}/" not in self.page.url.lower():
            assert_not_in_maintenance(self.session)
            self.session.capture_failure("unexpected-page")
            raise NavigationError(
                f"Expected the {self.settings.marketplace} dashboard, landed on {safe_url(self.page.url)}"
            )

        if find_first(self.page, "dashboard_ready", timeout_ms=self.settings.browser_timeout_ms) is None:
            assert_not_in_maintenance(self.session)
            self.session.capture_failure("dashboard-not-ready")
            raise NavigationError(
                "The dashboard did not finish loading (config/selectors.json 'dashboard_ready')."
            )
        self._wait_for_data()
        logger.debug("Dashboard loaded for %s", period.label)

    def wait_for_idle(self) -> None:
        self._wait_for_data()

    def _goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Timed out opening {safe_url(url)}") from exc

    def _wait_for_data(self) -> None:
        """Let the dashboard's XHRs finish and spinners disappear."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.settings.browser_timeout_ms)
        except PlaywrightTimeoutError:
            logger.debug("Dashboard never reached networkidle; continuing")
        spinner = find_first(self.page, "loading_indicator", timeout_ms=500)
        if spinner is not None:
            try:
                spinner.wait_for(state="hidden", timeout=self.settings.browser_timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning("Loading indicator still visible after %d ms", self.settings.browser_timeout_ms)
        if self.settings.page_settle_ms:
            self.page.wait_for_timeout(self.settings.page_settle_ms)

    def inspect(self) -> list[dict[str, str]]:
        """Visible clickable elements -- used to find the export control."""
        return list(self.page.evaluate(_INSPECT_JS))
