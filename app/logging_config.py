"""Logging setup.

Two sinks: a human-readable console stream and a rotating file under
``LOG_DIR``.  A redaction filter is installed on both so that a stray
``repr()`` of a DSN or a cookie jar can never reach disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

LOGGER_NAME = "sellerboard"

_CONSOLE_FORMAT = "%(asctime)s  %(message)s"
_CONSOLE_DATEFMT = "%H:%M:%S"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(process)d] %(module)s:%(lineno)d | %(message)s"

#: Patterns scrubbed from every log record.  Each must keep group 1 as the
#: literal prefix so the replacement stays readable.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"), r"\1***\2"),
    (
        re.compile(
            r"((?:password|passwd|pwd|secret|token|cookie|session[_-]?id|api[_-]?key|authorization)"
            r"\s*[=:]\s*)(?!\*)[^\s,;)'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # Token-bearing URL query parameters, e.g. an SSO response.
    (
        re.compile(
            r"([?&](?:ssoResponse|access_token|id_token|refresh_token|code|sig|signature)=)"
            r"[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


class RedactingFilter(logging.Filter):
    """Masks credential-shaped substrings in messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact(a) for a in record.args)
        return True


class ConsoleFormatter(logging.Formatter):
    """One readable line per record: time, a WARNING/ERROR tag when needed,
    and the first line of the message.  Tracebacks and Playwright call logs
    are left to the log file."""

    _TAGS = {logging.WARNING: "WARNING: ", logging.ERROR: "ERROR: ", logging.CRITICAL: "ERROR: "}

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().splitlines()[0] if record.getMessage() else ""
        if record.exc_info and not message:
            message = short_error(record.exc_info[1])
        tag = self._TAGS.get(record.levelno, "")
        return f"{self.formatTime(record, self.datefmt)}  {tag}{message}"


def short_error(exc: BaseException | None, limit: int = 160) -> str:
    """A one-line, plain-words version of ``exc`` for the console."""
    if exc is None:
        return ""
    text = str(exc)
    if "element is not enabled" in text:
        return "the export button stayed disabled"
    first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    first = re.sub(r"^\w+\.\w+: ", "", first)  # "Locator.click: Timeout..." -> "Timeout..."
    if "Timeout" in first and "exceeded" in first:
        seconds = re.search(r"Timeout (\d+)ms", first)
        first = f"timed out after {int(seconds.group(1)) // 1000}s" if seconds else "timed out"
    if "Target page, context or browser has been closed" in first:
        first = "the browser window was closed"
    return first if len(first) <= limit else first[: limit - 3] + "..."


def _redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    run_id: str | None = None,
    retention_days: int = 7,
    console: bool = True,
) -> logging.Logger:
    """Configure and return the ``sellerboard`` logger.

    Safe to call more than once; existing handlers are replaced so repeated
    calls (tests, re-entry) do not duplicate output.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    # The logger passes everything; each handler picks its own level, so the
    # file keeps the step-by-step detail the console leaves out.
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(level)
        stream.setFormatter(ConsoleFormatter(_CONSOLE_FORMAT, _CONSOLE_DATEFMT))
        stream.addFilter(redactor)
        logger.addHandler(stream)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "cron.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    # Third-party libraries are noisy at DEBUG and can echo connection strings.
    for noisy in ("sqlalchemy.engine", "mysql.connector", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if run_id:
        logger.debug("run_id=%s", run_id)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the ``sellerboard`` logger."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
