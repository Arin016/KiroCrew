"""Telegram command parsing.

Commands:
  /new         — start a fresh session (advances the generation counter)
  /compact     — trigger context compaction
  /link        — mirror this conversation's dashboard tab back here
  /unlink      — stop mirroring
  /help        — show available commands

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so existing
callers importing it from this module keep working).
"""

from __future__ import annotations

from kiro_claw.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_HELP_ALIASES = frozenset(("/help",))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'link', 'unlink', 'help', or None."""
    stripped = text.strip()
    # Telegram commands always start with /
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if cmd in _LINK_ALIASES:
        return "link"
    if cmd in _UNLINK_ALIASES:
        return "unlink"
    if cmd in _HELP_ALIASES:
        return "help"
    return None
