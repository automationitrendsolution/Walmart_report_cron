import subprocess
import sys
import time

import pytest

from app.config import PROJECT_ROOT
from app.exceptions import LockError
from app.runlock import run_lock

_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.runlock import run_lock
with run_lock(Path(sys.argv[2])):
    print("held", flush=True)
    time.sleep(30)
"""


def test_second_process_is_refused_and_lock_freed_on_death(tmp_path):
    lock = tmp_path / "run.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(PROJECT_ROOT), str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(LockError):
            with run_lock(lock):
                pass
    finally:
        proc.kill()
        proc.wait()
    # Windows can take a moment to drop a dead process's byte-range lock.
    deadline = time.monotonic() + 5
    while True:
        try:
            with run_lock(lock):
                break
        except LockError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
