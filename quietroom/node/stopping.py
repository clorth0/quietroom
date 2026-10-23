"""Graceful stop on SIGTERM / SIGINT for the node daemon.

The handler only records the signal and writes a byte to a self-pipe; the
daemon loop notices between segments and between ticks. Nothing is raised
into the middle of a sweep or a SQLite transaction, and waiting with
``select()`` on the pipe cannot deadlock the way calling ``threading.Event.set``
from a signal handler can (the handler may interrupt the same thread inside
``Event.wait`` while it holds the event's lock).
"""

from __future__ import annotations

import os
import select
import signal
from collections.abc import Iterable
from typing import Any

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Stopper:
    """A stop flag that signal handlers can set; ``is_set``/``wait`` like ``threading.Event``."""

    def __init__(self) -> None:
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        os.set_blocking(self._w, False)
        self.signame: str | None = None
        self._previous: dict[int, Any] = {}

    def set(self, signame: str = "stop") -> None:
        if self.signame is None:
            self.signame = signame
        try:
            os.write(self._w, b"\0")
        except (BlockingIOError, OSError):
            pass  # the pipe already holds a wake-up byte (or is closed)

    def is_set(self) -> bool:
        return self.signame is not None

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep until stopped or ``timeout`` seconds pass; True if stopped."""
        if self.is_set():
            return True
        try:
            select.select([self._r], [], [], timeout)
        except InterruptedError:  # pragma: no cover - PEP 475 retries select()
            pass
        return self.is_set()

    def _handle(self, signum: int, frame: Any) -> None:
        self.set(signal.Signals(signum).name)

    def install(self, signals: Iterable[int] = STOP_SIGNALS) -> None:
        for s in signals:
            self._previous[s] = signal.signal(s, self._handle)

    def restore(self) -> None:
        """Default handling again: a second signal during shutdown stops at once."""
        for s in list(self._previous):
            signal.signal(s, signal.SIG_DFL)
            del self._previous[s]

    def close(self) -> None:
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass
