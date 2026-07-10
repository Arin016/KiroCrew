"""Telegram command parsing and per-user conversation state.

Commands:
  /new         — start a fresh session (bumps gen counter)
  /compact     — trigger context compaction
  /help        — show available commands

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

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_HELP_ALIASES = frozenset(("/help",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'help', or None depending on leading command."""
    stripped = text.strip()
    # Telegram commands always start with /
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if cmd in _HELP_ALIASES:
        return "help"
    return None


# ── Per-user conversation state ──


@dataclass
class _UserState:
    gen: int = 0
    awaiting_compact: bool = False
    last_active: float = 0.0


@dataclass
class ConversationState:
    """In-memory per-user_id generation counter and awaiting-compact flag."""

    _state: dict[int, _UserState] = field(default_factory=dict)

    def _get(self, user_id: int) -> _UserState:
        if user_id not in self._state:
            self._state[user_id] = _UserState()
        return self._state[user_id]

    def bump_gen(self, user_id: int) -> int:
        """Increment generation and return the new value."""
        s = self._get(user_id)
        s.gen += 1
        s.awaiting_compact = False
        return s.gen

    def maybe_rotate(
        self, user_id: int, now: float, *, idle_minutes: int = 0, daily_reset_hour: int = -1
    ) -> bool:
        """Rotate the generation on an idle/daily boundary, then record activity."""
        s = self._get(user_id)
        rotate = should_rotate_generation(
            s.last_active, now, idle_minutes=idle_minutes, daily_reset_hour=daily_reset_hour
        )
        if rotate:
            s.gen += 1
            s.awaiting_compact = False
        s.last_active = now
        return rotate

    def current_gen(self, user_id: int) -> int:
        return self._get(user_id).gen

    def set_awaiting(self, user_id: int) -> None:
        self._get(user_id).awaiting_compact = True

    def clear_awaiting(self, user_id: int) -> None:
        self._get(user_id).awaiting_compact = False

    def is_awaiting(self, user_id: int) -> bool:
        return self._get(user_id).awaiting_compact
