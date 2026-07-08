"""Layer 3 -- namespaced channel linkage.

Session keys are namespaced as ``f"{channel_type}:{conversation_id}"`` so
keys never collide across channels. Legacy native-Slack sessions were keyed
by the bare ``thread_ts``; the helpers here provide the bidirectional
``bare <-> slack:`` shim used by ``SessionMap``.

Stdlib-only; imported by ``session_map`` (no import cycle).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Slack ts format: ``"{epoch_seconds}.{microseconds}"`` -- pure digits + one dot.
_SLACK_TS_RE = re.compile(r"\d+\.\d+")

SLACK_NAMESPACE = "slack"


@dataclass
class ChannelLink:
    """The inbound channel a session belongs to (its OWN channel).

    Distinct from the dashboard->Slack *mirror* binding, which stays behind
    ``SessionMap.get/set_slack_link`` and is NOT modeled here (guardrail G3).
    """

    channel_type: str
    channel_id: str | None = None
    thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChannelLink":
        return cls(
            channel_type=d.get("channel_type", ""),
            channel_id=d.get("channel_id"),
            thread_id=d.get("thread_id"),
        )


def session_key(channel_type: str, conversation_id: str) -> str:
    """Build a namespaced session key, e.g. ``slack:123.456``."""
    return f"{channel_type}:{conversation_id}"


def is_legacy_slack_key(key: str) -> bool:
    """True iff ``key`` is a bare Slack ``thread_ts`` (un-namespaced)."""
    return bool(_SLACK_TS_RE.fullmatch(key))


def canonical_key(key: str) -> str:
    """Normalize a legacy bare Slack ``thread_ts`` key to ``slack:<thread>``.

    Non-legacy keys (``dashboard:``, ``channel:``, ``slack:``, ...) pass
    through unchanged.
    """
    if is_legacy_slack_key(key):
        return f"{SLACK_NAMESPACE}:{key}"
    return key


def legacy_key(key: str) -> str | None:
    """Return the bare ``thread_ts`` for a ``slack:<thread>`` key, else None."""
    prefix = f"{SLACK_NAMESPACE}:"
    if key.startswith(prefix):
        rest = key[len(prefix):]
        if is_legacy_slack_key(rest):
            return rest
    return None
