"""Transcript reads that survived earlier offload passes must not run on the loop.

``ConversationLog.read_messages`` / ``read_messages_chained`` parse a whole
transcript from disk — 100-300 ms of blocking IO on a large store. On the gateway
event loop that stalls every other request; the liveness heartbeat is itself a
coroutine, so a stalled loop cannot pet its watchdog and the process exits.

Three call paths were still reading on the loop after the dashboard-wide sweep:

  * ``chat_runner._run_chat`` read the transcript inline to decide whether the
    in-memory history needed re-injecting. It is wrapped in ``asyncio.to_thread``
    now, so only the read crosses to a worker; the count and the prefix build
    stay loop-affine.
  * ``api_send_message``'s cron→origin path rebuilt a missing slot with the
    synchronous ``_rehydrate_slot_from_history``. It routes through the existing
    ``rehydrate_slot_from_history_async`` now, which reads off the loop and keeps
    the loop-affine slot construction on the loop.
  * the gateway's cron dedup-suppress and silent-suppress paths let
    ``inject_cron_result_to_dashboard`` do its linked-session read on the loop
    (they passed no ``history``). They prefetch it off the loop and pass
    ``history=`` now, matching the creator path — which makes the inject perform
    no on-loop read. The invariant the prefetch depends on is verified directly
    against ``inject_cron_result_to_dashboard``.

Each test records the thread each read actually runs on (a recorder wrapping the
real read) and asserts the loop thread's ident never appears, plus at least one
behaviour-preservation assertion per site. Modeled on
``test_completion_result_read_off_loop.py``.

NOTE: this suite cannot execute in the offline sandbox (aiohttp / pytest-asyncio
are not installed and PyPI is blocked). It is authored to the repo's off_loop
pattern and its execution is deferred to CI.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_runner import _run_chat


class _ReadRecorder:
    """Wrap a ``ConversationLog`` read and remember which thread ran each call.

    The wrapper delegates to the real method, so the recorded ident is the
    thread that genuinely opened and parsed the transcript — not merely a thread
    that reached a call site.
    """

    def __init__(self, conversation_log, method_name):
        self._real = getattr(conversation_log, method_name)
        self.threads: list[int] = []
        self.keys: list[str] = []

    def __call__(self, key, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.keys.append(key)
        return self._real(key, *args, **kwargs)


def _stream_empty(client: MagicMock) -> None:
    async def _empty(msg):
        return
        yield  # pragma: no cover - generator shape only

    client.stream = _empty
    client.stream_command = _empty


def _run_chat_state(tmp_path, name="offload-run-chat"):
    """A ``_run_chat`` harness whose session ``get_or_create`` reports is_new.

    The transcript read under test only runs on a NEW session with in-memory
    messages that have not been flushed (the stop-mid-turn re-injection guard),
    so the fixture pins ``is_new=True`` and seeds the slot with a user turn.
    """
    state = _make_state(tmp_path)
    client = MagicMock()
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    _stream_empty(client)
    return state, slot


# ── chat_runner._run_chat ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_chat_history_read_runs_off_the_loop_thread(tmp_path):
    """_run_chat's re-injection disk read executes on a non-loop thread."""
    state, slot = _run_chat_state(tmp_path)
    recorder = _ReadRecorder(state.conversation_log, "read_messages")

    with patch.object(state.conversation_log, "read_messages", recorder):
        await _run_chat(state, slot, "next message")

    assert recorder.threads, (
        "the _run_chat re-injection read never ran -- this test no longer "
        "exercises the on-loop read and would pass vacuously"
    )
    assert threading.get_ident() not in recorder.threads, (
        "the _run_chat history read ran on the event-loop thread; it must be "
        "handed to asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_run_chat_reinjects_when_memory_leads_disk(tmp_path):
    """Preservation: mem_count > disk_count still triggers history re-injection.

    The slot carries a user turn that was never flushed to disk (the empty
    transcript reads back zero rows), so the loop must still prepend the
    in-memory prefix to the outgoing prompt exactly as before the offload.
    """
    state, slot = _run_chat_state(tmp_path)
    sent: list[str] = []

    async def _capture(msg):
        sent.append(msg)
        return
        yield  # pragma: no cover - generator shape only

    state.sessions.get_or_create.return_value[0].stream = _capture
    state.sessions.get_or_create.return_value[0].stream_command = _capture

    with patch(
        "kiro_crew.dashboard.chat_runner._build_history_prefix",
        return_value="[history prefix]\n",
    ) as prefix:
        await _run_chat(state, slot, "next message")

    # disk read returned 0 rows while the slot holds a user message, so the
    # mem>disk branch fires and the prefix is applied to the sent prompt.
    prefix.assert_called_once_with(slot)
    assert sent, "the turn never reached the provider stream"
    assert sent[0].startswith("[history prefix]\n")


# ── inject_cron_result_to_dashboard (the invariant the gateway prefetch uses) ──


def _cron_job(job_id="cronjob1"):
    job = MagicMock()
    job.id = job_id
    job.name = "nightly"
    job.agent_id = ""
    return job


@pytest.mark.asyncio
async def test_cron_inject_performs_no_read_when_history_is_supplied(tmp_path):
    """Passing history= skips inject_cron_result_to_dashboard's on-loop read.

    The two gateway suppress paths prefetch the transcript off the loop and pass
    it in; this pins the contract they rely on — a supplied history means the
    inject never touches ``conversation_log`` on the loop.
    """
    from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard

    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    job = _cron_job()
    recorder = _ReadRecorder(state.conversation_log, "read_messages")

    with patch.object(state.conversation_log, "read_messages", recorder):
        inject_cron_result_to_dashboard(state, job, "cron output", history=[])

    assert recorder.threads == [], (
        "inject_cron_result_to_dashboard read the transcript even though the "
        "caller supplied history=; the gateway suppress paths depend on this "
        "read being skipped so their off-loop prefetch is the only read"
    )


@pytest.mark.asyncio
async def test_cron_inject_reads_inline_only_without_history(tmp_path):
    """Preservation: with no history the defensive inline read still runs.

    The inline read is the fallback for a purely-synchronous caller; removing it
    would silently drop the linked-session hydration. This is the on-loop read
    the gateway callers avoid by prefetching — kept here as the documented
    fallback, and shown to fire only when history is omitted.
    """
    from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard

    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    job = _cron_job("cronjob2")
    recorder = _ReadRecorder(state.conversation_log, "read_messages")

    with patch.object(state.conversation_log, "read_messages", recorder):
        inject_cron_result_to_dashboard(state, job, "cron output")

    assert recorder.keys == [
        "cron:cronjob2"
    ], "the no-history fallback did not read the linked cron transcript"


# ── api_send_message cron→origin rehydrate routing ─────────────────────────


def _send_app(state):
    from aiohttp import web

    from kiro_crew.dashboard.handlers import api_send_message

    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


def _send_state_missing_slot(job_id="sendjob1"):
    """A send-message state whose origin slot is absent, forcing rehydrate."""
    state = MagicMock()
    state.slack_client = None
    state.owner_id = ""
    job = MagicMock()
    job.id = job_id
    job.name = "nightly"
    job.session_key = "dashboard:chat-1-origin"
    state.crons.list_jobs.return_value = [job]
    state.get_slot.return_value = None
    return state


@pytest.mark.asyncio
async def test_send_message_origin_routes_through_async_rehydrate():
    """The cron→origin cold path awaits rehydrate_slot_from_history_async.

    Routing through the async entry keeps the transcript read off the loop while
    the loop-affine slot build stays on it. The synchronous _rehydrate helper
    must not be called on this path.
    """
    state = _send_state_missing_slot()
    revived = MagicMock()
    revived.running = False
    revived._in_stage_execution = False
    revived.task = None
    revived.key = "chat-1-origin"
    state._background_tasks = set()
    state.push_slots_update = MagicMock()

    with (
        patch(
            "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async",
            new_callable=AsyncMock,
            return_value=revived,
        ) as async_rehydrate,
        patch(
            "kiro_crew.dashboard.chat_persistence._rehydrate_slot_from_history"
        ) as sync_rehydrate,
        patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock),
    ):
        app = _send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "update",
                    "session": "origin",
                    "caller_session": "cron:sendjob1",
                },
                headers={"X-Session-Key": "cron:sendjob1"},
            )
            assert resp.status == 200

    async_rehydrate.assert_awaited_once_with(state, "chat-1-origin")
    sync_rehydrate.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_origin_none_rehydrate_falls_back_to_notify():
    """Preservation: async rehydrate returning None still degrades to the bell.

    The async entry has the same contract as the sync form — None for a
    closed/gone session — and the handler must keep treating that as
    origin-unreachable rather than creating a phantom slot.
    """
    state = _send_state_missing_slot("sendjob2")
    state.get_slot.return_value = None

    with patch(
        "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async",
        new_callable=AsyncMock,
        return_value=None,
    ) as async_rehydrate:
        app = _send_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "update",
                    "session": "origin",
                    "caller_session": "cron:sendjob2",
                },
                headers={"X-Session-Key": "cron:sendjob2"},
            )
            assert resp.status == 200
            data = await resp.json()

    async_rehydrate.assert_awaited_once_with(state, "chat-1-origin")
    assert data["session"] is False
    state.notify.assert_called_once()
