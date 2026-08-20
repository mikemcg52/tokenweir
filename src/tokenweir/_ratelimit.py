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

**The window is per reason, not per warner.** This is the whole of the difference
between rate-limiting a repeating failure and silencing a fleet of distinct ones.
A single window shared across reasons has two failure modes, both of them worse
than the noise it was meant to suppress: a genuinely new failure mode stays
invisible for a whole interval because an unrelated one warned first, and — worse
— the suppressed count is then reported against whichever message happens to get
through next, so an operator reads "the sink failed, 3 further occurrences
suppressed" about three events that were producer-side type errors. A count
attached to the wrong cause is not a smaller truth than silence; it is a
falsehood. Reasons are keyed by their message text, which is why every caller
passes a module-level constant rather than an interpolated string.

Private by module name. This is not API; it exists so that
:mod:`tokenweir.emitter` and :mod:`tokenweir.sink` share one implementation
rather than reaching into each other's internals for it.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional


class RateLimitedWarner:
    """Log at ``WARNING``, at most once per ``interval``, counting the rest.

    Each distinct ``key`` gets its own window and its own suppressed count, so one
    chatty failure cannot mask or misreport another. See the module docstring for
    why that separation is the point rather than a refinement.

    Thread-safe: the emit-side callers warn from a producer thread (a full buffer
    is noticed by whoever is emitting) and from a delivery worker (a sink failure
    is noticed there), and those are different threads by construction.

    Args:
        logger: where to log. Held, not named, so a caller keeps its own logger's
            identity in the output.
        interval: seconds between lines that get through, per key. ``0`` disables
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
        self._next_allowed: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}

    def suppressed(self, key: Optional[str] = None) -> int:
        """Warnings currently held back — for one key, or across all of them."""
        with self._lock:
            if key is None:
                return sum(self._suppressed.values())
            return self._suppressed.get(key, 0)

    def warn(
        self, message: str, *, key: Optional[str] = None, exc_info: bool = True
    ) -> None:
        """Log ``message``, or count it as suppressed against its key. Never raises.

        Args:
            message: the line to log.
            key: what counts as "the same warning" for rate-limiting. Defaults to
                ``message`` itself, which is the right answer when callers pass
                module-level constants — as every caller in this package does.
                Pass it explicitly when a message carries interpolated detail that
                would otherwise make every occurrence look distinct and defeat the
                limiting entirely.
            exc_info: as :meth:`logging.Logger.warning`.

        The nested guard around the logging call is deliberate and is the same one
        :func:`tokenweir.sink._warn_dropped` carries: an application may install a
        handler or filter that raises, and "the emit path never raises" has to
        survive a hostile logging configuration, or the guard has merely moved the
        throw site onto the logger.
        """
        try:
            bucket = message if key is None else key
            now = self._clock()
            with self._lock:
                next_allowed = self._next_allowed.get(bucket)
                if next_allowed is not None and now < next_allowed:
                    self._suppressed[bucket] = self._suppressed.get(bucket, 0) + 1
                    return
                suppressed = self._suppressed.pop(bucket, 0)
                if self._interval:
                    self._next_allowed[bucket] = now + self._interval
            if suppressed:
                message = (
                    f"{message} [{suppressed} further occurrence(s) suppressed in the "
                    f"last {self._interval:g}s]"
                )
            self._logger.warning(message, exc_info=exc_info)  # type: ignore[attr-defined]
        except Exception:
            pass
