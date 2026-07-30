"""Per-session queue of pending browser input events (dashboard → proxy).

The frame path is push (proxy POSTs, gateway broadcasts over the existing WS).
The input path cannot mirror that shape: the proxy binds no socket and the
gateway holds no handle back to it, and giving the proxy an inbound listener
would add a new loopback control surface inside the one process whose whole
design premise is "no inbound control" (see ``screencast.py`` on why the CDP
debug port was dropped).

So input is **pulled**: the panel POSTs a gesture, it lands here, and the proxy
long-polls for it. The gateway stays the only trust boundary.

Two properties this structure is responsible for:

* **Bounded** — a panel can generate gestures far faster than the browser can
  consume them (mousemove, wheel). The queue drops the OLDEST event when full,
  because for pointer input the newest position is the truthful one.
* **Expiring** — an event that was queued while nobody was draining must not
  fire minutes later when a browse session starts. Stale events are discarded on
  drain, so closing the panel cannot leave a delayed click armed.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

# Per-session cap. Comfortably above one rAF-coalesced gesture burst, far below
# anything that could pin memory.
_DEFAULT_MAXLEN = 64
# An input event older than this is discarded rather than injected. Input is only
# meaningful against the frame the user was looking at; a second-old click is
# already questionable, and a minute-old one is a bug.
_DEFAULT_TTL_S = 5.0


class BrowserInputQueue:
    """Bounded, TTL-expiring input queues keyed by browse session."""

    def __init__(self, maxlen: int = _DEFAULT_MAXLEN, ttl: float = _DEFAULT_TTL_S):
        self._maxlen = maxlen
        self._ttl = ttl
        self._queues: dict[str, deque[tuple[float, dict[str, Any]]]] = {}
        self._waiters: dict[str, asyncio.Event] = {}

    def _waiter(self, session_key: str) -> asyncio.Event:
        ev = self._waiters.get(session_key)
        if ev is None:
            ev = asyncio.Event()
            self._waiters[session_key] = ev
        return ev

    def push(self, session_key: str, event: dict[str, Any]) -> None:
        """Enqueue one validated event and wake any waiting drainer."""
        q = self._queues.get(session_key)
        if q is None:
            # maxlen makes the drop-oldest behaviour the deque's own invariant.
            q = deque(maxlen=self._maxlen)
            self._queues[session_key] = q
        q.append((time.monotonic(), event))
        self._waiter(session_key).set()

    def _take_fresh(self, session_key: str) -> list[dict[str, Any]]:
        """Pop everything queued, dropping anything past its TTL."""
        q = self._queues.get(session_key)
        if not q:
            return []
        now = time.monotonic()
        fresh = [ev for (queued_at, ev) in q if (now - queued_at) <= self._ttl]
        q.clear()
        return fresh

    async def drain(self, session_key: str, timeout: float) -> list[dict[str, Any]]:
        """Return queued events, waiting up to ``timeout`` for the first one.

        Long-poll rather than fixed-interval polling: an idle browse session costs
        one hanging request instead of a busy loop, and a click is delivered as
        soon as it arrives rather than on the next tick.
        """
        pending = self._take_fresh(session_key)
        if pending:
            return pending
        waiter = self._waiter(session_key)
        waiter.clear()
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._forget_if_idle(session_key)
            return []
        return self._take_fresh(session_key)

    def _forget_if_idle(self, session_key: str) -> None:
        """Drop bookkeeping for a session that timed out with nothing queued.

        Without this, one deque and one Event accumulate per session key for the
        gateway's lifetime. Safe against a racing ``push``: both run on the single
        event loop thread and there is no await between the emptiness check and the
        removal, so a pushed event cannot be dropped here.
        """
        q = self._queues.get(session_key)
        if q is not None and len(q) > 0:
            return
        self._queues.pop(session_key, None)
        self._waiters.pop(session_key, None)

    def tracked_sessions(self) -> int:
        """Number of sessions holding bookkeeping, for tests and diagnostics."""
        return len(self._queues)

    def pending_count(self, session_key: str) -> int:
        """Queued event count, for tests and diagnostics."""
        q = self._queues.get(session_key)
        return len(q) if q else 0
