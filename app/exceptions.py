"""Exception hierarchy.

Every failure mode has its own type so callers can decide whether to retry,
skip a period, or stop the run.  ``transient`` marks errors that are safe to
retry; authentication failures never are -- repeating a rejected password is
how accounts get locked.
"""

from __future__ import annotations


class SellerboardError(Exception):
    """Base class for every failure in this project."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(SellerboardError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(SellerboardError):
    """Sellerboard session missing/expired, credentials rejected, or a
    verification step only a person can complete.  Never retried."""

    exit_code = 3


class ServiceUnavailableError(SellerboardError):
    """Sellerboard is in maintenance ("System maintenance ... try again in
    5-10 minutes").  Not the credentials' fault and not worth hammering: the
    run stops, keeps its progress, and the next run continues."""

    exit_code = 13


class NavigationError(SellerboardError):
    """The dashboard did not reach the expected state."""

    transient = True
    exit_code = 4


class DownloadError(SellerboardError):
    """The export did not start, did not finish, or produced no file."""

    transient = True
    exit_code = 6


class FileValidationError(SellerboardError):
    """The downloaded file is not a usable export (e.g. an HTML login page).

    Transient: the usual cause is a page that had not finished loading, and a
    fresh attempt normally fixes it.
    """

    transient = True
    exit_code = 7


class DataError(SellerboardError):
    """A downloaded export could not be parsed into rows."""

    exit_code = 8


class DatabaseError(SellerboardError):
    """MySQL connectivity or transaction failure.

    ``transient`` is set per instance: a dropped connection is retried, a
    schema or data error is not.
    """

    exit_code = 9

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PeriodsFailedError(SellerboardError):
    """The run finished, but one or more periods could not be downloaded."""

    exit_code = 12


class LockError(SellerboardError):
    """Another run already holds the execution lock."""

    exit_code = 11
