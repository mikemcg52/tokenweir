"""Rate-limited warning — private infrastructure shared by the emit-side modules.

A metering guard that logs every drop is correct until the thing it is guarding
fails *systematically*. A broker that is down does not drop one record; it drops
every record, and a ``WARNING`` per drop is one log line per metered request —
the same "the library takes an output decision that belongs to the application"
problem that put the ``NullHandler`` in :mod:`tokenweir` in the first place, and
worse, because a log pipeline is a shared, finite resource.

So: the first occurrence in a window is logged immediately — a failure must never
be invisible for a whole interval before anyone hears about it — and further
occurrences inside that window are counted rather than logged, with the count
reported on the next line that gets through. Nothing is hidden: the suppressed
total is in the message, and the caller's own counters (which is what an operator
should alert on) are unaffected either way.

Private by module name. This is not API; it exists so that
:mod:`tokenweir.emitter` and :mod:`tokenweir.sink` share one implementation
rather than reaching into each other's internals for it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class RateLimitedWarner:
    """Log at ``WARNING``, at most once per ``interval``, counting the rest.

    Thread-safe: the emit-side callers warn from a producer thread (a full buffer
    is noticed by whoever is emitting) and from a delivery worker (a sink failure
    is noticed there), and those are different threads by construction.

    Args:
        logger: where to log. Held, not named, so a caller keeps its own logger's
            identity in the output.
        interval: seconds between lines that get through. ``0`` disables
            suppression entirely, which is what a test wants and what a
            low-volume caller may prefer.
        clock: monotonic seconds source. Injectable so the suppression window is
            testable without sleeping — a test that sleeps to prove a rate limit
            is a test that is slow *and* flaky.
    """

    def __init__(
        self,
        logger: object,
        *,
        interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._interval = max(0.0, float(interval))
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed: Optional[float] = None
        self._suppressed = 0

    @property
    def suppressed(self) -> int:
        """How many warnings are currently held back, for a caller that reports it."""
        with self._lock:
            return self._suppressed

    def warn(self, message: str, *, exc_info: bool = True) -> None:
        """Log ``message``, or count it as suppressed. Never raises.

        The nested guard around the logging call is deliberate and is the same one
        :func:`tokenweir.sink._warn_dropped` carries: an application may install a
        handler or filter that raises, and "the emit path never raises" has to
        survive a hostile logging configuration, or the guard has merely moved the
        throw site onto the logger.
        """
        try:
            now = self._clock()
            with self._lock:
                if self._next_allowed is not None and now < self._next_allowed:
                    self._suppressed += 1
                    return
                suppressed = self._suppressed
                self._suppressed = 0
                self._next_allowed = now + self._interval if self._interval else None
            if suppressed:
                message = (
                    f"{message} [{suppressed} further occurrence(s) suppressed in the "
                    f"last {self._interval:g}s]"
                )
            self._logger.warning(message, exc_info=exc_info)  # type: ignore[attr-defined]
        except Exception:
            pass
