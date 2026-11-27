"""Sellerboard sign-in.

The persistent browser profile is the primary mechanism; this module is the
fallback for when the session has expired.  It fills the login form with the
configured credentials.  It never retries a rejected password, and it stops on
any verification step (email code, CAPTCHA) instead of trying to get past it --
``python run.py --login`` is how a person completes those once.
"""

from __future__ import annotations

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession, safe_url
from .config import Settings
from .exceptions import AuthenticationError, ServiceUnavailableError
from .logging_config import get_logger
from .selectors import find_first, is_present

logger = get_logger("auth")

_SETTLE_MS = 20_000


def is_login_page(page: Page) -> bool:
    return "/auth/login" in page.url.lower() or is_present(page, "login_password_input", timeout_ms=1_000)


def assert_not_in_maintenance(session: BrowserSession) -> None:
    """Stop the run if Sellerboard is showing its maintenance banner."""
    if is_present(session.page, "maintenance_marker", timeout_ms=1_500):
        session.capture_failure("sellerboard-maintenance")
        raise ServiceUnavailableError(
            "Sellerboard is in system maintenance ('try again in 5-10 minutes'). "
            "Stopped without touching the login; progress is saved -- run again later."
        )


def sign_in(session: BrowserSession, settings: Settings) -> None:
    """Submit the login form with the configured credentials.

    Raises:
        AuthenticationError: No credentials, credentials rejected, or a
            verification step needs a person.  Never retried.
    """
    if not settings.can_auto_login:
        raise AuthenticationError(
            "The Sellerboard session is missing or expired and automatic sign-in is not "
            "available (set SELLERBOARD_EMAIL / SELLERBOARD_PASSWORD, or run "
            "'python run.py --login')."
        )

    page = session.page
    if "/auth/login" not in page.url.lower():
        page.goto(settings.login_url, wait_until="domcontentloaded")

    assert_not_in_maintenance(session)
    logger.info("Signing in to Sellerboard as %s ...", mask_email(settings.email or ""))
    email = find_first(page, "login_email_input", timeout_ms=10_000)
    password = find_first(page, "login_password_input", timeout_ms=5_000)
    if email is None or password is None:
        session.capture_failure("login-form-missing")
        raise AuthenticationError(
            "The Sellerboard login form did not render as expected "
            "(config/selectors.json 'login_email_input' / 'login_password_input')."
        )

    # fill() sets the value through the DOM; nothing is typed as keystrokes.
    email.fill(settings.email or "")
    password.fill(settings.password or "")

    remember = find_first(page, "login_keep_logged_in", timeout_ms=1_500)
    if remember is not None:
        try:
            remember.check(force=True)
        except Exception as exc:  # pragma: no cover - cosmetic
            logger.debug("Could not tick 'keep me logged in': %s", exc)

    submit = find_first(page, "login_submit_button", timeout_ms=5_000)
    if submit is None:
        session.capture_failure("login-submit-missing")
        raise AuthenticationError(
            "Could not find the login submit button (config/selectors.json 'login_submit_button')."
        )
    submit.click()
    _settle(page)

    if is_present(page, "login_challenge_marker", timeout_ms=2_000):
        session.capture_failure("login-verification-required")
        raise AuthenticationError(
            "Sellerboard asked for an extra verification step (code or CAPTCHA). "
            "Complete it once by hand with 'python run.py --login'; the session is "
            "then saved and reused by scheduled runs."
        )

    # Checked before blaming the password: during maintenance Sellerboard
    # bounces every sign-in back to this form with the banner on it.
    assert_not_in_maintenance(session)

    if is_login_page(page):
        message = _error_text(page)
        session.capture_failure("login-rejected")
        raise AuthenticationError(
            f"Sellerboard rejected the sign-in{': ' + message if message else ''}. "
            "Check SELLERBOARD_EMAIL and SELLERBOARD_PASSWORD. Not retrying, to avoid "
            "locking the account."
        )
    logger.debug("Signed in; now at %s", safe_url(page.url))


def _error_text(page: Page) -> str:
    banner = find_first(page, "login_error_message", timeout_ms=2_000)
    if banner is None:
        return ""
    try:
        return banner.inner_text(timeout=2_000).strip()[:300]
    except Exception:  # pragma: no cover - defensive
        return ""


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=_SETTLE_MS)
        page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
    except PlaywrightTimeoutError:
        logger.debug("Page did not go idle within %d ms; continuing", _SETTLE_MS)


def mask_email(email: str) -> str:
    """``someone@example.com`` -> ``s*****e@example.com``."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"
