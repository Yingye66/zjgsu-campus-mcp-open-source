"""Reentrant authentication-file lock shared by threads and independent processes.

Only hold this during local reads/conditional commits; never during HTTP login.
The lock file must remain in place, since unlinking it can split the lock domain.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time

_REGISTRY_GUARD = threading.Lock()
_REGISTRY: dict[str, tuple[threading.RLock, threading.local]] = {}


@contextmanager
def authentication_lock(data_dir: Path):
    """Serialize authentication-file access for this data directory (10s timeout)."""
    directory = Path(data_dir).resolve()
    key = os.path.normcase(str(directory))
    with _REGISTRY_GUARD:
        local_lock, state = _REGISTRY.setdefault(key, (threading.RLock(), threading.local()))
    with local_lock:
        if getattr(state, "depth", 0):
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return

        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".auth.lock").open("a+b", buffering=0) as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
            if os.name == "nt":
                import msvcrt

                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

            deadline = time.monotonic() + 10
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Authentication storage is busy; retry later") from None
                    time.sleep(0.02)
            state.depth = 1
            try:
                yield
            finally:
                state.depth = 0
                release()
