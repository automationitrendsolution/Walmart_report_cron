"""Orchestration and execution modes.

    periods (newest first) -> skip what the manifest already has
        -> dashboard URL for the period -> export -> validated file -> manifest
        -> parse -> MySQL (DELETE + INSERT for that period, one transaction)

A period that fails is recorded and the run moves on to the next one, so one
bad day never blocks the rest of the backfill.  Authentication failures stop
the run immediately, as does a streak of consecutive failures.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .auth import is_login_page, sign_in
from .browser import BrowserSession
from .config import Settings, default_table_name
from .database import Repository, build_repository
from .downloader import download_period
from .exceptions import (
    AuthenticationError,
    DatabaseError,
    DataError,
    PeriodsFailedError,
    SellerboardError,
    ServiceUnavailableError,
)
from .logging_config import get_logger, short_error
from .maintenance import cleanup_old_screenshots
from .file_reader import read_export
from .manifest import Manifest, needs_download, needs_load
from .periods import Period, build_periods, dashboard_url, last_reportable_day
from .retry import retry_call
from .runlock import run_lock
from .sellerboard import Dashboard
from .timeutils import now_utc, today_in

logger = get_logger("pipeline")


def has_display() -> bool:
    """Whether a visible (headed) browser can open on this machine."""
    if not sys.platform.startswith("linux"):
        return True  # Windows / macOS always have a desktop session
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class RunMode(str, Enum):
    RUN = "run"
    PLAN = "plan"
    LOGIN = "login"
    INSPECT = "inspect"
    LOAD_ONLY = "load-only"


@dataclass
class RunOptions:
    mode: RunMode = RunMode.RUN
    headed: bool = False
    force: bool = False
    limit: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    use_lock: bool = True
    #: Skip the database for this run even when it is configured.
    no_db: bool = False


@dataclass
class RunSummary:
    mode: str
    started_at: object = field(default_factory=now_utc)
    planned: int = 0
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    load_failed: list[str] = field(default_factory=list)
    rows_inserted: int = 0
    remaining_backlog: int = 0
    error: str | None = None

    def log(self) -> None:
        duration = (now_utc() - self.started_at).total_seconds()  # type: ignore[operator]
        failed = bool(self.error or self.failed or self.load_failed)
        logger.info("-" * 60)
        logger.info("Finished in %s: %s", _duration(duration), "OK" if not failed else "with problems")
        if self.mode == RunMode.RUN.value:
            logger.info("  Downloaded      : %d day(s)", len(self.downloaded))
        if self.mode != RunMode.PLAN.value:
            logger.info("  Saved to MySQL  : %d day(s), %s row(s)", len(self.loaded), f"{self.rows_inserted:,}")
        if self.failed:
            logger.info("  Failed download : %d day(s): %s", len(self.failed), _short_list(self.failed))
        if self.load_failed:
            logger.info("  Failed to save  : %d day(s): %s", len(self.load_failed), _short_list(self.load_failed))
        if self.mode == RunMode.RUN.value:
            logger.info("  Still to do     : %d day(s)", self.remaining_backlog)
        if self.failed or self.load_failed:
            logger.info("  Failed days are tried again on the next run. Details: logs/cron.log")
        if self.error:
            logger.info("  Stopped because : %s", self.error)
        logger.info("-" * 60)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _short_list(labels: list[str], limit: int = 8) -> str:
    shown = ", ".join(labels[:limit])
    return shown + (f" (+{len(labels) - limit} more)" if len(labels) > limit else "")


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        repository_factory: Callable[[], Repository] | None = None,
    ) -> None:
        """``repository_factory`` lets tests substitute SQLite for MySQL."""
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.mode.value)
        self._repository_factory = repository_factory
        # A caller-supplied repository is the caller's to dispose.
        self._owns_repository = repository_factory is None

    @property
    def db_enabled(self) -> bool:
        return not self.options.no_db and (
            self._repository_factory is not None or self.settings.database is not None
        )

    @property
    def table_name(self) -> str:
        if self.settings.database is not None:
            return self.settings.database.table
        return default_table_name(self.settings.marketplace, self.settings.granularity)

    # -- planning ----------------------------------------------------------

    def date_window(self) -> tuple[date, date]:
        today = today_in(self.settings.report_timezone)
        last = self.options.date_to or last_reportable_day(today, include_today=self.settings.include_today)
        first = self.options.date_from or self.settings.backfill_start_date
        return first, last

    def plan(self, manifest: Manifest) -> list[tuple[Period, str]]:
        first, last = self.date_window()
        todo: list[tuple[Period, str]] = []
        for period in build_periods(first, last, self.settings.granularity):
            reason = "forced" if self.options.force else needs_download(
                period,
                manifest.get(period),
                last_day=last,
                refresh_recent_days=self.settings.refresh_recent_days,
            )
            if reason:
                todo.append((period, reason))
        return todo

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        self.settings.ensure_directories()
        first, last = self.date_window()
        action = {"run": "download", "load-only": "load files into MySQL"}.get(
            self.options.mode.value, self.options.mode.value)
        logger.info("-" * 60)
        logger.info(
            "Sellerboard %s %s | days %s to %s | %s",
            self.settings.marketplace.capitalize(), action, first, last,
            f"MySQL table {self.table_name}" if self.db_enabled else "no database",
        )
        logger.info("-" * 60)
        for key, value in self.settings.describe().items():
            logger.debug("  %-16s: %s", key, value)
        try:
            with run_lock(self.settings.lock_file, enabled=self.options.use_lock and self.options.mode is not RunMode.PLAN):
                if self.options.mode is RunMode.PLAN:
                    self._run_plan()
                elif self.options.mode is RunMode.LOGIN:
                    self._run_login()
                elif self.options.mode is RunMode.INSPECT:
                    self._run_inspect()
                elif self.options.mode is RunMode.LOAD_ONLY:
                    self._run_load_only()
                else:
                    self._run_downloads()
        except SellerboardError as exc:
            self.summary.error = short_error(exc)
            logger.debug("Run failed", exc_info=True)
            raise
        except Exception as exc:
            self.summary.error = short_error(exc)
            logger.exception("Unexpected failure: %s", short_error(exc))
            raise
        finally:
            if self.options.mode in (RunMode.RUN, RunMode.PLAN, RunMode.LOAD_ONLY):
                self.summary.log()
        return self.summary

    # -- modes -------------------------------------------------------------

    def _run_plan(self) -> None:
        manifest = Manifest.load(self.settings.manifest_file)
        todo = self.plan(manifest)
        first, last = self.date_window()
        logger.info("%d day(s) would be downloaded (newest first):", len(todo))
        for period, reason in todo:
            print(f"  {period.label:<24} {reason}")
        if self.db_enabled:
            pending = self._load_pending(manifest)
            logger.info("%d downloaded day(s) not yet saved to MySQL", len(pending))
        self.summary.planned = len(todo)
        self.summary.remaining_backlog = len(todo)

    def _run_downloads(self) -> None:
        manifest = Manifest.load(self.settings.manifest_file)
        repository = self._open_repository()
        try:
            if repository is not None:
                self._load_periods(repository, manifest, self._load_pending(manifest))
            self._download_all(manifest, repository)
        finally:
            if repository is not None and self._owns_repository:
                repository.dispose()
        self._raise_if_incomplete()

    def _run_load_only(self) -> None:
        """Load already-downloaded files that are not in MySQL yet.  No browser."""
        if not self.db_enabled:
            raise DatabaseError("The database is disabled (DB_ENABLED=false or --no-db)")
        manifest = Manifest.load(self.settings.manifest_file)
        repository = self._open_repository()
        assert repository is not None
        try:
            periods = self._load_pending(manifest)
            if self.options.force:
                first, last = self.date_window()
                periods = [p for p in build_periods(first, last, self.settings.granularity)
                           if (manifest.get(p) or {}).get("status") == "done"]
            self.summary.planned = len(periods)
            self._load_periods(repository, manifest, periods)
        finally:
            if self._owns_repository:
                repository.dispose()
        self._raise_if_incomplete()

    def _raise_if_incomplete(self) -> None:
        if self.summary.failed:
            raise PeriodsFailedError(
                f"{len(self.summary.failed)} period(s) failed to download; retried on the next run"
            )
        if self.summary.load_failed:
            raise DatabaseError(
                f"{len(self.summary.load_failed)} period(s) failed to load; retried on the next run"
            )

    # -- database ----------------------------------------------------------

    def _open_repository(self) -> Repository | None:
        if not self.db_enabled:
            logger.debug("Database disabled; files are downloaded only")
            return None
        factory = self._repository_factory or (lambda: build_repository(self.settings.database))
        return retry_call(
            factory,
            description="Database connection",
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _load_pending(self, manifest: Manifest) -> list[Period]:
        """Downloaded periods in the window whose current file is not loaded."""
        first, last = self.date_window()
        return [p for p in build_periods(first, last, self.settings.granularity)
                if needs_load(manifest.get(p), self.table_name)]

    def _load_periods(self, repository: Repository, manifest: Manifest, periods: list[Period]) -> None:
        if periods:
            logger.info("Saving %d already-downloaded day(s) to MySQL first", len(periods))
        for period in periods:
            rows = self._load_one(repository, manifest, period)
            if rows is not None:
                logger.info("  %s  %s rows saved", period.label, rows)

    def _load_one(self, repository: Repository, manifest: Manifest, period: Period) -> int | None:
        """Parse the period's file and replace its rows.  Data and database
        problems are recorded, not raised -- the period is retried next run."""
        entry = manifest.get(period) or {}
        path = Path(str(entry.get("file", "")))
        try:
            sheets = read_export(path)
            result = retry_call(
                lambda: repository.replace_period(period, self.settings.marketplace, sheets, path.name),
                description=f"Load {period.label}",
                attempts=self.settings.max_retries,
                base_delay=self.settings.retry_backoff_seconds,
                max_delay=self.settings.retry_backoff_max_seconds,
            )
        except (DataError, DatabaseError) as exc:
            manifest.mark_load_failed(period, f"{type(exc).__name__}: {exc}")
            self.summary.load_failed.append(period.label)
            logger.error("%s  could not save to MySQL: %s", period.label, short_error(exc))
            return None
        manifest.mark_loaded(period, result.table, result.inserted)
        self.summary.loaded.append(period.label)
        self.summary.rows_inserted += result.inserted
        return result.inserted

    # -- downloads ---------------------------------------------------------

    def _download_all(self, manifest: Manifest, repository: Repository | None) -> None:
        todo = self.plan(manifest)
        limit = self.options.limit if self.options.limit is not None else self.settings.max_periods_per_run
        backlog = len(todo)
        if limit:
            todo = todo[:limit]
        self.summary.planned = len(todo)
        self.summary.remaining_backlog = backlog

        if not todo:
            logger.info("Nothing to download: every day is up to date")
            return
        refresh = sum(1 for _, reason in todo if reason == "recent refresh")
        logger.info(
            "%d day(s) to download%s%s",
            len(todo),
            f" ({refresh} recent day(s) refreshed, {len(todo) - refresh} new or retried)" if refresh else "",
            f"; {backlog} in total, the rest next run" if backlog > len(todo) else "",
        )

        session = BrowserSession(self.settings, headless=not self.options.headed and self.settings.headless)
        session.start()
        consecutive_failures = 0
        width = len(str(len(todo)))
        try:
            for index, (period, reason) in enumerate(todo, start=1):
                prefix = f"[{index:>{width}}/{len(todo)}] {period.label}"
                began = time.monotonic()
                logger.debug("%s: start (%s)", prefix, reason)
                try:
                    result = retry_call(
                        lambda: self._download_one(session, period),
                        description=prefix,
                        attempts=self.settings.max_retries,
                        base_delay=self.settings.retry_backoff_seconds,
                        max_delay=self.settings.retry_backoff_max_seconds,
                        on_retry=lambda _attempt, _exc: self._restart(session),
                    )
                except (AuthenticationError, ServiceUnavailableError) as exc:
                    # Neither improves by trying the next day: stop, keep progress.
                    manifest.mark_failed(period, str(exc))
                    self.summary.failed.append(period.label)
                    logger.error("%s  STOPPED: %s", prefix, short_error(exc))
                    raise
                except (SellerboardError, PlaywrightError, OSError) as exc:
                    manifest.mark_failed(period, f"{type(exc).__name__}: {exc}")
                    self.summary.failed.append(period.label)
                    logger.error("%s  FAILED: %s (will retry next run)", prefix, short_error(exc))
                    consecutive_failures += 1
                    if consecutive_failures >= self.settings.max_consecutive_failures:
                        raise PeriodsFailedError(
                            f"{consecutive_failures} periods failed in a row; stopping. "
                            "Check the screenshots in data/screenshots."
                        ) from exc
                    continue

                manifest.mark_done(period, result.path, result.size_bytes)
                self.summary.downloaded.append(period.label)
                rows = self._load_one(repository, manifest, period) if repository is not None else None
                self.summary.remaining_backlog -= 1
                if repository is None:
                    outcome = f"file saved ({result.size_bytes / 1024:.0f} KB)"
                elif rows is None:
                    outcome = "downloaded, MySQL save failed"
                else:
                    outcome = f"{rows} rows"
                notes = [n for n in (
                    "no sales that day" if result.empty else "",
                    "refreshed" if reason == "recent refresh" else "",
                ) if n]
                logger.info(
                    "%s  %-12s %4.0fs%s",
                    prefix, outcome, time.monotonic() - began,
                    f"  ({', '.join(notes)})" if notes else "",
                )
                consecutive_failures = 0
                if index < len(todo) and self.settings.pause_between_downloads_seconds:
                    time.sleep(self.settings.pause_between_downloads_seconds)
        finally:
            session.close()
            try:
                cleanup_old_screenshots(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)

    def _download_one(self, session: BrowserSession, period: Period):
        dashboard = Dashboard(session, self.settings)
        dashboard.open_period(period)
        return download_period(session, dashboard, self.settings, period)

    @staticmethod
    def _restart(session: BrowserSession) -> None:
        """Fresh Chromium for the next attempt; a crashed page cannot recover."""
        session.close()
        session.start()

    def _sample_period(self) -> Period:
        _first, last = self.date_window()
        return build_periods(last, last, "day")[0]

    def _run_login(self) -> None:
        """Sign in with a person watching, in a visible browser.

        On a server with no display (Linux without DISPLAY / WAYLAND_DISPLAY) a
        visible browser cannot start, so sign in headlessly with the configured
        credentials instead and confirm the dashboard opens.
        """
        if not has_display():
            self._run_headless_login()
            return
        period = self._sample_period()
        with BrowserSession(self.settings, headless=False) as session:
            page = session.page
            page.goto(dashboard_url(self.settings.base_url, self.settings.marketplace, period),
                      wait_until="domcontentloaded")
            if is_login_page(page) and self.settings.has_credentials:
                print("\nFilling in the configured credentials...")
                try:
                    sign_in(session, self.settings)
                except AuthenticationError as exc:
                    print(f"\nSellerboard needs you to finish by hand:\n  {exc}\n")
            print("\n" + "=" * 72)
            print("Finish anything Sellerboard still asks for in the browser window,")
            print("until the Walmart dashboard is visible. Then press Enter here.")
            print("=" * 72)
            input("Press Enter once the dashboard is showing... ")
            if is_login_page(page):
                raise AuthenticationError("Still on the login page; the session was not established.")
            print(f"\nSigned in. Session saved in {self.settings.session_dir}")
            print("Next: python run.py --inspect   (or: python run.py --limit 1)\n")

    def _run_headless_login(self) -> None:
        """``--login`` on a machine without a screen: credentials, no person."""
        if not self.settings.has_credentials:
            raise AuthenticationError(
                "No display is available for a visible browser, and SELLERBOARD_EMAIL / "
                "SELLERBOARD_PASSWORD are not set. Set them in .env and run --login again."
            )
        print("\nNo display found (headless server): signing in with the credentials from .env...")
        period = self._sample_period()
        with BrowserSession(self.settings, headless=True) as session:
            try:
                Dashboard(session, self.settings).open_period(period)
            except AuthenticationError as exc:
                raise AuthenticationError(
                    f"{exc} -- If Sellerboard wants a verification code, run --login from a "
                    "machine with a screen: e.g. 'ssh -X' to this server (with an X server "
                    "on your PC), or a desktop session. Screenshots are in "
                    f"{self.settings.screenshot_dir}."
                ) from exc
        print(f"\nSigned in. Session saved in {self.settings.session_dir}")
        print("Next: python run.py --inspect   (or: python run.py --limit 1)\n")

    def _run_inspect(self) -> None:
        """Open one day's dashboard and list its clickable controls."""
        period = self._sample_period()
        with BrowserSession(self.settings, headless=not self.options.headed and self.settings.headless) as session:
            dashboard = Dashboard(session, self.settings)
            dashboard.open_period(period)
            shot = session.screenshot("inspect-dashboard")
            elements = dashboard.inspect()
            print(f"\nDashboard for {period.label}; screenshot: {shot}")
            print(f"{len(elements)} visible clickable element(s):\n")
            for e in elements:
                print(f"  <{e['tag']}> id={e['id']!r} class={e['cls']!r} text={e['text']!r} title={e['title']!r}")
            print("\nPut the export button's locator at the top of 'export_trigger' in config/selectors.json.")
            if self.options.headed:
                input("\nBrowser left open for a look around. Press Enter to close... ")

