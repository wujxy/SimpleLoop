# Vendored from simpleloop/processes.py (S2b(ii)). The Host keeps its own copy for the loop's
# signal handling; the proposer uses only register/unregister here.
"""Run-scoped ownership of detached subprocess groups."""
from __future__ import annotations

import os
import signal
import threading
from contextlib import contextmanager


class ChildProcessRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._pgids: set[int] = set()

    def register(self, pgid: int) -> None:
        with self._lock:
            self._pgids.add(pgid)

    def unregister(self, pgid: int) -> None:
        with self._lock:
            self._pgids.discard(pgid)

    def terminate_all(self) -> None:
        with self._lock:
            pgids = tuple(self._pgids)
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.unregister(pgid)


CHILD_PROCESSES = ChildProcessRegistry()


@contextmanager
def run_signal_handlers():
    """Reap owned groups before interrupting the main run on SIGTERM."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signum, _frame):
        CHILD_PROCESSES.terminate_all()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
