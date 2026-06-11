"""
POSIX file lock for cross-process MDI + VT Bulk Check quota coordination.

Mirrors MDI backend/services/quota_lock.py — keep behavior aligned.

Environment:
  VT_QUOTA_LOCK_FILE — override lock path (tests use tmp_path).
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, Optional

DEFAULT_LOCK_TIMEOUT = 5.0


class QuotaLockTimeout(Exception):
    """Raised when flock cannot be acquired within the timeout window."""

    code = "quota_lock_timeout"

    def __init__(self, message: str = "Quota lock acquisition timed out") -> None:
        super().__init__(message)
        self.message = message


def default_lock_path() -> Path:
    return Path(os.environ.get("VT_QUOTA_LOCK_FILE", "/var/lib/dns-tool/vt_usage.lock"))


def ensure_lock_file(lock_path: Path) -> None:
    """Create lock file if missing; best-effort mode 660."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        lock_path.touch()
    try:
        os.chmod(lock_path, 0o660)
    except OSError:
        pass


@contextmanager
def quota_lock(
    mode: Literal["shared", "exclusive"] = "exclusive",
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    lock_path: Optional[Path] = None,
) -> Iterator[None]:
    """
    Acquire a POSIX flock on the quota lock file.

    mode: "shared" (LOCK_SH) for consistent reads, "exclusive" (LOCK_EX) for writes.
    Raises QuotaLockTimeout if not acquired within timeout seconds.
    """
    path = lock_path or default_lock_path()
    ensure_lock_file(path)
    flock_mode = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, flock_mode | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise QuotaLockTimeout(
                        f"Could not acquire {mode} quota lock within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
