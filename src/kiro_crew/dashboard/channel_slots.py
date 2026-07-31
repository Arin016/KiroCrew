"""Surface channel-originated sessions in the dashboard's active chat list.

A conversation started on Slack (or Discord/Telegram/Teams/…) persists under a
channel-namespaced session key such as ``slack:<thread_ts>``. The dashboard's
slot restore paths only ever built slots for ``dashboard:``-prefixed keys, so
those conversations existed on disk but had no chat slot — they only appeared
in the sidebar's collapsed **History** pane, where the user had to search for
one and resume it by hand.

This module reconciles recent channel sessions INTO slots so they show up in
the active chat list, and binds each one as **one session with two surfaces**:
the same conversation, readable and repliable from either the dashboard tab or
the channel it started in.

Design notes:

* **One session, one transcript — the channel's.** The slot's
  ``shared_session_key`` is set to the channel session key, so the transcript is
  READ, WRITTEN and RUN under that single key (see
  :func:`~kiro_crew.dashboard.chat_utils.slot_transcript_key`). There is no
  second ``dashboard:<slot.key>`` file to diverge from, and therefore no
  mirroring or sync step: both surfaces append to the same file.

  This replaces an earlier design in which the slot owned its own transcript and
  was seeded with a COPY of the channel conversation. That copy was frozen at
  the moment of surfacing — a thread that stayed live on the channel left the tab
  showing only the turns that existed on the first reconcile pass, and a reply
  typed into the tab answered from that stale window and never reached the
  channel. The single-key binding is what removes both failures.

* **Inbound channel replies run the slot's turn.** A surfaced Slack thread is
  registered via :meth:`DashboardState.link_slack`, so
  ``slack.handler.maybe_route_linked_thread`` hands an inbound reply to this
  slot and runs it through the dashboard turn path instead of Slack's own. That
  keeps ONE running turn per conversation, guarded by the single per-session
  semaphore, and gives the channel the dashboard's streaming mirror for free.

* **The window live-follows until the user takes over.** While a surfaced slot is
  neither dirty nor running, every pass re-reads the shared transcript and
  refreshes the in-memory window and the frozen-prefix counters
  (:func:`resync_channel_slot`). This keeps the tab current for a conversation
  still moving on the channel, and — critically — keeps ``_disk_older_count``
  honest, since a stale count would make the next save rewrite the file from an
  out-of-date snapshot and drop turns appended in the meantime.

* **Recency-bounded.** Only sessions modified within the dashboard's configured
  ``restore_window_minutes`` become slots, so a long DM history does not turn
  into hundreds of tabs. Pinned and foldered sessions are exempt from the
  window, matching :func:`~kiro_crew.dashboard.chat_persistence.restore_recent_sessions`.
* **Closed is sticky.** A session the user closed on the dashboard
  (``meta.closed``) is never re-surfaced — otherwise closing the tab would be
  undone on the next reconcile pass. Because the slot now writes ``closed`` to
  the channel key, the two-key read below is load-bearing rather than defensive:
  it also honors the legacy ``dashboard:``-keyed flag written by the old design.
* **Ephemeral stays ephemeral.** ``incognito``/``temporary`` channel threads are
  skipped: the user asked for a conversation that leaves no trace, and a
  durable sidebar tab contradicts that.
* **Idempotent.** Every pass re-asserts the binding for sessions that already
  own a slot rather than creating a second one, so it is safe to run on a timer
  and self-heals after a gateway restart (the ``_slack_to_slot`` index is
  in-memory only).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.messaging.link import channel_namespace_of, is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How often the background reconciler runs. Not user-configurable: it only
#: bounds how stale the sidebar can be for a channel conversation started while
#: the dashboard is already open, and each pass is a cheap metadata scan.
RECONCILE_INTERVAL_SECS = 30

#: Memory modes whose sessions must never be surfaced as a durable slot.
_EPHEMERAL_MEMORY_MODES = frozenset({"incognito", "temporary"})

#: Human-facing label per channel namespace, used only when a session has no
#: title of its own yet (first turn still in flight).
_CHANNEL_LABELS: dict[str, str] = {
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "webex": "Webex",
    "wecom": "WeCom",
    "teams": "Teams",
    "weixin": "Weixin",
    "unified": "Direct message",
}

#: Messages hydrated into a newly surfaced slot. Matches the cron inject path.
_HYDRATE_LIMIT = 50


class _Transcript(NamedTuple):
    """A slot's seed transcript plus the shared file's line accounting.

    ``disk_older`` + ``disk_window`` always sum to the length of the CHANNEL
    transcript — the file a surfaced slot shares and writes. The merged
    ``messages`` may be longer because it also folds in turns left behind in a
    legacy ``dashboard:``-keyed file by the previous copy-based design.
    """

    messages: list[dict[str, Any]]
    disk_older: int
    disk_window: int


def channel_label(session_key: str) -> str:
    """Return the display label for *session_key*'s channel (``"Slack"``, …)."""
    ns = channel_namespace_of(session_key)
    return _CHANNEL_LABELS.get(ns, ns.capitalize() if ns else "Channel")


def channel_slot_name(session_key: str) -> str:
    """Return the dashboard slot key for a channel session key.

    Deterministic and idempotent — the same channel session always maps to the
    same slot, which is what makes repeat reconcile passes no-ops and lets the
    key itself carry the conversation's provenance.
    """
    return _normalize_slot_key(session_key)


def slot_history_key(session_key: str) -> str:
    """The LEGACY ``dashboard:``-keyed transcript for a channel session.

    Under the current single-session design a surfaced slot reads and writes the
    channel key itself, so this file is not written any more. It is still read on
    two paths: the ``closed`` flag a tab dismissed under the old design wrote
    here, and :func:`reconcile_channel_slots`' one-time merge of turns a
    dashboard reply left in this file before the migration.
    """
    return _history_key_for(channel_slot_name(session_key))


def channel_key_for_slot_name(slot_name: str, sessions: list[dict[str, Any]]) -> str:
    """The channel session key whose slot name is *slot_name*, or ``""``.

    An EXACT reverse lookup over *sessions* rather than a string transform:
    ``_normalize_slot_key`` folds every non-word char to ``_``, so a key with
    inner separators (``telegram:kirocrew:direct:9:gen3``) cannot be recovered
    from its slot name by substitution. Comparing forward-normalized candidates
    is lossless for every namespace, and cheap — the restore paths only reach
    this when a slot has no legacy transcript of its own.

    Accepts either key spelling ``list_sessions`` serves (``slack:1.1`` or the
    ``slack_1.1`` filename stem); both fold to the same slot name, and both
    resolve to the same transcript file via ``history._safe_key``.
    """
    if not slot_name:
        return ""
    for s in sessions:
        key = s.get("key", "")
        if key and is_channel_session_key(key) and channel_slot_name(key) == slot_name:
            return key
    return ""


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def eligible_channel_sessions(
    sessions: list[dict[str, Any]],
    *,
    metadata: dict[str, dict[str, Any]],
    cutoff: float | None,
) -> list[dict[str, Any]]:
    """Filter a ``list_sessions()`` result down to channel sessions to surface.

    *metadata* maps session key -> ``get_metadata()`` result, and must contain an
    entry for BOTH the channel key and its ``dashboard:``-prefixed slot key (see
    :func:`slot_history_key`) — closing a tab writes ``closed`` to the slot key,
    so reading only the channel key would let a closed tab reopen on the next
    pass. *cutoff* is a unix timestamp; sessions older than it are dropped unless
    pinned or foldered. ``None`` disables the recency filter (mirrors
    ``restore_window_minutes=0``).

    Pure and side-effect free so the eligibility rules are directly testable.
    """
    out: list[dict[str, Any]] = []
    for s in sessions:
        key = s.get("key", "")
        if not key or not is_channel_session_key(key):
            continue
        meta = metadata.get(key) or {}
        slot_meta = metadata.get(slot_history_key(key)) or {}
        # Either side having been closed means the user dismissed this
        # conversation; never resurface it.
        if meta.get("closed") or slot_meta.get("closed"):
            continue
        modes = (
            str(meta.get("memory_mode", "")).lower(),
            str(slot_meta.get("memory_mode", "")).lower(),
            str(s.get("memory_mode", "")).lower(),
        )
        if any(m in _EPHEMERAL_MEMORY_MODES for m in modes):
            continue
        exempt = (
            bool(meta.get("pinned"))
            or bool(meta.get("folder_id"))
            or bool(slot_meta.get("pinned"))
            or bool(slot_meta.get("folder_id"))
        )
        if not exempt and cutoff is not None and float(s.get("modified", 0) or 0) < cutoff:
            continue
        out.append(s)
    return out


def _identity(msg: dict[str, Any]) -> tuple[str, str, str]:
    """Match/dedupe identity for one transcript message.

    Used both to drop the channel turns already copied into the slot and to
    locate a slot's on-disk lines inside the hydrate window.
    """
    return (
        str(msg.get("role", "")),
        str(msg.get("content", "")),
        str(msg.get("ts", "")),
    )


def _ts_sort_key(msg: dict[str, Any]) -> tuple[int, float, str]:
    """Chronological sort key for one transcript message.

    The two sides of a merge are written by different code paths and nothing
    makes them agree on timezone: the dashboard writes an ISO-8601 UTC string,
    while a channel turn can land as a naive local-time one. Comparing those
    lexicographically interleaves them wrongly by the host's UTC offset on any
    machine that is not on UTC, so parse to an absolute instant first.

    Buckets keep the fallbacks out of the ordered region: ``0`` parseable
    (ordered by epoch seconds), ``1`` present but unparseable (lexicographic),
    ``2`` absent (stable-sorted last, preserving relative order).
    """
    raw = str(msg.get("ts", "") or "")
    if not raw:
        return (2, 0.0, "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return (1, 0.0, raw)
    if parsed.tzinfo is None:
        # Naive means host-local — that is what a writer using datetime.now()
        # emits. Reading it as UTC would shift the turn by the host's offset.
        parsed = parsed.astimezone()
    return (0, parsed.timestamp(), "")


def merge_transcripts(
    slot_messages: list[dict[str, Any]],
    channel_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a slot's own transcript with its channel transcript, by timestamp.

    Neither side is a superset of the other. The slot transcript holds any
    dashboard replies; the channel transcript holds anything the user said on
    the channel AFTER the conversation was first surfaced. Re-surfacing (e.g.
    after a restart with ``restore_sessions`` off) must seed the tab with both.

    Ordering is by the ``ts`` field, normalized to an absolute instant by
    :func:`_ts_sort_key` so a naive local-time channel turn cannot interleave
    wrongly with a UTC dashboard turn. Duplicates — the channel turns already
    copied into the slot — are dropped on :func:`_identity`.
    """
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for m in list(slot_messages) + list(channel_messages):
        ident = _identity(m)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(m)
    return sorted(merged, key=_ts_sort_key)


def hydrate_window(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tail of *messages* a newly surfaced slot is actually seeded with.

    One function so the seeding loop and the frozen-prefix arithmetic that has
    to agree with it cannot drift: empty-content lines are dropped first (they
    are never appended), then the tail is capped at :data:`_HYDRATE_LIMIT`.
    """
    return [m for m in messages if m.get("content")][-_HYDRATE_LIMIT:]


def frozen_prefix_len(
    slot_messages: list[dict[str, Any]],
    window: list[dict[str, Any]],
) -> int:
    """Count the slot's on-disk lines that *window* does not re-serialize.

    ``_save_slot_to_history`` models a session file as **frozen prefix + live
    window**: it copies the first ``slot._disk_older_count`` on-disk lines
    verbatim, then writes ``serialize(window)`` after them. Seeding a slot with
    only the tail of a long transcript while leaving that count at ``0`` tells
    the next save the file has no prefix, so the older lines are re-emitted
    after newer ones — the transcript loses chronological order.

    The boundary is positional: every slot line before the first one the window
    carries. Because the window is the chronological tail of the merge, every
    later slot line is in it too, so nothing between the boundary and the end
    of the file is stranded.
    """
    if not slot_messages:
        return 0
    carried = {_identity(m) for m in window}
    for i, msg in enumerate(slot_messages):
        if _identity(msg) in carried:
            return i
    return len(slot_messages)


def surface_channel_session(
    state: "DashboardState",
    session_info: dict[str, Any],
    meta: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    disk_older: int = 0,
    disk_window: int = 0,
) -> "_ChatSlot | None":
    """Create the dashboard slot for one channel session.

    Returns the slot when this call newly surfaced it, else ``None`` (already
    present — the common steady-state case).

    *messages* is the merged transcript to seed the slot with, read by the caller
    so the disk IO stays off the event loop. *disk_older* and *disk_window* are
    that caller's split of the CHANNEL transcript — the file this slot now shares
    — into the frozen prefix the window does not carry and the region it
    re-serializes; see :func:`frozen_prefix_len` for why a save needs both.
    """
    session_key = session_info.get("key", "")
    if not session_key or not is_channel_session_key(session_key):
        return None

    # The slot key is derived from the channel session key, deterministically and
    # idempotently, so it IS the record of where the conversation started — the
    # sidebar reads provenance straight off it, and every restore path preserves
    # it for free because the key is the slot's identity.
    slot_name = channel_slot_name(session_key)
    if slot_name in state._slots:
        return None
    try:
        slot = state.get_or_create_slot(name=slot_name, agent=meta.get("agent", "") or "")
    except ValueError:
        # A slot with this name exists under a conflicting memory_mode. Leave it
        # alone rather than fighting over the key.
        logger.debug("channel slot %s exists with a conflicting memory_mode", slot_name)
        return None

    raw_title = session_info.get("title") or meta.get("title") or ""
    slot.title = _redact(raw_title) if raw_title else channel_label(session_key)
    slot._titled = bool(raw_title)
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("model"):
        slot.model = meta["model"]
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("pinned"):
        slot.pinned = True

    for msg in hydrate_window(messages):
        role = msg.get("role", "assistant")
        content = _redact(msg.get("content", ""))
        slot.append(
            role,
            content,
            f"msg msg-{'a' if role != 'user' else 'u'}",
            ts=msg.get("ts", ""),
            broadcast=False,
        )
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # The shared session file is frozen prefix + live window, and the file the
    # slot now writes IS the channel transcript — so both counts are measured
    # against the CHANNEL side of the merge. Crediting legacy dashboard-keyed
    # turns here would let a trim fold lines that are not in this file into its
    # frozen prefix, and the next save would rewrite the file short.
    slot._disk_older_count = disk_older
    slot._disk_window_len = disk_window
    # One session, two surfaces: read, write and run all resolve to the channel
    # key from here on. This is what makes the tab a live view of the channel
    # conversation rather than a copy of it.
    slot.shared_session_key = session_key
    # Not dirty: a surfaced conversation the user never touches costs no write.
    # The first real turn on either surface is what persists it.
    slot._dirty = False
    logger.info(
        "Surfaced %s session %s as slot %s", channel_label(session_key), session_key, slot_name
    )
    return slot


def resync_channel_slot(
    slot: "_ChatSlot",
    messages: list[dict[str, Any]],
    *,
    disk_older: int,
    disk_window: int,
) -> bool:
    """Refresh an already-surfaced slot's window from the shared transcript.

    Returns ``True`` when the slot changed. A no-op when the slot is dirty or
    running: the user (or a turn on either surface) owns the window then, and
    replacing it would drop unsaved turns.

    This is what keeps a tab current for a conversation still moving on the
    channel — and it is also a correctness requirement, not just freshness. The
    frozen-prefix counters are a snapshot of the shared file; if the channel side
    appends while they go stale, the next save rewrites the file from
    ``meta + frozen + window`` and silently drops everything appended past the
    snapshot.
    """
    if slot._dirty or slot.running:
        return False
    window = hydrate_window(messages)
    if len(window) == len(slot.messages) and all(
        _identity(a) == _identity(b) for a, b in zip(window, slot.messages)
    ):
        # Already current — do not churn a broadcast every 30s.
        slot._disk_older_count = disk_older
        slot._disk_window_len = disk_window
        return False
    slot.messages.clear()
    for msg in window:
        role = msg.get("role", "assistant")
        content = _redact(msg.get("content", ""))
        slot.append(
            role,
            content,
            f"msg msg-{'a' if role != 'user' else 'u'}",
            ts=msg.get("ts", ""),
            broadcast=False,
        )
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_older_count = disk_older
    slot._disk_window_len = disk_window
    slot._dirty = False
    return True


def _bind_slack_thread(state: "DashboardState", session_key: str, slot_name: str) -> None:
    """Register a surfaced Slack slot so inbound thread replies run ITS turn.

    ``slack.handler.maybe_route_linked_thread`` looks the slot up by the BARE
    ``thread_ts`` in ``state._slack_to_slot``; once bound, an inbound reply is
    appended to this slot and dispatched through the dashboard turn path instead
    of Slack's own ``handle_message`` loop, so the conversation has exactly one
    running turn and one transcript.

    ``link_slack`` is reused verbatim rather than reimplemented: it owns evicting
    a stale thread mapping, stealing the thread from a previous owner slot, and
    persisting the binding to the session map. Called on every pass because
    ``_slack_to_slot`` is in-memory and does not survive a gateway restart.
    """
    if channel_namespace_of(session_key) != "slack":
        return
    thread_ts = session_key.split(":", 1)[1] if ":" in session_key else ""
    if not thread_ts:
        return
    slot = state._slots.get(slot_name)
    if slot is not None and slot._slack_linked and slot._slack_thread_ts == thread_ts:
        return  # already bound — link_slack would only re-broadcast
    channel_id = ""
    if state.sessions is not None:
        try:
            _ts, _chan = state.sessions.get_slack_link(session_key)
            channel_id = _chan or ""
        except Exception:
            channel_id = ""
    try:
        state.link_slack(slot_name, thread_ts, channel_id)
    except Exception:
        logger.debug("channel reconcile: link_slack failed for %s", slot_name, exc_info=True)


async def reconcile_channel_slots(state: "DashboardState", window_minutes: int) -> int:
    """One reconcile pass. Returns the number of slots surfaced or re-bound.

    Safe to call on the event loop: every filesystem read is offloaded, and only
    the in-memory slot mutations run on the loop (so ``state._slots`` is never
    touched from a worker thread).
    """
    log = state.conversation_log
    if log is None:
        return 0
    loop = asyncio.get_running_loop()
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None

    try:
        sessions = await loop.run_in_executor(None, log.list_sessions)
    except Exception:
        logger.debug("channel reconcile: list_sessions failed", exc_info=True)
        return 0

    candidates = [s for s in sessions if is_channel_session_key(s.get("key", ""))]
    if not candidates:
        return 0

    def _load_meta() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        # Both the channel key and the legacy slot key: a tab closed under the
        # old design recorded `closed` on the latter, and skipping it would
        # resurface it every pass.
        for s in candidates:
            for key in (s.get("key", ""), slot_history_key(s.get("key", ""))):
                if not key or key in out:
                    continue
                try:
                    out[key] = log.get_metadata(key)
                except Exception:
                    out[key] = {}
        return out

    metadata = await loop.run_in_executor(None, _load_meta)
    eligible = eligible_channel_sessions(candidates, metadata=metadata, cutoff=cutoff)
    if not eligible:
        return 0

    def _load_messages() -> dict[str, _Transcript]:
        out: dict[str, _Transcript] = {}
        for s in eligible:
            key = s.get("key", "")
            try:
                chan_msgs = log.read_messages(key)
            except Exception:
                chan_msgs = []
            try:
                legacy_msgs = log.read_messages(slot_history_key(key))
            except Exception:
                legacy_msgs = []
            # The channel transcript is the slot's own file now; a legacy
            # dashboard-keyed file only exists for conversations surfaced by the
            # previous design, and its turns are folded in once so migrating
            # loses nothing.
            merged = merge_transcripts(legacy_msgs, chan_msgs)
            older = frozen_prefix_len(chan_msgs, hydrate_window(merged))
            out[key] = _Transcript(merged, older, max(0, len(chan_msgs) - older))
        return out

    transcripts = await loop.run_in_executor(None, _load_messages)

    changed = 0
    for s in eligible:
        key = s.get("key", "")
        transcript = transcripts.get(key) or _Transcript([], 0, 0)
        slot_name = channel_slot_name(key)
        try:
            existing = state._slots.get(slot_name)
            if existing is None:
                if surface_channel_session(
                    state,
                    s,
                    metadata.get(key) or {},
                    transcript.messages,
                    disk_older=transcript.disk_older,
                    disk_window=transcript.disk_window,
                ):
                    changed += 1
            else:
                # Self-heal a slot that predates this binding (or a restart that
                # dropped it), then live-follow the shared transcript.
                if not existing.shared_session_key:
                    existing.shared_session_key = key
                    changed += 1
                if resync_channel_slot(
                    existing,
                    transcript.messages,
                    disk_older=transcript.disk_older,
                    disk_window=transcript.disk_window,
                ):
                    changed += 1
            if slot_name in state._slots:
                _bind_slack_thread(state, key, slot_name)
        except Exception:
            logger.warning("channel reconcile: failed to reconcile %s", key, exc_info=True)
    if changed:
        state.push_slots_update()
    return changed


async def channel_slot_reconciler(state: "DashboardState", window_minutes: int) -> None:
    """Background task: reconcile now, then every ``RECONCILE_INTERVAL_SECS``.

    Runs for the gateway's lifetime. Every iteration is guarded so a transient
    filesystem error can never kill the loop.
    """
    while True:
        try:
            await reconcile_channel_slots(state, window_minutes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("channel slot reconciler pass failed", exc_info=True)
        await asyncio.sleep(RECONCILE_INTERVAL_SECS)
