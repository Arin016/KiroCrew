"""Session persistence — save, restore, history prefix."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from kiro_claw import model_registry
from kiro_claw.agent import KIRO_AGENTS_DIR
from kiro_claw.atomic_write import atomic_write
from kiro_claw.config.loader import KiroClawConfig
from kiro_claw.dashboard.chat_utils import (
    _history_key_for,
    _normalize_model,
    _sync_dashboard_slots,
)
from kiro_claw.dashboard.state import DashboardState, _ChatSlot
from kiro_claw.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_claw.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)


def _redact_value(v):  # type: ignore[no-untyped-def]
    """Recursively redact any value (str, dict, list, or passthrough)."""
    if isinstance(v, str):
        v, _ = redact_exfiltration_urls(v)
        v, _ = redact_credentials(v)
        return v
    if isinstance(v, dict):
        return _redact_meta(v)
    if isinstance(v, list):
        return [_redact_value(i) for i in v]
    return v


def _redact_meta(meta: dict) -> dict:
    """Recursively redact string values in meta dict."""
    return {k: _redact_value(v) for k, v in meta.items()}


def _redact_meta_for_role(role: str, meta: dict) -> dict:
    """Redact meta, but preserve role-specific user-actionable external URLs (e.g. mcp_oauth)."""
    if role == "mcp_oauth":
        out: dict = {}
        for k, v in meta.items():
            if k == "oauth_url" and isinstance(v, str):
                # Two gates on rehydrate:
                #   1. http(s)-only — a tampered history line can't smuggle a
                #      javascript:/data: URL into <a href>.
                #   2. URL must not embed a credential or exfil-eligible host —
                #      a legit OAuth consent URL never carries credential
                #      patterns; presence of one means it's tampered/bogus.
                lower = v.lower()
                safe_scheme = lower.startswith("https://") or lower.startswith("http://")
                _, hit_cred = redact_credentials(v)
                _, hit_exfil = redact_exfiltration_urls(v)
                out[k] = v if (safe_scheme and not hit_cred and not hit_exfil) else ""
            else:
                out[k] = _redact_value(v)
        return out
    return _redact_meta(meta)


_MAX_HISTORY_CHARS = 8000

# Fallback effort levels — used when no ACP session has reported its config
# yet (cold start). Sourced from the shared ``effort.py`` vocabulary so every
# provider agrees on the levels (incl. "xhigh") and there is a single source of
# truth; ACP overrides these at runtime via update_reasoning_effort_values().
# Order matches natural escalation (low→max) for display purposes.
_REASONING_EFFORT_FALLBACK_ORDER: list[str] = list(EFFORT_LEVELS)
_REASONING_EFFORT_FALLBACK = EFFORT_VALUES

# Runtime state: validation set + ordered list (ACP order preserved).
# Persisted JSON is untrusted input — values flow into a subprocess CLI arg
# (Claude Code's --effort flag) and the ACP /effort slash command, so BSC1
# set-membership validation applies on the read path too, not just the API.
_reasoning_effort_values: set[str] = set(_REASONING_EFFORT_FALLBACK)
_reasoning_effort_ordered: list[str] = list(_REASONING_EFFORT_FALLBACK_ORDER)

# Re-exported (back-compat) for any caller importing the static allowlist.
_REASONING_EFFORT_VALUES = EFFORT_VALUES


def get_reasoning_effort_values() -> frozenset[str]:
    """Return currently valid effort levels (ACP-dynamic + fallback)."""
    return frozenset(_reasoning_effort_values)


def get_reasoning_effort_ordered() -> list[str]:
    """Return effort levels in ACP-reported order (excludes empty/default)."""
    return list(_reasoning_effort_ordered)


# Anchored with ``\Z`` (not ``$``) so a value with a trailing newline such as
# "low\n" is rejected — ``$`` would match before the newline and let it through
# to the persistence/subprocess boundary.
_SAFE_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,20}\Z")


def update_reasoning_effort_values(acp_levels: list[str]) -> None:
    """Update valid effort levels from ACP session config.

    Preserves ACP order for display. The validation set grows monotonically —
    it UNIONS the new levels onto the existing set (and the fallback) and never
    shrinks, so a level that a prior session reported (and that a slot may have
    persisted) stays valid even after another session reports a narrower config.

    Sanitizes input: only lowercase alphanumeric strings pass through
    (defense-in-depth for subprocess boundary).

    Note: ``_reasoning_effort_ordered`` is a process-global *fallback* display
    list only. The dropdown resolves levels per-slot from the slot's live ACP
    provider (see ``api_effort_levels``); this global is served only when no
    live provider is available.
    """
    global _reasoning_effort_values, _reasoning_effort_ordered
    safe_levels = [
        level for level in acp_levels if isinstance(level, str) and _SAFE_EFFORT_RE.match(level)
    ]
    level_set = set(safe_levels)
    # Union-only: never drop a previously-valid level (BSC1 persistence safety).
    merged = _reasoning_effort_values | set(_REASONING_EFFORT_FALLBACK) | level_set | {""}
    ordered = [level for level in safe_levels if level]
    if merged != _reasoning_effort_values or ordered != _reasoning_effort_ordered:
        logger.info("Effort levels updated from ACP: %s", ordered)
        _reasoning_effort_values = merged
        _reasoning_effort_ordered = ordered


def _validate_reasoning_effort(raw: object) -> str:
    """Return *raw* if it's a valid reasoning_effort string, else "".

    Used by the persistence restore paths so a tampered/corrupted
    metadata file cannot smuggle an arbitrary string into the CC
    ``--effort`` subprocess argument.
    """
    if isinstance(raw, str) and raw in _reasoning_effort_values:
        return raw
    if raw:
        logger.warning("Discarding invalid persisted reasoning_effort: %r", raw)
    return ""


def save_all_slots_to_history(state: DashboardState) -> None:
    """Save all active slots to history. Called on gateway shutdown."""
    for slot in list(state._slots.values()):
        try:
            _save_slot_to_history(state, slot, force=True)
        except Exception:
            logger.error("Shutdown: failed to save slot %s", slot.key, exc_info=True)


def _attach_variants(slot: _ChatSlot, m: dict) -> None:
    """Copy variant history from a persisted message onto the slot's last message, with redaction."""
    if m.get("variants"):
        slot.messages[-1]["variants"] = [  # type: ignore[assignment]
            {
                **v,
                "content": redact_credentials(redact_exfiltration_urls(v.get("content", ""))[0])[0],
            }
            for v in m["variants"]
            if isinstance(v, dict)
        ]
        slot.messages[-1]["variant_idx"] = m.get("variant_idx", 0)


def _rehydrate_slot_from_history(state: DashboardState, slot_name: str) -> _ChatSlot | None:
    """Rehydrate a single dashboard slot from persisted history.

    Unlike ``state.get_or_create_slot`` (which creates a fresh, empty slot with
    default ``memory_mode='persistent'``), this helper reads the session's
    metadata and messages from ``conversation_log`` so the restored slot has
    the original title/agent/model/memory_mode and its message history
    populated. Returns ``None`` if the session does not exist on disk (so
    callers can fall through to other delivery paths without creating a
    phantom empty tab).

    Intended for targeted resume paths (e.g. cron→origin injection after
    gateway restart). Bulk startup restore still uses ``restore_recent_sessions``.
    """
    if not state.conversation_log:
        return None
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = _history_key_for(slot_name)
    meta = state.conversation_log.get_metadata(history_key)
    # No metadata → session was never persisted. Don't create a phantom slot.
    if not meta:
        return None
    if meta.get("closed"):
        return None
    try:
        _restore_cfg = KiroClawConfig.load()
    except Exception:
        _restore_cfg = None
    # Build the same kiro-agent model map as restore_recent_sessions so
    # legacy sessions without persisted `model` still resolve correctly.
    kiro_model_map: dict[str, str] = {}
    try:
        for f in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    kiro_model_map[data["name"]] = model
                kiro_model_map[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
    slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
    # Pull display fields from session listing for title parity with bulk restore.
    sessions = state.conversation_log.list_sessions()
    session_info = next(
        (s for s in sessions if s.get("key") == history_key),
        {},
    )
    # Titles may have been auto-generated by an LLM (_generate_title_via_kiro)
    # and are surfaced on the dashboard, so apply the same redaction passes
    # used on assistant content before setting. Defence-in-depth — the title
    # author is trusted-ish (our own kiro process), but the generation input
    # is user content, so a prompt injection could craft a title with an
    # exfiltration URL or leaked credential.
    raw_title = session_info.get("title") or meta.get("title") or slot_name
    raw_title, _ = redact_exfiltration_urls(raw_title)
    raw_title, _ = redact_credentials(raw_title)
    slot.title = raw_title
    slot._titled = bool(session_info.get("title") or meta.get("title"))
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("agent"):
        slot.agent = meta["agent"]
    if meta.get("model"):
        # _normalize_model handles deprecation renames. For claude_code sessions,
        # also map a pre-migration raw provider id back to the canonical key so it
        # matches the canonical-keyed dropdown (no-op for other providers). Reuse
        # the already-loaded _restore_cfg provider — no second config load.
        _prov = _restore_cfg.agent.provider if _restore_cfg else ""
        slot.model = model_registry.canonicalize_for_provider(
            _normalize_model(meta["model"]), _prov
        )
    elif slot.agent:
        try:
            mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
            kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
            slot.model = kiro_model_map.get(kiro_name, "")
        except Exception:
            logger.debug("Failed to resolve model for rehydrated slot %s", slot_name, exc_info=True)
    if meta.get("reasoning_effort"):
        slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode"):
        slot.mode = meta["mode"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("app"):
        slot._app = meta["app"]
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{slot_name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    messages = state.conversation_log.read_messages(history_key)
    for m in messages[-200:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
        )
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._dirty = False
    logger.info("Rehydrated session %s (%s) from history", slot_name, slot.title)
    return slot


def restore_recent_sessions(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """Restore sessions as chat slots."""
    if not state.conversation_log:
        return 0
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    restored = 0

    kiro_model_map: dict[str, str] = {}
    try:

        for f in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    kiro_model_map[data["name"]] = model
                kiro_model_map[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
    try:
        _restore_cfg = KiroClawConfig.load()
    except Exception:
        _restore_cfg = None
    for s in state.conversation_log.list_sessions():
        key = s.get("key", "")
        if key.startswith("dashboard:"):
            slot_name = key.removeprefix("dashboard:")
        elif key.startswith("dashboard_"):
            slot_name = key.removeprefix("dashboard_")
        else:
            continue
        if slot_name in state._slots:
            continue
        meta = state.conversation_log.get_metadata(key)
        has_folder = bool(meta.get("folder_id"))
        has_pin = bool(meta.get("pinned"))
        if folders_only and not has_folder and not has_pin:
            continue
        if meta.get("closed"):
            continue
        if not has_folder and not has_pin:
            if cutoff is not None and s.get("modified", 0) < cutoff:
                continue
        slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
        # Titles can be LLM-generated (auto-title) and are surfaced on the
        # dashboard — apply the same redaction as assistant content. Matches
        # the treatment in _rehydrate_slot_from_history above.
        raw_title = s.get("title", slot_name)
        raw_title, _ = redact_exfiltration_urls(raw_title)
        raw_title, _ = redact_credentials(raw_title)
        slot.title = raw_title
        slot._titled = bool(s.get("title"))
        if meta.get("created_at"):
            slot.created_at = meta["created_at"]
        if meta.get("agent"):
            slot.agent = meta["agent"]
        if meta.get("model"):
            # Canonicalize a pre-migration claude_code provider id to the
            # canonical dropdown key (no-op for other providers); reuse the
            # already-loaded _restore_cfg provider.
            _prov = _restore_cfg.agent.provider if _restore_cfg else ""
            slot.model = model_registry.canonicalize_for_provider(
                _normalize_model(meta["model"]), _prov
            )
        elif slot.agent:
            try:
                mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
                kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
                slot.model = kiro_model_map.get(kiro_name, "")
            except Exception:
                logger.debug(
                    "Failed to resolve model for restored slot %s", slot_name, exc_info=True
                )
        if meta.get("reasoning_effort"):
            slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
        if meta.get("workspace"):
            slot.workspace = meta["workspace"]
        if meta.get("project"):
            slot.project = meta["project"]
        if meta.get("mode"):
            slot.mode = meta["mode"]
        if meta.get("folder_id"):
            slot.folder_id = meta["folder_id"]
        if meta.get("app"):
            slot._app = meta["app"]
        if meta.get("pinned"):
            slot.pinned = True
        if meta.get("color_index") is not None:
            slot.color_index = meta["color_index"]
        if meta.get("color_theme"):
            slot.color_theme = meta["color_theme"]
        raw_tags = meta.get("tags")
        if isinstance(raw_tags, list):
            slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
        mm = meta.get("memory_mode", "persistent")
        slot.memory_mode = mm
        if mm != "persistent":
            state._restricted_keys.add(f"dashboard:{slot_name}")
        if meta.get("forked_from") is not None:
            slot.forked_from = meta["forked_from"]
        tab_id = meta.get("tab_id")
        if not tab_id:
            tab_id = uuid.uuid4().hex[:12]
            state.conversation_log.update_metadata(key, {"tab_id": tab_id})
        slot._tab_id = tab_id
        messages = state.conversation_log.read_messages_chained(key)
        slot._disk_older_count = max(0, len(messages) - 500)
        for m in messages[-500:]:
            role = m.get("role", "assistant")
            cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
            content = m.get("content", "")
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            slot.append(
                role,
                content,
                cls,
                ts=m.get("ts", ""),
                meta=(
                    _redact_meta_for_role(role, m["meta"])
                    if isinstance(m.get("meta"), dict)
                    else None
                ),
            )
            _attach_variants(slot, m)
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False
        restored += 1
        logger.info("Restored session %s (%s)", slot_name, slot.title)
    _sync_dashboard_slots(state)
    return restored


def _save_slot_to_history(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    force: bool = False,
) -> None:
    """Persist slot messages to JSONL history."""
    msgs = messages if messages is not None else slot.messages
    if not state.conversation_log or not msgs:
        return
    if slot._resumed_count > 0 and len(msgs) <= slot._resumed_count:
        if not closed and not force:
            return
    history_key = _history_key_for(slot.key)
    try:
        existing_meta = state.conversation_log.get_metadata(history_key)

        path = state.conversation_log._path(history_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_line: dict = {
            "_type": "metadata",
            "created_at": existing_meta.get("created_at") or slot.created_at,
            "last_consolidated": existing_meta.get("last_consolidated", 0),
        }
        if closed:
            meta_line["closed"] = True
        meta_line["memory_mode"] = slot.memory_mode
        if slot.title and slot.title != slot.key:
            meta_line["title"] = slot.title
        if slot.agent:
            meta_line["agent"] = slot.agent
        meta_line["model"] = slot.model
        if slot.reasoning_effort:
            meta_line["reasoning_effort"] = slot.reasoning_effort
        if slot.mode:
            meta_line["mode"] = slot.mode
        if slot.workspace and slot.workspace != "default":
            meta_line["workspace"] = slot.workspace
        if slot.project:
            meta_line["project"] = slot.project
        if slot.folder_id:
            meta_line["folder_id"] = slot.folder_id
        if slot._app:
            meta_line["app"] = slot._app
        if slot.pinned:
            meta_line["pinned"] = True
        if slot.color_index is not None:
            meta_line["color_index"] = slot.color_index
        if slot.color_theme:
            meta_line["color_theme"] = slot.color_theme
        if slot.tags:
            meta_line["tags"] = list(slot.tags)
        if slot.forked_from is not None:
            meta_line["forked_from"] = slot.forked_from
        tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
        if tab_id:
            meta_line["tab_id"] = tab_id
        lines = [json.dumps(meta_line) + "\n"]
        for m in msgs:
            role = m.get("role", "assistant")
            if role in ("chunk", "done", "streaming", "queued", "permission"):
                continue
            content = m.get("content", "")
            if role not in ("user", "system"):
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            entry: dict = {
                "role": role,
                "content": content,
                "ts": m.get("ts", ""),
                "source_thread": "dashboard",
                "source_user": "dashboard",
            }
            if m.get("variants"):
                redacted_variants: list[dict] = []
                for v in m["variants"]:
                    if not isinstance(v, dict):
                        continue
                    vc = v.get("content", "")
                    vc, _ = redact_exfiltration_urls(vc)
                    vc, _ = redact_credentials(vc)
                    redacted_variants.append({**v, "content": vc})
                entry["variants"] = redacted_variants
                entry["variant_idx"] = m.get("variant_idx", 0)
            cls_val = m.get("cls", "")
            if role == "system" and cls_val:
                entry["cls"] = cls_val
            if isinstance(m.get("meta"), dict):
                entry["meta"] = _redact_meta_for_role(role, m["meta"])
            lines.append(json.dumps(entry) + "\n")

        atomic_write(path, "".join(lines), fsync=True)
        state.conversation_log._invalidate_cache(history_key)
        state.conversation_log.invalidate_tab_id_cache()
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


def _build_history_prefix(slot: _ChatSlot) -> str:
    """Build a condensed history prefix from slot messages for session re-injection."""
    lines: list[str] = []
    total = 0
    for m in slot.messages:
        role = m.get("role", "")
        if role in ("chunk", "done", "streaming", "queued", "permission", "error", "tool"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = m.get("content", "")[:500]
        line = f"{label}: {text}"
        if total + len(line) > _MAX_HISTORY_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "[Previous chat history for this tab — session was reset after stop]\n"
        + "\n".join(lines)
        + "\n[End of history]\n\n"
    )
