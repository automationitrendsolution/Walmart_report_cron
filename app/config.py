"""Environment-driven configuration.

Every secret arrives through the process environment (``.env`` locally,
exported variables under cron / Task Scheduler).  Validation fails fast: a bad
setting stops the run before a browser opens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = ZoneInfo("UTC")

GRANULARITIES = ("day", "week", "month")

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _get_date(name: str, default: date | None) -> date | None:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc


def _get_zone(name: str, default: str) -> ZoneInfo:
    raw = _get(name, default) or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone "
            "(install 'tzdata' if the system zone database is missing)"
        ) from exc


#: Keeps Chromium alive on small Linux hosts whose /dev/shm is 64 MB.
_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    table: str = "sellerboard_walmart_daily"
    connect_timeout_seconds: int = 30
    pool_recycle_seconds: int = 1800
    ssl_ca: Path | None = None
    ssl_verify_cert: bool = False
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
        elif self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def safe_url(self) -> str:
        return f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    base_url: str
    marketplace: str
    email: str | None
    password: str | None = field(repr=False)
    auto_login: bool

    #: Oldest day the backfill reaches.  Periods are downloaded newest first
    #: until this date.
    backfill_start_date: date
    granularity: str
    include_today: bool
    #: Recent periods are downloaded again on every run because Sellerboard
    #: keeps revising them (refunds, ad attribution, fee settlement).
    refresh_recent_days: int
    #: Cap on downloads per run; 0 means no cap.
    max_periods_per_run: int
    pause_between_downloads_seconds: float
    #: Stop the run after this many periods fail back to back -- something
    #: systemic is wrong, and hammering the site will not fix it.
    max_consecutive_failures: int
    report_timezone: ZoneInfo
    #: None when DB_ENABLED=false -- files are then downloaded only.
    database: DatabaseConfig | None

    download_dir: Path
    screenshot_dir: Path
    state_dir: Path
    log_dir: Path
    session_dir: Path
    lock_file: Path

    headless: bool
    browser_timeout_ms: int
    download_timeout_ms: int
    page_settle_ms: int
    slow_mo_ms: int
    user_agent: str | None
    browser_args: tuple[str, ...]
    viewport_width: int
    viewport_height: int

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float

    log_level: str
    log_retention_days: int
    screenshot_retention_days: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)

    @property
    def can_auto_login(self) -> bool:
        return self.auto_login and self.has_credentials

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/en/auth/login/"

    @property
    def manifest_file(self) -> Path:
        return self.state_dir / f"manifest_{self.marketplace}_{self.granularity}.json"

    def ensure_directories(self) -> None:
        for path in (
            self.download_dir,
            self.screenshot_dir,
            self.state_dir,
            self.log_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "base_url": self.base_url,
            "marketplace": self.marketplace,
            "credentials": "configured" if self.has_credentials else "not configured",
            "auto_login": self.can_auto_login,
            "backfill_start": self.backfill_start_date.isoformat(),
            "granularity": self.granularity,
            "include_today": self.include_today,
            "refresh_recent": f"{self.refresh_recent_days} day(s)",
            "max_per_run": self.max_periods_per_run or "unlimited",
            "timezone": str(self.report_timezone),
            "download_dir": str(self.download_dir),
            "manifest": str(self.manifest_file),
            "session_dir": str(self.session_dir),
            "database": f"{self.database.safe_url} table={self.database.table}"
            if self.database else "disabled",
            "headless": self.headless,
            "max_retries": self.max_retries,
        }


_GRANULARITY_SUFFIX = {"day": "daily", "week": "weekly", "month": "monthly"}


def default_table_name(marketplace: str, granularity: str) -> str:
    return f"sellerboard_{marketplace}_{_GRANULARITY_SUFFIX[granularity]}"


def _database(marketplace: str, granularity: str) -> DatabaseConfig | None:
    if not _get_bool("DB_ENABLED", True):
        return None
    missing = [n for n in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not _get(n)]
    if missing:
        raise ConfigurationError(
            f"{', '.join(missing)} not set. Fill in the MySQL settings, or set DB_ENABLED=false "
            "to only download files."
        )
    table = _get("DB_TABLE") or default_table_name(marketplace, granularity)
    if not table.replace("_", "").isalnum() or len(table) > 64:
        raise ConfigurationError(f"DB_TABLE must be a plain MySQL identifier, got {table!r}")

    ssl_ca = None
    raw_ca = _get("DB_SSL_CA")
    if raw_ca:
        ssl_ca = _get_path("DB_SSL_CA", Path(raw_ca))
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    return DatabaseConfig(
        host=_get("DB_HOST") or "",
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=_get("DB_NAME") or "",
        user=_get("DB_USER") or "",
        password=_get("DB_PASSWORD") or "",
        table=table,
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Raises:
        ConfigurationError: If a value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    base_url = (_get("SELLERBOARD_BASE_URL", "https://app.sellerboard.com") or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigurationError(f"SELLERBOARD_BASE_URL must be an https URL, got {base_url!r}")

    marketplace = (_get("SELLERBOARD_MARKETPLACE", "walmart") or "walmart").lower()
    if not marketplace.isalnum():
        raise ConfigurationError(
            f"SELLERBOARD_MARKETPLACE must be a single path segment, got {marketplace!r}"
        )

    granularity = (_get("PERIOD_GRANULARITY", "day") or "day").lower()
    if granularity not in GRANULARITIES:
        raise ConfigurationError(
            f"PERIOD_GRANULARITY must be one of {', '.join(GRANULARITIES)}, got {granularity!r}"
        )

    backfill_start = _get_date("BACKFILL_START_DATE", date(2025, 1, 1))
    assert backfill_start is not None

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        base_url=base_url,
        marketplace=marketplace,
        email=_get("SELLERBOARD_EMAIL"),
        password=_get("SELLERBOARD_PASSWORD"),
        auto_login=_get_bool("SELLERBOARD_AUTO_LOGIN", True),
        backfill_start_date=backfill_start,
        granularity=granularity,
        include_today=_get_bool("INCLUDE_TODAY", False),
        refresh_recent_days=_get_int("REFRESH_RECENT_DAYS", 30, minimum=0),
        max_periods_per_run=_get_int("MAX_PERIODS_PER_RUN", 0, minimum=0),
        pause_between_downloads_seconds=_get_float(
            "PAUSE_BETWEEN_DOWNLOADS_SECONDS", 3.0, minimum=0.0
        ),
        max_consecutive_failures=_get_int("MAX_CONSECUTIVE_FAILURES", 5, minimum=1),
        report_timezone=_get_zone("REPORT_TIMEZONE", "America/Los_Angeles"),
        database=_database(marketplace, granularity),
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        state_dir=_get_path("STATE_DIR", data_dir / "state"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "sellerboard.lock"),
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        download_timeout_ms=_get_int("DOWNLOAD_TIMEOUT_MS", 180_000, minimum=1_000),
        page_settle_ms=_get_int("PAGE_SETTLE_MS", 3_000, minimum=0),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        max_retries=_get_int("MAX_RETRIES", 3, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 5.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 14, minimum=1),
        screenshot_retention_days=_get_int("SCREENSHOT_RETENTION_DAYS", 14, minimum=1),
    )

    if bool(settings.email) != bool(settings.password):
        missing = "SELLERBOARD_PASSWORD" if settings.email else "SELLERBOARD_EMAIL"
        raise ConfigurationError(
            f"{missing} is not set. Automatic sign-in needs both SELLERBOARD_EMAIL and "
            "SELLERBOARD_PASSWORD, or neither (session-only mode)."
        )
    return settings
