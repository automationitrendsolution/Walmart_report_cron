#!/usr/bin/env python3
"""Sellerboard Walmart export -- command-line entry point.

    python run.py                  # scheduled run: refresh recent days, continue backfill
    python run.py --plan           # list what would be downloaded; no browser
    python run.py --login          # one-time sign-in in a visible browser
    python run.py --inspect        # list the dashboard's buttons (selector setup)
    python run.py --limit 1        # download just the newest outstanding period
    python run.py --load-only      # load downloaded files into MySQL; no browser

The scheduler calls ``python run.py`` with no flags, from the project
directory so ``.env`` is found.  Overlapping runs are refused by a lock.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from app import __version__
from app.config import load_settings
from app.exceptions import AuthenticationError, ConfigurationError, SellerboardError, ServiceUnavailableError
from app.logging_config import configure_logging, get_logger
from app.main import Pipeline, RunMode, RunOptions

_EPILOG = """
exit codes:
  0 success · 2 config · 3 authentication (run --login) · 4 navigation
  6 download · 7 bad file · 8 unreadable export · 9 database
  11 another run holds the lock
  12 some periods failed (they are retried on the next run)
  13 Sellerboard under maintenance (run again later)
"""


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sellerboard-walmart",
        description="Download Sellerboard Walmart dashboard exports, one file per period, newest first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Show outstanding periods and exit.")
    modes.add_argument("--login", action="store_true", help="Interactive sign-in in a visible browser.")
    modes.add_argument("--inspect", action="store_true", help="List the dashboard's clickable controls.")
    modes.add_argument("--load-only", action="store_true",
                       help="Load already-downloaded files into MySQL (with --force: reload all). No browser.")
    parser.add_argument("--from", dest="date_from", type=_parse_date, metavar="YYYY-MM-DD",
                        help="Oldest day for this run (default BACKFILL_START_DATE).")
    parser.add_argument("--to", dest="date_to", type=_parse_date, metavar="YYYY-MM-DD",
                        help="Newest day for this run (default yesterday).")
    parser.add_argument("--day", type=_parse_date, metavar="YYYY-MM-DD",
                        help="Just this one period (shorthand for --from D --to D --force).")
    parser.add_argument("--force", action="store_true", help="Re-download periods already in the manifest.")
    parser.add_argument("--limit", type=int, metavar="N", help="Download at most N periods (overrides MAX_PERIODS_PER_RUN).")
    parser.add_argument("--no-db", action="store_true", help="Download files only; skip MySQL this run.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--no-lock", action="store_true", help="Skip the single-instance lock.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = (
        RunMode.PLAN if args.plan
        else RunMode.LOGIN if args.login
        else RunMode.INSPECT if args.inspect
        else RunMode.LOAD_ONLY if args.load_only
        else RunMode.RUN
    )
    if args.day:
        args.date_from = args.date_to = args.day
        args.force = True
    if args.limit is not None and args.limit < 1:
        print("--limit must be >= 1", file=sys.stderr)
        return 2

    if args.no_db:
        os.environ["DB_ENABLED"] = "false"  # MySQL settings not needed for this run
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(settings.log_dir, level=args.log_level or settings.log_level,
                      retention_days=settings.log_retention_days)
    logger = get_logger("cli")

    if args.date_from and args.date_to and args.date_from > args.date_to:
        logger.error("--from must not be after --to")
        return 2

    options = RunOptions(
        mode=mode,
        headed=args.headed,
        force=args.force,
        limit=args.limit,
        date_from=args.date_from,
        date_to=args.date_to,
        use_lock=not args.no_lock,
        no_db=args.no_db,
    )
    try:
        Pipeline(settings, options).run()
    except SellerboardError as exc:
        if isinstance(exc, ConfigurationError):
            return exc.exit_code
        if isinstance(exc, ServiceUnavailableError):
            logger.error("Sellerboard is under maintenance. Nothing is lost; run again in 10-15 minutes.")
        if isinstance(exc, AuthenticationError):
            logger.error("Sign-in needs a person: run 'python run.py --login' once, then re-run.")
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Stopped by you (Ctrl+C). Everything saved so far is kept.")
        return 130
    except Exception:  # already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
