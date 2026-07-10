"""WeChat command parsing and per-user conversation state.

Commands:
  /new (or 新对话 / 清空)  — start a fresh session (bumps gen counter)
  /compact               — trigger context compaction

ConversationState wraps a simple dict persisted nowhere special — the handler
owns it in memory. gen (int) rotates the session_key; awaiting_compact (bool)
tracks whether the soft-threshold prompt was sent this turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_claw.messaging.link import should_rotate_generation

logger = logging.getLogger(__name__)

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "新对话", "清空"))
_COMPACT_ALIASES = frozenset(("/compact",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', or None depending on leading command word."""
    stripped = text.strip()
    lower = stripped.lower()
    if lower in _NEW_ALIASES or stripped in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES:
        return "compact"
    return None


# ── Per-user conversation state ──


@dataclass
class _UserState:
    gen: int = 0
    awaiting_compact: bool = False
    last_active: float = 0.0


@dataclass
class ConversationState:
    """In-memory per-userid generation counter and awaiting-compact flag."""

    _state: dict[str, _UserState] = field(default_factory=dict)

    def _get(self, userid: str) -> _UserState:
        if userid not in self._state:
            self._state[userid] = _UserState()
        return self._state[userid]

    def bump_gen(self, userid: str) -> int:
        """Increment generation and return the new value."""
        s = self._get(userid)
        s.gen += 1
        s.awaiting_compact = False
        return s.gen

    def maybe_rotate(
        self, userid: str, now: float, *, idle_minutes: int = 0, daily_reset_hour: int = -1
    ) -> bool:
        """Rotate the generation on an idle/daily boundary, then record activity."""
        s = self._get(userid)
        rotate = should_rotate_generation(
            s.last_active, now, idle_minutes=idle_minutes, daily_reset_hour=daily_reset_hour
        )
        if rotate:
            s.gen += 1
            s.awaiting_compact = False
        s.last_active = now
        return rotate

    def current_gen(self, userid: str) -> int:
        return self._get(userid).gen

    def set_awaiting(self, userid: str) -> None:
        self._get(userid).awaiting_compact = True

    def clear_awaiting(self, userid: str) -> None:
        self._get(userid).awaiting_compact = False

    def is_awaiting(self, userid: str) -> bool:
        return self._get(userid).awaiting_compact
