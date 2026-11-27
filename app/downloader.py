"""Dashboard export: trigger, capture, validate and store the file.

The download is captured through Playwright's download event, so a file is only
accepted once Chromium reports the transfer finished.  It is saved to a
temporary name, validated, and only then moved over the final path -- a failed
refresh never destroys the previous good file.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Download

from .browser import BrowserSession
from .config import Settings
from .exceptions import DownloadError, FileValidationError
from .logging_config import get_logger
from .periods import Period
from .auth import assert_not_in_maintenance
from .sellerboard import Dashboard
from .selectors import find_first, is_present

logger = get_logger("downloader")

MIN_FILE_BYTES = 32
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml")
_ZIP_MAGIC = b"PK\x03\x04"  # .xlsx is a zip container
_KNOWN_EXTENSIONS = (".xlsx", ".xls", ".csv", ".zip")

#: How long one click gets to produce a menu or a file before it is repeated.
#: Exports take 5-20 s; re-clicking sooner requests the same export twice.
MENU_WAIT_MS = 60_000
MENU_ATTEMPTS = 3

#: How long a disabled export button gets to become enabled before the day is
#: treated as having no products (Sellerboard disables export on an empty table).
EMPTY_WAIT_MS = 20_000

#: Columns of the Products export, used for the empty file of a no-sales day.
EMPTY_EXPORT_HEADERS = (
    "Product", "SKU", "Units", "Refunds", "Sales", "Ads", "TACOS", "Shipping costs",
    "Fees", "Cost of Goods", "GrossProfit", "Margin", "Expenses", "Net profit", "ROI", "AOV",
)


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    suggested_filename: str
    #: True when Sellerboard had no products for the day (headers-only file).
    empty: bool = False


def target_path(settings: Settings, period: Period, suggested: str) -> Path:
    """``<DOWNLOAD_DIR>/<marketplace>/<year>/<stem><ext>`` -- stable per period."""
    ext = Path(suggested).suffix.lower()
    if ext not in _KNOWN_EXTENSIONS:
        ext = ".xlsx"
    folder = settings.download_dir / settings.marketplace / str(period.start.year)
    if period.granularity != "day":
        folder = folder / period.granularity
    return folder / f"{period.file_stem(settings.marketplace)}{ext}"


def download_period(
    session: BrowserSession, dashboard: Dashboard, settings: Settings, period: Period
) -> DownloadResult:
    """Export the currently loaded dashboard and store it for ``period``."""
    if _export_disabled(session, settings):
        return _save_empty_export(settings, period)
    download = _request_download(session, dashboard, settings)

    failure = download.failure()
    if failure:
        session.capture_failure("download-failed")
        raise DownloadError(f"Download failed: {failure}")

    suggested = download.suggested_filename or "export.xlsx"
    final = target_path(settings, period, suggested)
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".part")
    try:
        download.save_as(str(partial))
    except Exception as exc:
        session.capture_failure("download-save-failed")
        raise DownloadError(f"Could not save download to {partial}: {exc}") from exc

    try:
        size = validate_download(partial)
    except FileValidationError:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, final)
    logger.debug("Saved %s (%.1f KB, Sellerboard name %r)", final, size / 1024, suggested)
    return DownloadResult(path=final, size_bytes=size, suggested_filename=suggested)


def _request_download(session: BrowserSession, dashboard: Dashboard, settings: Settings) -> Download:
    """Click the export button, then wait for the file.

    Sellerboard builds the export server-side (a progress popup polls for it)
    and then downloads it, so after one click the wait covers the whole
    DOWNLOAD_TIMEOUT_MS.  A format menu, if one opens, is answered with Excel.
    The click is only repeated when nothing at all happened.
    """
    page = session.page
    started: list[Download] = []

    def on_download(download: Download) -> None:
        started.append(download)

    page.on("download", on_download)
    try:
        for attempt in range(1, MENU_ATTEMPTS + 1):
            trigger = find_first(page, "export_trigger", timeout_ms=settings.browser_timeout_ms)
            if trigger is None:
                assert_not_in_maintenance(session)
                session.capture_failure("export-trigger-missing")
                raise DownloadError(
                    "No export button on the dashboard (config/selectors.json 'export_trigger'). "
                    "Run 'python run.py --inspect' to list the page's buttons."
                )
            trigger.click()
            logger.debug("Export requested (attempt %d); waiting for the file", attempt)

            deadline = time.monotonic() + settings.download_timeout_ms / 1000
            activity_deadline = time.monotonic() + MENU_WAIT_MS / 1000
            menu_clicked = False
            while time.monotonic() < deadline:
                if started:
                    return _await_finished(started[0])
                if is_present(page, "export_emailed_marker", timeout_ms=200):
                    session.capture_failure("export-emailed")
                    raise DownloadError(
                        "Sellerboard chose to e-mail this export instead of downloading it "
                        "(usually a very large range). Use a smaller PERIOD_GRANULARITY."
                    )
                if not menu_clicked:
                    option = find_first(page, "export_format_option", timeout_ms=200)
                    if option is not None:
                        logger.debug("Export menu open; choosing the format")
                        option.click()
                        menu_clicked = True
                        continue
                busy = is_present(page, "export_progress_marker", timeout_ms=200)
                if not busy and not menu_clicked and time.monotonic() > activity_deadline:
                    break  # the click did nothing visible; click again
                page.wait_for_timeout(500)
            else:
                assert_not_in_maintenance(session)
                session.capture_failure("download-timeout")
                raise DownloadError(
                    f"No file arrived within {settings.download_timeout_ms} ms after requesting the "
                    "export. Raise DOWNLOAD_TIMEOUT_MS if Sellerboard is slow to build exports."
                )

            logger.debug("Export click %d/%d produced nothing; trying again", attempt, MENU_ATTEMPTS)
            page.keyboard.press("Escape")
            dashboard.wait_for_idle()
    finally:
        page.remove_listener("download", on_download)

    session.capture_failure("export-no-download")
    raise DownloadError(
        f"Export never produced a file after {MENU_ATTEMPTS} clicks "
        "(config/selectors.json 'export_trigger' / 'export_format_option')."
    )


def _export_disabled(session: BrowserSession, settings: Settings) -> bool:
    """True when the export button stays disabled -- the day has no products."""
    page = session.page
    trigger = find_first(page, "export_trigger", timeout_ms=settings.browser_timeout_ms)
    if trigger is None:
        return False  # _request_download reports the missing button
    deadline = time.monotonic() + EMPTY_WAIT_MS / 1000
    while True:
        try:
            if not trigger.is_disabled(timeout=2_000):
                return False
        except Exception:
            return False
        if time.monotonic() > deadline:
            return True
        page.wait_for_timeout(1_000)


def _save_empty_export(settings: Settings, period: Period) -> DownloadResult:
    """Store a headers-only workbook so the day is recorded as zero sales."""
    from openpyxl import Workbook

    final = target_path(settings, period, "empty.xlsx")
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".part")
    wb = Workbook()
    wb.active.title = "TDSheet"
    wb.active.append(list(EMPTY_EXPORT_HEADERS))
    with partial.open("wb") as handle:
        wb.save(handle)
    size = validate_download(partial)
    os.replace(partial, final)
    logger.debug(
        "Export button disabled for %s: no products that day; saved an empty file %s",
        period.label, final,
    )
    return DownloadResult(path=final, size_bytes=size, suggested_filename="", empty=True)


def _await_finished(download: Download) -> Download:
    download.path()  # blocks until the transfer completes
    return download


def validate_download(path: Path) -> int:
    """Check ``path`` looks like a spreadsheet export; return its size.

    Raises:
        FileValidationError: Missing, tiny, HTML, or not CSV/XLSX-shaped.
    """
    if not path.is_file():
        raise FileValidationError(f"Downloaded file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_FILE_BYTES:
        raise FileValidationError(f"Downloaded file {path} is only {size} bytes")

    with path.open("rb") as handle:
        head = handle.read(4096)

    if head.startswith(_ZIP_MAGIC) or head.startswith(b"\xd0\xcf\x11\xe0"):  # xlsx / legacy xls
        return size
    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        raise FileValidationError(
            f"{path} is an HTML page, not an export -- usually an expired session."
        )
    text = head.decode("utf-8-sig", errors="replace")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not any(sep in first_line for sep in (",", ";", "\t")):
        raise FileValidationError(f"{path} has no delimited header row: {first_line[:120]!r}")
    return size
