"""Persistent session-to-kiro-cli mapping.

Stores ``session_map.json`` mapping session keys to kiro-cli session IDs,
with Slack thread linkage for bidirectional sync.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from kiro_claw.config.paths import config_dir

logger = logging.getLogger(__name__)

_SESSION_MAP_FILE = "session_map.json"

# kiro-cli session file directory
_KIRO_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"


class SessionMap:
    """Persistent mapping of session_key → kiro-cli session ID.

    Stored as ``~/.kiroclaw/session_map.json``. Atomic write via tmp+rename.
    Only used for long-lived conversational sessions (Slack DM, dashboard).
    Stateless sessions (cron, subagent, taskrunner) are excluded.

    Each entry is a dict with keys: ``sid``, ``slack_thread_ts``, ``slack_channel_id``.
    A reverse index ``_thread_to_session`` maps Slack thread_ts → session_key
    for bidirectional sync lookups.
    """

    def __init__(self) -> None:
        self._path = config_dir() / _SESSION_MAP_FILE
        self._data: dict[str, dict] = {}  # key → {"sid", "slack_thread_ts", "slack_channel_id"}
        self._thread_to_session: dict[str, str] = {}  # slack_thread_ts → session_key
        self._load()

    def _load(self) -> None:
        self._thread_to_session.clear()
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._data = {}
                return
            if not isinstance(raw, dict):
                self._data = {}
                return
            migrated = False
            for key, val in raw.items():
                if isinstance(val, str):
                    # Backward compat: plain string → new dict format
                    self._data[key] = {
                        "sid": val,
                        "slack_thread_ts": None,
                        "slack_channel_id": None,
                    }
                    migrated = True
                elif isinstance(val, dict) and "sid" in val:
                    self._data[key] = val
                else:
                    continue  # skip corrupt entries
            self._rebuild_thread_index()
            if migrated:
                self._save()
        else:
            self._data = {}

    def _rebuild_thread_index(self) -> None:
        """Rebuild _thread_to_session from current _data."""
        self._thread_to_session.clear()
        for key, entry in self._data.items():
            ts = entry.get("slack_thread_ts")
            if ts:
                self._thread_to_session[ts] = key

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get(self, key: str) -> str | None:
        """Return kiro-cli session ID if mapping exists and .json file is present.

        Handles the dashboard history key round-trip: the original session key
        ``dashboard:chat-1-xxx`` becomes ``dashboard_chat-1-xxx`` on disk (via
        ``_safe_key``), and when resumed from history the slot name becomes
        ``dashboard_chat-1-xxx``, producing session key
        ``dashboard:dashboard_chat-1-xxx``.  We try the canonical form too.
        """
        entry = self._data.get(key)
        # Fallback: dashboard history round-trip (dashboard:dashboard_X → dashboard:X)
        matched_key = key
        if not entry and key.startswith("dashboard:dashboard_"):
            canonical = "dashboard:" + key[len("dashboard:dashboard_"):]
            entry = self._data.get(canonical)
            if entry:
                matched_key = canonical
        if not entry:
            return None
        sid = entry["sid"]
        if entry.get("provider") == "claude_code":
            return sid
        if sid and (_KIRO_SESSIONS_DIR / f"{sid}.json").exists():
            jsonl = _KIRO_SESSIONS_DIR / f"{sid}.jsonl"
            try:
                jsonl_size = jsonl.stat().st_size
            except FileNotFoundError:
                jsonl_size = 0
            if jsonl_size < 10:
                logger.info("Session %s has empty JSONL — pruning stale entry for %s", sid, key)
                self._remove_entry(matched_key)
                return None
            return sid
        if sid:
            self._remove_entry(matched_key)
        return None

    def _remove_entry(self, key: str) -> None:
        """Remove an entry and update reverse index."""
        entry = self._data.pop(key, None)
        if entry:
            ts = entry.get("slack_thread_ts")
            if ts and self._thread_to_session.get(ts) == key:
                del self._thread_to_session[ts]
            self._save()

    def set(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Save mapping and persist to disk, preserving existing slack fields."""
        existing = self._data.get(key)
        if existing:
            existing["sid"] = sid
            if provider:
                existing["provider"] = provider
            if cwd:
                existing["cwd"] = cwd
        else:
            entry: dict = {"sid": sid, "slack_thread_ts": None, "slack_channel_id": None}
            if provider:
                entry["provider"] = provider
            if cwd:
                entry["cwd"] = cwd
            self._data[key] = entry
        self._save()

    def get_cwd(self, key: str) -> str:
        """Return the stored CWD for *key*, or '' if not set."""
        entry = self._data.get(key)
        if not entry:
            return ""
        return entry.get("cwd", "")

    def get_provider(self, key: str) -> str:
        """Return the stored provider for *key* (e.g. 'acp', 'claude_code'), or ''."""
        entry = self._data.get(key)
        if not entry:
            return ""
        return entry.get("provider", "")

    def clear_sid(self, key: str) -> None:
        """Clear the stored session ID without removing the entry.

        Used on provider switch: the SID is incompatible with the new
        provider, but we keep the entry so Slack link and CWD persist.
        """
        entry = self._data.get(key)
        if entry and entry.get("sid"):
            entry["sid"] = ""
            self._save()

    def delete(self, key: str) -> None:
        """Remove mapping and persist."""
        self._remove_entry(key)

    def prune(self) -> int:
        """Remove entries whose session files no longer exist."""
        stale = [
            k
            for k, entry in self._data.items()
            if entry.get("provider") != "claude_code"
            and (
                (entry.get("sid") and not (_KIRO_SESSIONS_DIR / f"{entry['sid']}.json").exists())
                or (not entry.get("sid") and not entry.get("slack_thread_ts"))
            )
        ]
        for k in stale:
            del self._data[k]
        if stale:
            self._rebuild_thread_index()
            self._save()
            logger.info("Pruned %d stale session map entries", len(stale))
        return len(stale)

    def set_slack_link(self, key: str, thread_ts: str, channel_id: str | None) -> None:
        """Link a session to a Slack thread. Creates entry if needed."""
        entry = self._data.get(key)
        if entry:
            if (
                entry.get("slack_thread_ts") == thread_ts
                and entry.get("slack_channel_id") == channel_id
            ):
                self._thread_to_session.setdefault(thread_ts, key)
                return
            old_ts = entry.get("slack_thread_ts")
            if old_ts and old_ts != thread_ts:
                self._thread_to_session.pop(old_ts, None)
            entry["slack_thread_ts"] = thread_ts
            entry["slack_channel_id"] = channel_id
        else:
            self._data[key] = {
                "sid": "",
                "slack_thread_ts": thread_ts,
                "slack_channel_id": channel_id,
            }
        self._thread_to_session[thread_ts] = key
        self._save()

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        """Return (thread_ts, channel_id) for a session."""
        entry = self._data.get(key)
        if not entry:
            return None, None
        return entry.get("slack_thread_ts"), entry.get("slack_channel_id")

    def clear_slack_link(self, key: str) -> bool:
        """Remove the Slack link from a session, keeping the session itself.

        Clears only ``slack_thread_ts`` + ``slack_channel_id`` (preserves
        ``sid`` and the entry) and evicts the ``_thread_to_session`` reverse
        index so a later Slack reply in the old thread does not re-route to
        this session and silently re-engage mirroring. Returns True iff a link
        was present (only then is ``_save()`` called).
        """
        entry = self._data.get(key)
        if not entry:
            return False
        old_ts = entry.get("slack_thread_ts")
        had_link = bool(old_ts or entry.get("slack_channel_id"))
        if old_ts and self._thread_to_session.get(old_ts) == key:
            del self._thread_to_session[old_ts]
        entry.pop("slack_thread_ts", None)
        entry.pop("slack_channel_id", None)
        if had_link:
            self._save()
        return had_link

    def get_session_for_thread(self, thread_ts: str) -> str | None:
        """Return the session key linked to a Slack thread_ts, or None."""
        return self._thread_to_session.get(thread_ts)

    def find_key_by_sid(self, session_id: str) -> str | None:
        """Find the session map key for a given kiro-cli session ID."""
        for k, entry in self._data.items():
            sid = entry.get("sid") if isinstance(entry, dict) else entry
            if sid == session_id:
                return k
        return None
