"""Single-instance execution lock, on Linux and Windows.

An OS-level lock on a dedicated file: the OS releases it if the process dies,
so a crashed run cannot wedge the schedule the way a stale PID file would.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .exceptions import LockError
from .logging_config import get_logger

logger = get_logger("lock")

if sys.platform == "win32":  # pragma: no cover - platform specific
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - platform specific
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def run_lock(lock_file: Path, *, enabled: bool = True) -> Iterator[None]:
    """Hold an exclusive lock for the duration of the block.

    Raises:
        LockError: If another run already holds the lock.
    """
    if not enabled:
        yield
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _try_lock(fd)
        except OSError as exc:
            raise LockError(
                f"Another run is in progress (lock {lock_file}). Refusing to start a second one."
            ) from exc
        # The PID goes after the locked first byte: on Windows a locked byte
        # cannot be written, even by its owner through another handle.
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        logger.debug("Acquired run lock %s", lock_file)
        try:
            yield
        finally:
            _unlock(fd)
            logger.debug("Released run lock %s", lock_file)
    finally:
        os.close(fd)
