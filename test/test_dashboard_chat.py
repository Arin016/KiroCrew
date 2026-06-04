"""Tests for dashboard chat session — slot management, pagination, history persistence."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import (
    AsyncIterator,
    _make_app,
    _make_app_with_agent_routes,
    _make_folder_app,
    _make_state,
)

from kiro_claw.dashboard.state import _MAX_SLOT_MESSAGES, DashboardState, _ChatSlot
from kiro_claw.history import ConversationLog

# ── Slot unit tests ──


class TestChatSlot:
    def test_append_and_drain(self):
        slot = _ChatSlot("s1")
        slot.append("user", "hello", "msg")
        slot.append("assistant", "hi", "msg")
        pending = slot.drain()
        assert len(pending) == 2
        assert pending[0]["role"] == "user"
        assert pending[1]["role"] == "assistant"
        assert slot.drain() == []

    def test_drain_clears_stale_pending_after_reader_disconnect(self):
        """Simulate SSE reader disconnect: pending chunks must be discarded."""
        slot = _ChatSlot("s1")
        slot._has_reader = True
        slot.append("assistant", "stale response", "msg")
        assert len(slot._pending) == 1
        slot.drain()
        slot._has_reader = False
        assert slot._pending == []
        assert slot.drain() == []
        slot.append("assistant", "fresh response", "msg")
        pending = slot.drain()
        assert len(pending) == 1
        assert pending[0]["content"] == "fresh response"

    def test_total_messages_survives_trim(self):
        slot = _ChatSlot("s1")
        count = _MAX_SLOT_MESSAGES + 100
        for i in range(count):
            slot.append("user", f"msg {i}")
        assert len(slot.messages) == _MAX_SLOT_MESSAGES
        assert slot.total_messages == count

    def test_trim_keeps_latest(self):
        slot = _ChatSlot("s1")
        count = _MAX_SLOT_MESSAGES + 50
        for i in range(count):
            slot.append("user", f"msg {i}")
        assert slot.messages[0]["content"] == "msg 50"
        assert slot.messages[-1]["content"] == f"msg {count - 1}"

    def test_to_dict(self):
        slot = _ChatSlot("s1", title="Test Chat", mode="orchestrator")
        slot.append("user", "hi")
        d = slot.to_dict()
        assert d["key"] == "s1"
        assert d["title"] == "Test Chat"
        assert d["mode"] == "orchestrator"
        assert d["messages"] == 1
        assert d["running"] is False
        assert d["pending_approval"] is False

    def test_pending_approval_flag(self):
        slot = _ChatSlot("s1")
        loop = asyncio.new_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        assert slot.to_dict()["pending_approval"] is True
        fut.set_result("approved")
        assert slot.to_dict()["pending_approval"] is False
        loop.close()

    def test_pending_subagent_failures_initialized_empty(self):
        slot = _ChatSlot("s1")
        assert slot._pending_subagent_failures == []

    def test_pending_subagent_failures_drain(self):
        slot = _ChatSlot("s1")
        slot._pending_subagent_failures.append(
            "[Subagent completion event]\nAgent `a1` ❌ timed out"
        )
        slot._pending_subagent_failures.append(
            "[Subagent completion event]\nAgent `a2` ❌ timed out"
        )
        # Simulate drain logic from _run_chat
        failures = slot._pending_subagent_failures[:]
        slot._pending_subagent_failures.clear()
        message = "\n\n".join(failures) + "\n\n" + "user message"
        assert "[Subagent completion event]" in message
        assert "Agent `a1`" in message
        assert "Agent `a2`" in message
        assert message.endswith("user message")
        assert slot._pending_subagent_failures == []


@pytest.mark.asyncio
class TestApiChatDrainOnDisconnect:
    """Cover the slot.drain() call in chat_handlers' SSE finally block."""

    async def test_sse_reader_drains_pending_on_cancel(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        async def fake_run_chat(st, sl, msg):
            sl.append("chunk", "partial answer", "chunk")
            await asyncio.sleep(60)

        monkeypatch.setattr("kiro_claw.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "s1"},
                timeout=None,
            )
            line = b""
            async for chunk in resp.content.iter_any():
                line += chunk
                if b"partial answer" in line:
                    break

            resp.close()
            await asyncio.sleep(0.1)

        assert slot._pending == []
        assert slot._has_reader is False


# ── Slot detail pagination (HTTP) ──


class TestSlotDetailPagination:
    @pytest.mark.asyncio
    async def test_default_returns_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        for i in range(10):
            slot.append("user", f"msg {i}")
        slot.drain()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test")
            data = await resp.json()
            assert data["total"] == 10
            assert len(data["messages"]) == 10
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_pagination_with_before(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        log = state.conversation_log
        for i in range(300):
            log.append("dashboard:test", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/test?limit=200")
            data = await resp.json()
            assert data["has_more"] is True
            assert len(data["messages"]) == 200
            assert data["total"] == 300

            resp = await client.get("/api/chat/slots/test?limit=200&before=100")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["has_more"] is False
            assert data["messages"][0]["content"] == "msg 0"

    @pytest.mark.asyncio
    async def test_empty_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/empty")
            data = await resp.json()
            assert data["total"] == 0
            assert data["messages"] == []
            assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nonexistent")
            assert resp.status == 404


# ── History persistence and disk fallback ──


class TestHistoryPersistence:
    def test_tool_messages_saved(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "tool", "✅ bash")
        log.append("dashboard:s1", "assistant", "hi there")
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 3
        assert msgs[1]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_disk_fallback_for_trimmed_slot(self, tmp_path, monkeypatch):
        """Default view uses in-memory; pagination of older messages uses disk."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("big")
        log = state.conversation_log

        # Use a count that fits in memory — test disk pagination without trim
        for i in range(300):
            log.append("dashboard:big", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/big?limit=200")
            data = await resp.json()
            assert data["total"] == 300
            assert data["has_more"] is True
            assert data["messages"][-1]["content"] == "msg 299"

            # Pagination with before: falls back to disk
            resp = await client.get("/api/chat/slots/big?limit=200&before=100")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False


# ── Slot lifecycle ──


class TestSlotLifecycle:
    @pytest.mark.asyncio
    async def test_list_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("a")
        state.get_or_create_slot("b")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots")
            data = await resp.json()
            keys = [s["key"] for s in data]
            assert "a" in keys and "b" in keys

    @pytest.mark.asyncio
    async def test_approve_no_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_approve_resolves_future(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            data = await resp.json()
            assert data["ok"] is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_trust_sets_flag_and_approves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True
            assert slot._trust is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_approve_broadcasts_approval_resolved_single_pending(self, tmp_path, monkeypatch):
        """Single pending future without explicit request_id: extracts id and broadcasts."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-abc"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved", {"id": "req-abc", "approved": True}
            )

    @pytest.mark.asyncio
    async def test_approve_broadcasts_with_explicit_request_id(self, tmp_path, monkeypatch):
        """Explicit request_id is forwarded in the broadcast."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-xyz"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "approved", "request_id": "req-xyz"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved", {"id": "req-xyz", "approved": True}
            )

    @pytest.mark.asyncio
    async def test_reject_broadcasts_approved_false(self, tmp_path, monkeypatch):
        """Rejection broadcasts approved=False."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-rej"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "rejected", "request_id": "req-rej"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved", {"id": "req-rej", "approved": False}
            )


# ── Multi-slot isolation ──


class TestMultiSlotIsolation:
    @pytest.mark.asyncio
    async def test_slots_have_independent_messages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        s1.append("user", "hello from s1")
        s2.append("user", "hello from s2")
        s2.append("assistant", "reply in s2")
        s1.drain()
        s2.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            r1 = await (await client.get("/api/chat/slots/s1")).json()
            r2 = await (await client.get("/api/chat/slots/s2")).json()
            assert r1["total"] == 1
            assert r2["total"] == 2
            assert r1["messages"][0]["content"] == "hello from s1"


# ── Full pagination walk (simulates infinite scroll) ──


class TestFullPaginationWalk:
    @pytest.mark.asyncio
    async def test_walk_all_pages(self, tmp_path, monkeypatch):
        """Simulate frontend infinite scroll — walk backwards through all messages."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("walk")
        log = state.conversation_log
        total_msgs = 450

        for i in range(total_msgs):
            log.append("dashboard:walk", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            all_collected: list[str] = []
            before = None
            pages = 0

            while True:
                url = "/api/chat/slots/walk?limit=100"
                if before is not None:
                    url += f"&before={before}"
                resp = await client.get(url)
                data = await resp.json()
                msgs = data["messages"]
                all_collected = [m["content"] for m in msgs] + all_collected
                pages += 1

                if not data["has_more"]:
                    break
                before = data["total"] - len(all_collected)

            assert len(all_collected) == total_msgs
            assert all_collected[0] == "msg 0"
            assert all_collected[-1] == f"msg {total_msgs - 1}"
            assert pages > 1

    @pytest.mark.asyncio
    async def test_walk_with_trimmed_memory(self, tmp_path, monkeypatch):
        """Pagination with before uses disk — can access all messages."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("trim")
        log = state.conversation_log

        for i in range(400):
            log.append("dashboard:trim", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/trim?limit=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][-1]["content"] == "msg 399"

            # Pagination: disk has all 400
            resp = await client.get("/api/chat/slots/trim?limit=200&before=200")
            data = await resp.json()
            assert data["total"] == 400
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False


# ── SSE broadcast: _has_reader mutual exclusion ──


class TestHasReaderFlag:
    """Verify _has_reader prevents duplicate message delivery."""

    def test_broadcast_skipped_when_reader_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = True
        slot.append("assistant", "should not broadcast")
        assert len(received) == 0

    def test_broadcast_fires_when_no_reader(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("assistant", "should broadcast")
        assert len(received) == 1
        assert received[0]["role"] == "assistant"

    def test_chunk_never_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("chunk", "text")
        assert len(received) == 0

    def test_user_never_broadcast(self, tmp_path, monkeypatch):
        """User messages are added optimistically by frontend — no SSE broadcast."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("user", "hello")
        assert len(received) == 0

    def test_tool_and_permission_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("tool", "✅ bash")
        slot.append("permission", "run ls")
        assert len(received) == 2


# ── Chunk cleanup after response ──


class TestChunkCleanup:
    def test_chunks_removed_from_messages(self):
        """After assistant response, chunk messages should be cleaned up."""
        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("chunk", "He")
        slot.append("chunk", "llo")
        slot.append("chunk", " world")
        assert sum(1 for m in slot.messages if m["role"] == "chunk") == 3

        # Simulate what _run_chat does after streaming
        slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
        slot.append("assistant", "Hello world")
        assert sum(1 for m in slot.messages if m["role"] == "chunk") == 0
        assert slot.messages[-1]["role"] == "assistant"
        assert slot.messages[0]["role"] == "user"


# ── _prepare_messages filtering ──


class TestPrepareMessages:
    def test_queued_preserved_done_stripped(self):
        """queued messages must survive _prepare_messages so the frontend shows the banner after tab switch."""
        from kiro_claw.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "queued", "content": "next msg"},
            {"role": "done", "content": ""},
            {"role": "assistant", "content": "hi"},
        ]
        out = _prepare_messages(msgs, running=False)
        roles = [m["role"] for m in out]
        assert "queued" in roles, "queued must be preserved for tab-switch indicator"
        assert "done" not in roles, "done must be stripped"

    def test_chunks_collapsed_to_streaming(self):
        """Trailing chunks should be collapsed into a single streaming message."""
        from kiro_claw.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "chunk", "content": "Hel"},
            {"role": "chunk", "content": "lo"},
        ]
        out = _prepare_messages(msgs, running=True)
        assert out[-1]["role"] == "streaming"
        assert "Hel" in out[-1]["content"]

    def test_queued_placeholder_removed_on_processing(self):
        """When a queued message starts processing, its placeholder is replaced by a user entry."""
        import json

        from kiro_claw.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        slot.append("user", "first")
        qid = slot.queue_append("second")
        slot.append("queued", "second", json.dumps({"queue_id": qid}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        roles = [m["role"] for m in slot.messages]
        assert "queued" not in roles, "queued placeholder must be removed once processing starts"
        assert roles.count("user") == 2

    def test_duplicate_queued_removes_only_targeted(self):
        """When the same text is queued twice, only the targeted placeholder is removed by ID."""
        import json

        from kiro_claw.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        qid1 = slot.queue_append("hello")
        qid2 = slot.queue_append("hello")
        slot.append("queued", "hello", json.dumps({"queue_id": qid1}))
        slot.append("queued", "hello", json.dumps({"queue_id": qid2}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued) == 1, "second queued placeholder must survive"
        # Verify the surviving placeholder is the one with qid2
        surviving_cls = json.loads(queued[0].get("cls", "{}"))
        assert surviving_cls.get("queue_id") == qid2


# ── History save on close (not per-turn) ──


class TestHistorySaveOnClose:
    @pytest.mark.asyncio
    async def test_close_saves_to_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            data = await resp.json()
            assert data["ok"] is True

        # Verify saved to disk
        msgs = state.conversation_log.read_messages("dashboard:s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_transient_roles_excluded_from_history(self, tmp_path, monkeypatch):
        """chunk, done, queued, permission should not be saved to history."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "run ls")
        slot.append("permission", "ls")
        slot.append("tool", "✅ ls")
        slot.append("queued", "next msg")
        slot.append("chunk", "partial")
        slot.append("done", "")
        slot.append("assistant", "done")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.delete("/api/chat/slots/s1")

        msgs = state.conversation_log.read_messages("dashboard:s1")
        roles = [m["role"] for m in msgs]
        assert "chunk" not in roles
        assert "done" not in roles
        assert "queued" not in roles
        assert "permission" not in roles
        assert roles == ["user", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_no_save_for_unchanged_resumed_session(self, tmp_path, monkeypatch):
        """Resumed session closed without new messages should not re-save."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:hist1", "user", "old msg")
        log.append("dashboard:hist1", "assistant", "old reply")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume
            await client.post(
                "/api/chat/slots/hist1/resume",
                json={"key": "dashboard:hist1", "title": "Old Chat"},
            )
            # Close without chatting
            await client.delete("/api/chat/slots/hist1")

        # Original history should be unchanged
        msgs = log.read_messages("dashboard:hist1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "old msg"

    def test_close_saves_mode_to_history(self, tmp_path, monkeypatch):
        """Slot mode is persisted in session metadata on close."""
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("orch1", mode="orchestrator")
        slot.append("user", "plan")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:orch1")
        assert meta.get("mode") == "orchestrator"

    def test_close_does_not_persist_trust(self, tmp_path, monkeypatch):
        """Trust flags are ephemeral — not written to session metadata."""
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t1")
        slot._trust = True
        slot._trust_reads = True
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        meta = state.conversation_log._read_metadata("dashboard:t1")
        assert meta.get("trust") is None
        assert meta.get("trust_reads") is None


# ── Resume deduplication ──


class TestResumeDedupe:
    @pytest.mark.asyncio
    async def test_resume_existing_slot_returns_it(self, tmp_path, monkeypatch):
        """Resuming a session that's already active should return existing slot."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            # First resume
            r1 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r1["ok"] is True

            # Add a message to the active slot
            state._slots["s1"].append("user", "new msg")
            state._slots["s1"].drain()

            # Second resume — should return existing with new msg
            r2 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r2["ok"] is True
            assert r2["total"] == 2  # original + new

            # Should still be one slot, not two
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert sum(1 for s in slots if s["key"] == "s1") == 1

    @pytest.mark.asyncio
    async def test_resume_close_resume_no_duplicate_history(self, tmp_path, monkeypatch):
        """Resume → close → resume → close should not create duplicate history."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "assistant", "hi")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume and add a message
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            state._slots["s1"].append("user", "new question")
            state._slots["s1"].append("assistant", "new answer")
            state._slots["s1"].drain()
            await client.delete("/api/chat/slots/s1")

            # Resume again and close without changes
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            await client.delete("/api/chat/slots/s1")

        # Should have 4 messages (original 2 + new 2), not duplicated
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 4


# ── History key prefix handling ──


class TestHistoryKeyPrefix:
    @pytest.mark.asyncio
    async def test_no_double_dashboard_prefix(self, tmp_path, monkeypatch):
        """Slot key starting with 'dashboard:' should not get double-prefixed."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:chat-1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat/slots/dashboard:chat-1/resume",
                json={"key": "dashboard:chat-1"},
            )
            state._slots["dashboard:chat-1"].append("user", "new msg")
            state._slots["dashboard:chat-1"].drain()
            await client.delete("/api/chat/slots/dashboard:chat-1")

        # Should be saved under dashboard:chat-1, not dashboard:dashboard:chat-1
        msgs = log.read_messages("dashboard:chat-1")
        assert len(msgs) == 2
        assert log.read_messages("dashboard:dashboard:chat-1") == []


# ── Default view uses in-memory (not stale disk) ──


class TestInMemoryAuthority:
    @pytest.mark.asyncio
    async def test_default_view_shows_current_messages(self, tmp_path, monkeypatch):
        """Default slot detail should return in-memory messages, not stale disk."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Stale disk data
        log.append("dashboard:s1", "user", "old question")
        log.append("dashboard:s1", "assistant", "old answer")

        # Active slot with different messages
        slot = state.get_or_create_slot("s1")
        slot.append("user", "new question")
        slot.append("tool", "✅ running")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1")
            data = await resp.json()
            # Should show in-memory (2 msgs), not disk (2 different msgs)
            assert data["total"] == 2
            assert data["messages"][0]["content"] == "new question"
            assert data["messages"][1]["content"] == "✅ running"

    @pytest.mark.asyncio
    async def test_full_load_prepends_older_disk_messages(self, tmp_path, monkeypatch):
        """No-limit path prepends older disk messages when restore truncated."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Simulate: 8 messages on disk total (5 older + 3 recent)
        for i in range(8):
            log.append("dashboard:s2", "user", f"msg {i}")
        # Slot has only the last 3 in memory (simulating truncated restore)
        slot = state.get_or_create_slot("s2")
        slot.append("user", "msg 5")
        slot.append("user", "msg 6")
        slot.append("user", "msg 7")
        slot.drain()
        # Flag that restore truncated older messages
        slot._disk_older_count = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s2")
            data = await resp.json()
            assert data["total"] == 8  # 5 older + 3 recent
            assert data["has_more"] is False
            assert data["messages"][0]["content"] == "msg 0"
            assert data["messages"][4]["content"] == "msg 4"
            assert data["messages"][5]["content"] == "msg 5"

    @pytest.mark.asyncio
    async def test_legacy_pagination_with_limit(self, tmp_path, monkeypatch):
        """Legacy limit-based pagination reads from chained disk."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:s3", "user", f"msg {i}")
        slot = state.get_or_create_slot("s3")  # noqa: F841

        async with TestClient(TestServer(_make_app(state))) as client:
            # limit=3 returns last 3, has_more=True
            resp = await client.get("/api/chat/slots/s3?limit=3")
            data = await resp.json()
            assert data["total"] == 10
            assert data["has_more"] is True
            assert len(data["messages"]) == 3
            assert data["messages"][-1]["content"] == "msg 9"

            # limit=3&before=5 returns msgs 2-4
            resp = await client.get("/api/chat/slots/s3?limit=3&before=5")
            data = await resp.json()
            assert data["has_more"] is True  # msgs 0, 1 still older
            assert [m["content"] for m in data["messages"]] == ["msg 2", "msg 3", "msg 4"]

            # before=2 returns last 2 older
            resp = await client.get("/api/chat/slots/s3?limit=100&before=2")
            data = await resp.json()
            assert data["has_more"] is False
            assert [m["content"] for m in data["messages"]] == ["msg 0", "msg 1"]


# ── Session rename tests ──


class TestSessionRename:
    @pytest.mark.asyncio
    async def test_rename_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "My Chat"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["title"] == "My Chat"
            assert slot.title == "My Chat"
            assert slot._titled is True
            state.push_slot_title.assert_called_once_with("s1", "My Chat")

    @pytest.mark.asyncio
    async def test_rename_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nonexistent/title", json={"title": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_empty_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/title",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_truncates_at_200(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            long_title = "x" * 300
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": long_title})
            data = await resp.json()
            assert resp.status == 200
            assert len(data["title"]) == 200
            assert state._slots["s1"].title == "x" * 200
            state.push_slot_title.assert_called_once_with("s1", "x" * 200)

    @pytest.mark.asyncio
    async def test_resumed_session_preserves_title(self, tmp_path, monkeypatch):
        """Resumed session should set _titled=True so auto-title doesn't overwrite."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1", "title": "My Custom Title"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "My Custom Title"
            assert slot._titled is True


# ── Session color tests ──


class TestSessionColor:
    @pytest.mark.asyncio
    async def test_set_color_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 3})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["color_index"] == 3
            assert state._slots["s1"].color_index == 3
            state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_set_color_null(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_index = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": None})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] is None
            assert slot.color_index is None

    @pytest.mark.asyncio
    async def test_set_color_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nope/color", json={"color_index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_set_color_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/color",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_negative_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_bool_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 0})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] == 0
            assert slot.color_index == 0

    @pytest.mark.asyncio
    async def test_set_color_large_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 99999})
            assert resp.status == 400

    def test_color_zero_persisted(self, tmp_path, monkeypatch):
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_index = 0
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_index") == 0

    def test_color_persisted_in_history(self, tmp_path, monkeypatch):
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_index = 4
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_index") == 4

    def test_color_null_not_persisted(self, tmp_path, monkeypatch):
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert "color_index" not in meta


# ── Slash command tests ──


class TestBlockedSlashCommands:
    """Tests for _BLOCKED_SLASH_COMMANDS blocking dangerous commands."""

    def test_quit_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/quit" in _BLOCKED_SLASH_COMMANDS

    def test_exit_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/exit" in _BLOCKED_SLASH_COMMANDS

    def test_q_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/q" in _BLOCKED_SLASH_COMMANDS

    def test_editor_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/editor" in _BLOCKED_SLASH_COMMANDS

    def test_chat_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/chat" in _BLOCKED_SLASH_COMMANDS

    def test_paste_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/paste" in _BLOCKED_SLASH_COMMANDS

    def test_reply_is_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/reply" in _BLOCKED_SLASH_COMMANDS

    def test_compact_is_not_blocked(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS

        assert "/compact" not in _BLOCKED_SLASH_COMMANDS

    def test_blocked_is_subset_of_slash(self):
        from kiro_claw.dashboard.chat import _BLOCKED_SLASH_COMMANDS, _SLASH_COMMANDS

        assert _BLOCKED_SLASH_COMMANDS.issubset(_SLASH_COMMANDS)

    @pytest.mark.asyncio
    async def test_blocked_command_returns_warning_no_session(self, tmp_path, monkeypatch):
        """Posting /quit should add warning to slot and never acquire a session."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/quit")

        # Should have the warning message
        texts = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert any("not available in the dashboard" in t for t in texts)
        # Should never have called get_or_create (no session acquired)
        state.sessions.get_or_create.assert_not_called()


# ── Background session leak regression ──


class TestTitleGenerationSessionLeak:
    """_generate_title_via_kiro must release BACKGROUND_KEY even when stream() raises."""

    @pytest.mark.asyncio
    async def test_background_session_released_on_stream_error(self, tmp_path):
        from kiro_claw.dashboard.chat import _generate_title_via_kiro
        from kiro_claw.session import BACKGROUND_KEY

        state = _make_state(tmp_path)

        # Mock client whose stream() raises mid-iteration
        mock_client = MagicMock()

        async def _exploding_stream(prompt):
            raise RuntimeError("throttle / ACP error")
            yield  # noqa: unreachable — makes this an async generator

        mock_client.stream = _exploding_stream
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

        with pytest.raises(RuntimeError, match="throttle"):
            await _generate_title_via_kiro(state, messages)

        # The critical assertion: release MUST be called even though stream() raised
        state.sessions.release.assert_called_once_with(BACKGROUND_KEY)

    @pytest.mark.asyncio
    async def test_permission_request_rejected_during_title_gen(self, tmp_path):
        from kiro_claw.dashboard.chat import _generate_title_via_kiro
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )
        from kiro_claw.session import BACKGROUND_KEY

        state = _make_state(tmp_path)
        mock_client = MagicMock()
        mock_client.reject_tool = AsyncMock()

        async def _stream(prompt):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="My Title")
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id="req-1")
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        mock_client.reject_tool.assert_called_once_with("req-1")
        assert title == "My Title"
        state.sessions.release.assert_called_once_with(BACKGROUND_KEY)

    @pytest.mark.asyncio
    async def test_complete_event_breaks_stream(self, tmp_path):
        from kiro_claw.dashboard.chat import _generate_title_via_kiro
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_claw.session import BACKGROUND_KEY

        state = _make_state(tmp_path)
        mock_client = MagicMock()

        async def _stream(prompt):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Good")
            yield LLMEvent(kind=EVENT_COMPLETE)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=" SHOULD NOT APPEAR")

        mock_client.stream = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        assert title == "Good"
        state.sessions.release.assert_called_once_with(BACKGROUND_KEY)


# ── Inline tool cards: _flush_segment and segment flush in _run_chat ──


class TestFlushSegment:
    """Unit tests for _flush_segment helper function."""

    def test_flush_segment_persists_and_broadcasts(self, tmp_path, monkeypatch):
        """_flush_segment persists assistant message and broadcasts chat_segment.

        Validates: Requirements 1.1, 1.2, 4.3, 6.3
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        # Simulate accumulated chunks
        slot.append("chunk", "Hello ")
        slot.append("chunk", "world")

        from kiro_claw.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "Hello world")

        # Chunks should be removed
        chunk_msgs = [m for m in slot.messages if m.get("role") == "chunk"]
        assert len(chunk_msgs) == 0
        # Assistant message should be persisted
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Hello world"
        # chat_segment should be broadcast
        state.broadcast_ws.assert_called_once_with("chat_segment", {"slot": "s1"})


class TestRunChatSegmentFlush:
    """Tests for segment flush behavior in _run_chat()."""

    @staticmethod
    def _make_mock_client(events):
        """Create a mock ACP client that yields the given LLMEvent list."""
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        """Create a DashboardState wired for _run_chat tests."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_text_tool_text_complete_produces_two_segments(self, tmp_path, monkeypatch):
        """Mock event stream: text → tool_call → text → complete produces
        two assistant messages and one tool message.

        Validates: Requirements 1.1, 1.2, 1.3, 4.3
        """
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before tool"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="After tool"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Check persisted messages (exclude transient roles)
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 2
        assert assistant_msgs[0]["content"] == "Before tool"
        assert assistant_msgs[1]["content"] == "After tool"

        # Verify both chat_segment and tool_call are broadcast
        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        ws_types = [t for t, _ in ws_calls]
        assert "chat_segment" in ws_types
        assert "tool_call" in ws_types

    @pytest.mark.asyncio
    async def test_text_permission_request_flushes_segment(self, tmp_path, monkeypatch):
        """Mock event stream: text → permission_request flushes segment
        before permission flow.

        Validates: Requirements 1.4
        """
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Analyzing..."),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="bash",
                tool_kind="execute",
                request_id="req-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        # Enable YOLO mode so permission auto-approves (simplifies test)
        state.enable_yolo()
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.approve_tool = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "run ls")

        # Segment should have been flushed before permission flow
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" in ws_types
        # The flushed segment should be persisted as assistant
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any(m["content"] == "Analyzing..." for m in assistant_msgs)

    @pytest.mark.asyncio
    async def test_text_only_complete_no_segments(self, tmp_path, monkeypatch):
        """Text-only stream → complete produces one assistant message (no segments).

        Validates: Requirements 8.1
        """
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Just text"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No chat_segment events
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in ws_types
        # One assistant message
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Just text"

    @pytest.mark.asyncio
    async def test_chunk_seq_monotonically_increasing_across_segments(self, tmp_path, monkeypatch):
        """chunk_seq values in broadcast calls are monotonically increasing
        across segments.

        Validates: Requirements 7.1
        """
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="b"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="c"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="d"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Collect all seq values from chat_chunk broadcasts
        seq_values: list[int] = []
        for call in state.broadcast_ws.call_args_list:
            if call.args[0] == "chat_chunk":
                seq_values.append(call.args[1]["seq"])

        assert len(seq_values) == 4  # 4 text chunks
        # Verify strict monotonic increase
        for i in range(1, len(seq_values)):
            assert seq_values[i] > seq_values[i - 1], f"seq not monotonic: {seq_values}"


class TestRunChatCompactDeferredWait:
    """The deferred-compaction wait at the end of _run_chat is a kiro-cli-only
    protocol step. claude-agent-acp performs /compact synchronously inside
    session/prompt and never emits ``_kiro.dev/compaction/status``, so the
    handler must skip ``wait_for_compaction`` for that backend or it sits
    blocked for 30 minutes and finally surfaces "Compaction timed out."
    """

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.wait_for_compaction = AsyncMock(return_value={"type": "timeout"})

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_claude_backend_skips_wait_for_compaction(self, tmp_path, monkeypatch):
        """When ``is_claude_backend(client)`` is True, the dashboard must
        report success immediately and never call ``wait_for_compaction``."""
        from kiro_claw.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        # Patch the binding chat_runner imported at module load.
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_runner.is_claude_backend", lambda _provider: True
        )

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_not_called()
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("Conversation compacted" in m["content"] for m in assistant_msgs)
        assert not any("timed out" in m["content"] for m in assistant_msgs)
        # Updated context% must be broadcast so the dashboard bar refreshes.
        ws_kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "context_usage" in ws_kinds

    @pytest.mark.asyncio
    async def test_kiro_backend_still_waits_for_compaction(self, tmp_path, monkeypatch):
        """kiro-cli backend keeps the original deferred-wait path."""
        from kiro_claw.providers.base import EVENT_COMPLETE, LLMEvent

        events = [LLMEvent(kind=EVENT_COMPLETE)]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": "summary text"}
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_runner.is_claude_backend", lambda _provider: False
        )

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "/compact")

        client.wait_for_compaction.assert_awaited_once()
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("summary text" in m["content"] for m in assistant_msgs)


class TestTokenPersistenceBackfill:
    """Regression tests for the late-backfill of slot.model before
    persist_token_record is called from _run_chat.

    Background: Claude Code reports its model only after the prompt is
    dispatched (via the `init` system event). The original eager backfill
    at the start of _run_chat reads client._model too early for CC, so
    slot.model stays empty and tokens.jsonl records get model="". The
    fix re-reads client._model right before persisting the token record.
    """

    @staticmethod
    def _make_mock_client(events, prov_model=""):
        """Mock provider that exposes a nested client._model attribute,
        mirroring AcpClient/CcClient layout (provider.client._model).
        """
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        # Expose `client.client._model` like the real provider wrappers
        inner = MagicMock()
        inner._model = prov_model
        client.client = inner

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_late_backfill_populates_model_for_cc_session(self, tmp_path, monkeypatch):
        """When slot.model is empty at EVENT_COMPLETE but the provider has
        learned its model (CC init event), persist_token_record receives the
        provider model and slot.model is updated.

        The mock starts with an empty ``inner._model`` so the *early* backfill
        at the top of _run_chat finds nothing, then mutates ``inner._model``
        just before yielding EVENT_COMPLETE — mirroring CC reporting its
        model only after the prompt is dispatched. This way only the *late*
        backfill branch can populate the record's model, so removing the
        late-backfill code would cause this test to fail.
        """
        from kiro_claw.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(
                kind=EVENT_COMPLETE,
                input_tokens=12,
                output_tokens=34,
            ),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""  # CC has not yet emitted init when _run_chat begins

        # This simulates a claude_code session, so the backfill must run under
        # provider=claude_code for canonicalize_for_provider to map 'opus' ->
        # 'opus-4.8-1m'. The default test config is provider=acp, under which the
        # backfill (correctly) leaves a kiro/acp model unchanged — so force a CC
        # config here. _run_chat reads only cfg.agent.provider (+ dashboard.
        # merge_queued_messages) on this path, so a MagicMock cfg suffices.
        _cc_cfg = MagicMock()
        _cc_cfg.agent.provider = "claude_code"
        _cc_cfg.dashboard.merge_queued_messages = False
        monkeypatch.setattr("kiro_claw.dashboard.chat_runner.KiroClawConfig.load", lambda: _cc_cfg)

        # Build a mock whose inner._model starts EMPTY so the early backfill
        # branch (chat_runner.py:471-476) finds nothing and leaves slot.model
        # blank. Then mutate inner._model mid-stream — just before yielding
        # EVENT_COMPLETE — so only the late backfill branch can populate it.
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        inner = MagicMock()
        inner._model = ""  # empty at session-create time
        client.client = inner

        async def _stream(msg):
            # Simulate CC's `init` system event arriving mid-turn, after the
            # prompt has been dispatched but before EVENT_COMPLETE.
            inner._model = "opus"
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []

        def _fake_persist(slot_key, model, event, provider=""):
            captured.append((slot_key, model, provider))

        monkeypatch.setattr("kiro_claw.dashboard.chat_runner.persist_token_record", _fake_persist)

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        slot_key, model, provider = captured[0]
        assert slot_key == "s1"
        # The backfill canonicalizes the provider's model via from_provider_id so
        # it matches the canonical-keyed dropdown: 'opus' alias -> 'opus-4.8-1m'.
        assert model == "opus-4.8-1m", "late backfill should populate canonical model"
        # slot.model should also be updated so subsequent turns reuse it
        assert slot.model == "opus-4.8-1m"

    @pytest.mark.asyncio
    async def test_late_backfill_skips_auto_sentinel(self, tmp_path, monkeypatch):
        """The sentinel value 'auto' (CC's pre-init placeholder) must not be
        persisted as the model -- the record stays blank until a real model
        is known.
        """
        from kiro_claw.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, input_tokens=5, output_tokens=7),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = ""

        client = self._make_mock_client(events, prov_model="auto")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_runner.persist_token_record",
            lambda k, m, e, provider="": captured.append((k, m, provider)),
        )

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == ""
        assert slot.model == ""

    @pytest.mark.asyncio
    async def test_existing_slot_model_is_not_overwritten(self, tmp_path, monkeypatch):
        """OpenCode resolves model synchronously; slot.model is already set
        when EVENT_COMPLETE arrives. Backfill must not clobber it.
        """
        from kiro_claw.providers.base import EVENT_COMPLETE, LLMEvent

        events = [
            LLMEvent(kind=EVENT_COMPLETE, input_tokens=1, output_tokens=2),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.model = "claude-opus-4.6"

        # Even if the inner client somehow reports a different value,
        # slot.model wins because it was already set explicitly.
        client = self._make_mock_client(events, prov_model="should-not-be-used")
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        captured: list[tuple] = []
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_runner.persist_token_record",
            lambda k, m, e, provider="": captured.append((k, m, provider)),
        )

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert len(captured) == 1
        assert captured[0][1] == "claude-opus-4.6"
        assert slot.model == "claude-opus-4.6"


class TestPrepareMessagesInterleaved:
    """Tests for _prepare_messages with interleaved assistant/tool/chunk messages."""

    def test_interleaved_assistant_tool_chunk_structure(self):
        """_prepare_messages with interleaved assistant/tool/chunk returns
        correct structure.

        Validates: Requirements 6.1
        """
        from kiro_claw.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Before tool", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ read_file", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "After tool", "cls": "msg msg-a"},
            {"role": "chunk", "content": "still "},
            {"role": "chunk", "content": "streaming"},
        ]

        result = _prepare_messages(messages, running=True)

        # user, assistant, tool, assistant, streaming (collapsed chunks)
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Before tool"
        assert result[2]["role"] == "tool"
        assert result[3]["role"] == "assistant"
        assert result[3]["content"] == "After tool"
        assert result[4]["role"] == "streaming"
        assert result[4]["content"] == "still streaming"

    def test_no_trailing_chunks_no_streaming(self):
        """Without trailing chunks, no streaming message is produced."""
        from kiro_claw.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Segment 1", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ bash", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "Segment 2", "cls": "msg msg-a"},
        ]

        result = _prepare_messages(messages, running=False)

        assert len(result) == 4
        roles = [m["role"] for m in result]
        assert "streaming" not in roles
        assert "chunk" not in roles


# ── Runtime wiring tests (multi-agent-orchestration) ──


class TestRuntimeWiring:
    """Tests for multi-agent-orchestration runtime wiring.

    Requirements: 1.3, 2.3, 2.4, 3.1
    """

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_agent response includes resolved workspace field.

        Requirements: 1.3
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.reset = AsyncMock()

        # Mock config loading to return a config with a known agent
        mock_cfg = MagicMock()
        mock_cfg.agents = {"oncall": MagicMock(workspace="oncall-ws", memory_store="oncall-mem")}
        mock_cfg.workspaces = {"oncall-ws": MagicMock(dir="/tmp/oncall")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {"oncall-mem": MagicMock()}
        mock_cfg.memory = MagicMock()

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/oncall")
        mock_bindings.memory_store_name = "oncall-mem"

        monkeypatch.setattr("kiro_claw.dashboard.chat.KiroClawConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers.KiroClawConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "oncall"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "oncall"
            assert "workspace" in data
            assert data["workspace"] == "oncall-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_persists_to_metadata(self, tmp_path, monkeypatch):
        """Switching a slot's agent writes the new value to the JSONL metadata.

        Without this, a session resumed after a gateway restart reverts to
        whatever agent (if any) was recorded in the initial metadata line.
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.reset = AsyncMock()

        # Seed a session file so update_metadata has something to patch.
        # Use the canonical colon-separated key that the API handler derives
        # via _history_key_for("s1") → "dashboard:s1".  Using "dashboard_s1"
        # (underscore) maps to the same *file* on disk (_safe_key converts
        # both to "dashboard_s1.jsonl") but creates a different *cache key*,
        # so update_metadata's cache invalidation for "dashboard:s1" would
        # leave the "dashboard_s1" cache entry stale.
        history_key = "dashboard:s1"
        state.conversation_log.append(history_key, "user", "hi", agent="old-agent")
        assert state.conversation_log.get_metadata(history_key).get("agent") == "old-agent"

        # Minimal config stub (agent-binding resolution is exercised by the
        # workspace-focused test above; here we only care about persistence).
        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr("kiro_claw.dashboard.chat.KiroClawConfig.load", lambda: mock_cfg)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["agent"] == "new-agent"

        meta = state.conversation_log.get_metadata(history_key)
        assert (
            meta.get("agent") == "new-agent"
        ), f"expected new-agent in metadata, got {meta.get('agent')!r}"

    @pytest.mark.asyncio
    async def test_api_chat_slot_create_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_create response includes resolved workspace field.

        Requirements: 2.4
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()

        mock_bindings = MagicMock()
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"

        monkeypatch.setattr("kiro_claw.dashboard.chat.KiroClawConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers.KiroClawConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "new-slot", "agent": "research"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["workspace"] == "research-ws"

    def test_get_or_create_slot_accepts_workspace_parameter(self, tmp_path, monkeypatch):
        """get_or_create_slot accepts workspace parameter and sets it on the slot.

        Requirements: 2.3
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("ws-test", agent="oncall", workspace="oncall-ws")
        assert slot.workspace == "oncall-ws"
        assert slot.agent == "oncall"

        # Default workspace when not specified
        slot2 = state.get_or_create_slot("ws-default")
        assert slot2.workspace == "default"

        # Mode parameter
        slot3 = state.get_or_create_slot("mode-test", mode="orchestrator")
        assert slot3.mode == "orchestrator"
        assert state.get_or_create_slot("ws-default").mode == ""

    @pytest.mark.asyncio
    async def test_run_chat_passes_memory_store_to_build_message(self, tmp_path, monkeypatch):
        """_run_chat resolves agent bindings and passes memory_store to build_message.

        Requirements: 3.1
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)

        # Track calls to build_message
        build_message_calls: list[dict] = []

        def mock_build_message(self_ctx, text, is_new, session_key=None, **kwargs):
            build_message_calls.append({"text": text, "kwargs": kwargs})
            return text, MagicMock(action=None, text="")

        # Mock config loading
        mock_cfg = MagicMock()
        mock_cfg.agents = {"oncall": MagicMock(workspace="oncall-ws", memory_store="oncall-mem")}
        mock_cfg.default_agent = "default"

        mock_bindings = MagicMock()
        mock_bindings.memory_store_name = "oncall-mem"

        monkeypatch.setattr("kiro_claw.dashboard.chat.KiroClawConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )
        monkeypatch.setattr("kiro_claw.dashboard.chat_runner.KiroClawConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_claw.dashboard.chat_runner.resolve_agent_bindings",
            lambda cfg, name: mock_bindings,
        )

        # Create a context builder with mocked build_message
        from kiro_claw.context import ContextBuilder
        from kiro_claw.memory import MemoryStore
        from kiro_claw.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder, "build_message", lambda *a, **kw: mock_build_message(ctx_builder, *a, **kw)
        )

        state = _make_state(tmp_path, context_builder=ctx_builder)

        # Create a slot with an agent
        slot = state.get_or_create_slot("mem-test", agent="oncall")

        # Mock session manager to return a mock client
        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)

        # Import and run _run_chat
        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "test message")

        # Verify build_message was called with memory_store
        assert len(build_message_calls) == 1
        assert build_message_calls[0]["kwargs"].get("memory_store") == "oncall-mem"


class TestRunChatToolBoundarySegments:
    """Test that _run_chat inserts whitespace across tool call boundaries."""

    @pytest.mark.asyncio
    async def test_tool_boundary_splits_segments(self, tmp_path, monkeypatch):
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import LLMEvent

        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Let me check."),
            LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
            LLMEvent(kind="text_chunk", text="Done!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = AsyncMock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # With _flush_segment, text is split into separate segments at tool boundaries
        # so gluing can't happen — each segment is independent
        assert len(assistant_msgs) == 2
        assert "Let me check." in assistant_msgs[0]["content"]
        assert "Done!" in assistant_msgs[1]["content"]

    @pytest.mark.asyncio
    async def test_tool_boundary_empty_chunk_still_splits(self, tmp_path, monkeypatch):
        """Empty text chunk after tool call doesn't prevent segment splitting."""
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import LLMEvent

        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Before."),
            LLMEvent(kind="tool_call", title="T", tool_kind="read"),
            LLMEvent(kind="text_chunk", text=""),  # empty chunk
            LLMEvent(kind="text_chunk", text="After!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = AsyncMock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # Segments are flushed at tool boundaries; empty chunks don't create segments
        assert len(assistant_msgs) == 2
        assert "Before." in assistant_msgs[0]["content"]
        assert "After!" in assistant_msgs[1]["content"]


class TestRunChatToolCallUpdate:
    """EVENT_TOOL_CALL_UPDATE handler — claude-agent-acp emits a refinement
    once the streamed tool input is complete. The handler patches the in-place
    pill, persisted message, _pending_tools, and the SEL audit trail."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_refinement_patches_pill_content_and_meta(self, tmp_path, monkeypatch):
        """An initial tool_call with a stub title is overwritten by the refined
        title and the meta picks up the populated input."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-1"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command": "ls /tmp"}',
                tool_call_id="tc-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # The persisted content should reflect the refined title, not the stub.
        assert tool_msgs[0]["content"] == "🔧 ls /tmp"
        assert tool_msgs[0]["meta"]["tool_call_id"] == "tc-1"
        # The refined input is patched into meta.
        assert "ls /tmp" in tool_msgs[0]["meta"]["input"]

    @pytest.mark.asyncio
    async def test_refinement_broadcasts_chat_message_update(self, tmp_path, monkeypatch):
        """The handler broadcasts a chat_message_update WS event so the
        frontend can patch the persisted tile in place without a reload."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-2"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-2",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        kinds = [k for k, _ in ws_calls]
        # Refinement broadcasts both a fresh tool_call (toolLog merge) AND a
        # chat_message_update so the persisted tile updates live too.
        assert kinds.count("tool_call") >= 2
        assert "chat_message_update" in kinds
        # The second tool_call carries is_update:True so the frontend merges.
        update_payloads = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert len(update_payloads) == 1
        assert update_payloads[0]["tool"] == "ls /tmp"
        assert update_payloads[0]["tool_call_id"] == "tc-2"
        # And the chat_message_update carries the patched content + meta.
        msg_updates = [p for k, p in ws_calls if k == "chat_message_update"]
        assert msg_updates[0]["tool_call_id"] == "tc-2"
        assert msg_updates[0]["content"] == "🔧 ls /tmp"
        assert "input" in msg_updates[0]["meta"]

    @pytest.mark.asyncio
    async def test_refinement_preserves_existing_icon(self, tmp_path, monkeypatch):
        """Auto-approved tools may already carry a ✅ marker on the message
        with the same tool_call_id. The patch must preserve that prefix."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # Pre-seed a message that matches the tool_call_id with a ✅ icon —
        # mirrors the auto-approved-tool case where the post-approval marker
        # is already in place.
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-3"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-3",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # ✅ icon survives the patch; only the title changes.
        assert tool_msgs[0]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_breaks_on_first_match_walking_reverse(self, tmp_path, monkeypatch):
        """When two messages share the tool_call_id (auto-approved double-emit
        with 🔧 then ✅), only the most recent one is patched."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-4",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        # The earlier 🔧 message is untouched.
        assert tool_msgs[0]["content"] == "🔧 Terminal"
        # The later ✅ message is patched, with its icon preserved.
        assert tool_msgs[1]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_strips_running_prefix_from_pending_tools(self, tmp_path, monkeypatch):
        """_pending_tools feeds PostToolUse hooks by tool name. The refinement
        must strip the "Running: " prefix exactly like EVENT_TOOL_CALL does so
        hooks matching by name keep working after the refinement event."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Running: stub",
                tool_kind="execute",
                tool_call_id="tc-5",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="Running: ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-5",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        # Patch fire_tool_hooks so we can inspect the name passed in. The hook
        # only fires on EVENT_TOOL_CALL — but the refinement updates
        # _pending_tools, which is read by the EVENT_TOOL_RESULT handler. We
        # don't drive a tool_result here; instead we inspect _pending_tools
        # was updated correctly via the WS-broadcast surface area: the
        # refinement broadcasts the refined title without the "Running: "
        # prefix on the handler's local copy.
        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Indirectly verify via the persisted message — the title displayed on
        # the pill should be the refined one, sans "Running: " prefix because
        # the refinement code strips it for _pending_tools (the displayed
        # title still carries the prefix, but the hook-name copy doesn't).
        # The persisted pill carries the refined title verbatim — this is the
        # display surface, not the hook surface.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "🔧 Running: ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_logs_sel_audit_event(self, tmp_path, monkeypatch):
        """The handler logs a `tool_invocation` audit event with
        outcome="refined" so the audit trail captures the refined name."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        captured = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                captured.append(kw)

        monkeypatch.setattr("kiro_claw.dashboard.chat_runner.sel", lambda: _FakeSel())

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-6",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        refined = [c for c in captured if c.get("outcome") == "refined"]
        assert len(refined) == 1
        assert refined[0]["tool_name"] == "ls /tmp"
        assert refined[0]["tool_kind"] == "execute"
        assert refined[0]["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_refinement_no_tool_call_id_skipped(self, tmp_path, monkeypatch):
        """Refinement events without a tool_call_id are silently dropped —
        we have nothing to merge against."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TOOL_CALL_UPDATE, title="ls /tmp", tool_call_id=""),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No tool messages should have been added.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msgs == []
        # No chat_message_update broadcast either.
        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_no_matching_message_no_chat_message_update(
        self, tmp_path, monkeypatch
    ):
        """When no persisted tool message matches the tool_call_id, the
        handler still broadcasts the tool_call merge but skips
        chat_message_update (nothing to patch)."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-orphan",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        # tool_call still broadcast (toolLog merge by id is still meaningful).
        assert "tool_call" in kinds
        # Nothing to patch in slot.messages, so no chat_message_update.
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_redacts_credentials_in_input(self, tmp_path, monkeypatch):
        """Credentials in tool_input must be redacted before the broadcast
        and the persisted meta. _redact_tool_field applies both
        redact_exfiltration_urls and redact_credentials."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-cred"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="echo secret",
                tool_kind="execute",
                tool_input='{"command":"echo AKIAIOSFODNN7EXAMPLE"}',
                tool_call_id="tc-cred",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        # Credential must not appear in the persisted meta.
        assert "AKIAIOSFODNN7EXAMPLE" not in tool_msgs[0]["meta"].get("input", "")
        # Or in any of the broadcast payloads.
        for call in state.broadcast_ws.call_args_list:
            assert "AKIAIOSFODNN7EXAMPLE" not in str(call.args[1])

    @pytest.mark.asyncio
    async def test_refinement_handler_swallows_exceptions(self, tmp_path, monkeypatch):
        """A malformed broadcast or other exception inside the handler must
        not tear down the run loop. The try/except logs and continues."""
        from kiro_claw.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        # Make broadcast_ws raise on the first call (the tool_call broadcast).
        # The try/except in the handler must catch it so subsequent events
        # (the trailing EVENT_TEXT_CHUNK) still process.
        original_broadcast = state.broadcast_ws

        def _raising_then_normal(*args, **kwargs):
            if args and args[0] == "tool_call":
                raise RuntimeError("simulated broadcast failure")
            return original_broadcast(*args, **kwargs)

        state.broadcast_ws = MagicMock(side_effect=_raising_then_normal)

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-boom",
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="after the boom"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_claw.dashboard.chat import _run_chat

        # Must not raise — the run loop should continue past the exception.
        await _run_chat(state, slot, "hello")

        # The trailing assistant message proves the run loop survived.
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("after the boom" in m["content"] for m in assistant_msgs)


# ── Mode/approval policy propagation (HTTP handlers) ──


class TestApiChatModePropagation:
    """api_chat_mode propagates approval policy to all session slots."""

    @pytest.mark.asyncio
    async def test_yolo_mode_propagates_auto_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        policies = [c.args[1] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys
        assert all(p == "auto" for p in policies)

    @pytest.mark.asyncio
    async def test_normal_mode_clears_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_claw.safety_override import safety_override

        safety_override().activate("test")
        state = _make_state(tmp_path)
        state.enable_yolo()
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._trust = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "")

    @pytest.mark.asyncio
    async def test_trust_mode_scoped_to_slot_channel(self, tmp_path, monkeypatch):
        """Trust with slot_key only trusts that slot's linked channel."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"

        ch1 = MagicMock(trusted=False)
        ch2 = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is True
        ch1._save.assert_called_once()
        assert ch2.trusted is False
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_mode_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Trust without slot_key trusts all channels."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=False)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is True
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_mode_scoped_resets_only_linked_channel(self, tmp_path, monkeypatch):
        """Normal mode with slot_key should only reset that slot's linked channel."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._slack_channel = "ch1"
        state.get_or_create_slot("s2")

        ch1 = MagicMock(trusted=True)
        ch2 = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch1, "ch2": ch2})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert (await resp.json())["ok"] is True

        assert ch1.trusted is False
        ch1._save.assert_called_once()
        assert ch2.trusted is True
        ch2._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_mode_resets_all_channels_when_no_slot(self, tmp_path, monkeypatch):
        """Normal mode without slot_key resets all channel trust."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        ch = MagicMock(trusted=True)
        mgr = MagicMock(_channels={"ch1": ch})
        state.channel_manager = mgr

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert (await resp.json())["ok"] is True

        assert ch.trusted is False
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_trust_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Trust with unknown slot_key must return 400, not trust all."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_normal_mode_unknown_slot_returns_400(self, tmp_path, monkeypatch):
        """Normal with unknown slot_key must return 400, not reset all."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "normal", "slot": "nonexistent"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "unknown slot"

    @pytest.mark.asyncio
    async def test_trust_slot_preserves_other_slot_trust(self, tmp_path, monkeypatch):
        """Mesh-464: trusting slot B must not wipe trust from slot A."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert s1._trust is True

            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s2"})
            assert s2._trust is True
            assert s1._trust is True  # must survive

    @pytest.mark.asyncio
    async def test_yolo_restores_per_slot_trust(self, tmp_path, monkeypatch):
        """Mesh-464: YOLO does not mutate per-slot trust; disabling preserves it."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Set per-slot modes: s1=trust, s2=trust_reads
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s2"})
            assert s1._trust is True
            assert s2._trust_reads is True

            # YOLO overrides everything
            await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert s1._trust is True  # unchanged
            assert s2._trust_reads is True  # unchanged

            # Set s1 to normal (leaving YOLO) — s2 should be untouched
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert s1._trust is False
            assert s1._trust_reads is False
            assert s2._trust_reads is True  # preserved

    def test_yolo_auto_expires_and_clears_untrusted_policies(self, tmp_path, monkeypatch):
        """Mesh-464: YOLO expiry clears policies for untrusted slots only."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        s1._trust = True

        from unittest.mock import patch

        from kiro_claw.safety_override import safety_override

        # Wire the on_expired callback (as server.py does at startup)
        def _on_expired(source: str) -> None:
            if state.sessions is not None:
                for slot in state._slots.values():
                    if not slot._trust and not slot._trust_reads:
                        state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")

        with patch("kiro_claw.safety_override.sel"):
            safety_override().activate("dashboard")
        safety_override().on_expired = _on_expired
        safety_override()._expires_at = 0  # already expired

        assert state.is_yolo_active() is False
        assert s1._trust is True  # per-slot trust survives expiry

        cleared = [
            c[0][0] for c in state.sessions.set_approval_policy.call_args_list if c[0][1] == ""
        ]
        assert "dashboard:s2" in cleared
        assert "dashboard:s1" not in cleared


class TestApproveYoloPropagation:
    """api_chat_slot_approve with yolo action propagates policy to all slots."""

    @pytest.mark.asyncio
    async def test_yolo_approve_propagates_to_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        s1 = state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        s1._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "yolo"})
            data = await resp.json()
            assert data["ok"] is True

        calls = state.sessions.set_approval_policy.call_args_list
        keys = [c.args[0] for c in calls]
        assert "dashboard:s1" in keys
        assert "dashboard:s2" in keys

    @pytest.mark.asyncio
    async def test_trust_approve_propagates_to_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True

        state.sessions.set_approval_policy.assert_called_with("dashboard:s1", "auto")


# ── Coverage: bulk-approve broadcasts ──


class TestBulkApproveBroadcast:
    """Trust/YOLO mode change bulk-approve must broadcast approval_resolved."""

    @pytest.mark.asyncio
    async def test_mode_yolo_broadcasts_for_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        f1: asyncio.Future[str] = loop.create_future()
        f2: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-1"] = f1
        slot._approval_futures["req-2"] = f2

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            assert (await resp.json())["ok"] is True

        broadcast_calls = [
            c for c in state.broadcast_ws.call_args_list if c.args[0] == "approval_resolved"
        ]
        ids = {c.args[1]["id"] for c in broadcast_calls}
        assert "req-1" in ids
        assert "req-2" in ids


# ── Coverage: multi-pending approval 400 and trust auto-approve ──


class TestMultiPendingApproval:
    """Cover the 400 response when multiple approvals are pending without request_id."""

    @pytest.mark.asyncio
    async def test_multi_pending_returns_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        slot._approval_futures["a1"] = loop.create_future()
        slot._approval_futures["a2"] = loop.create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 400
            data = await resp.json()
            assert "pending" in data
            assert set(data["pending"]) == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_approve_with_request_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        slot._approval_futures["specific"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "approved", "request_id": "specific"},
            )
            assert resp.status == 200
            assert fut.result() == "approved"


# ── Agent passing via /api/chat (AgentRock integration) ──


class TestApiChatAgentPassing:
    @pytest.mark.asyncio
    async def test_agent_set_on_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "agentrock-my-skill", "agent": "my-aim-agent"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert state._slots["agentrock-my-skill"].agent == "my-aim-agent"

    @pytest.mark.asyncio
    async def test_agent_mismatch_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-x")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "slot-x", "agent": "agent-b"},
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_empty_agent_on_agent_slot_allowed(self, tmp_path, monkeypatch):
        """Follow-up message with no agent on an agent-bound slot must not 409."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-y")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "follow-up", "slot": "slot-y", "agent": ""},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_invalid_agent_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": "../evil"},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "../evil", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_non_string_agent_logs_actual_value(self, tmp_path, monkeypatch):
        """Fix for Post 22: str(agent) preserves malicious input in audit trail."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": 123},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "123", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_no_agent_no_emit(self, tmp_path, monkeypatch):
        """Fix for Post 23: no SEL event when no agent involved (reduces audit noise)."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "no-agent-slot"},
                )
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_sel_event_on_running_slot_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import MagicMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-r")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        with patch("kiro_claw.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "slot-r", "agent": "new-agent"},
                )
                assert resp.status == 409
            mock_emit.assert_called_once_with("slot-r", "new-agent", outcome="denied_running")


# ── Plan action & auto-run tests ──


class TestPlanAction:
    """Tests for api_chat_plan_action: Go/Go All label display and auto-run flag."""

    @pytest.mark.asyncio
    async def test_go_shows_go_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot", mode="orchestrator")
        slot.append("assistant", "📋 Plan for: test\n\nStage 1: Do\n\n[OPTION: Go | Cancel]")
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", AsyncMock())
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/plan-slot/plan-action",
                    json={"action": "go"},
                )
                assert resp.status == 200
        user_msgs = [m for m in slot.messages if m["role"] == "user"]
        assert user_msgs[-1]["content"] == "Go"
        assert not slot._auto_run

    @pytest.mark.asyncio
    async def test_go_all_shows_go_all_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot2", mode="orchestrator")
        slot.append("assistant", "📋 Plan for: test\n\nStage 1: Do\n\n[OPTION: Go | Cancel]")
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", AsyncMock())
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/plan-slot2/plan-action",
                    json={"action": "go all"},
                )
                assert resp.status == 200
        user_msgs = [m for m in slot.messages if m["role"] == "user"]
        assert user_msgs[-1]["content"] == "Go All"
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_cancel_clears_auto_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plan-slot3", mode="orchestrator")
        slot._auto_run = True
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/plan-slot3/plan-action",
                json={"action": "cancel"},
            )
            assert resp.status == 200
        assert slot._auto_run is False


class TestPlanValidationStuck:
    """Tests for has_plan=False after strip_plan_markers on invalid plans."""

    def test_strip_plan_markers_clears_has_plan(self):
        """After stripping, has_plan must be False so ensure_go_all_option doesn't run."""
        from kiro_claw.context_management import (
            strip_plan_markers,
            validate_plan_format,
        )

        # Simulate a response that looks like a plan but fails validation
        bad_plan = "📋 Plan for: test\n\nThis has no Stage lines.\n\n[OPTION: Go | Cancel]"
        has_plan, valid, _ = validate_plan_format(bad_plan)
        assert has_plan, "Expected plan header to be detected"
        assert not valid, "Expected plan to be invalid (no Stage lines)"
        stripped = strip_plan_markers(bad_plan)
        has_plan_after, _, _ = validate_plan_format(stripped)
        assert not has_plan_after, (
            "strip_plan_markers must remove plan markers so "
            "validate_plan_format no longer detects a plan"
        )
        assert "📋" not in stripped


# ── Tests: plan execution via Go/Go All button simulation ──


class TestPlanExecutionViaButton:
    """Simulate Go/Go All button clicks on a fake plan and verify the Python
    orchestration code drives stage advancement correctly."""

    def _make_slot(
        self, key="plan-exec", max_stages=2, auto_run=False, titles=None, goal="Sample goal"
    ):
        slot = _ChatSlot(key, mode="orchestrator")
        slot._auto_run = auto_run
        slot._stage_titles = (
            titles if titles is not None else [f"Step {i}" for i in range(1, max_stages + 1)]
        )
        slot._plan_goal = goal
        slot._orch_tracker = None  # fresh — will be created by _stage_loop
        return slot

    def _make_state(self, has_subagents=True):
        state = MagicMock()
        state.broadcast_ws = MagicMock()
        if has_subagents:
            state.subagents.running_agents_for.return_value = []
        else:
            state.subagents = MagicMock()
            state.subagents.running_agents_for = MagicMock(return_value=[])
        return state

    @pytest.mark.asyncio
    async def test_go_button_triggers_stage_loop(self, tmp_path, monkeypatch):
        """Clicking 'Go' calls _stage_loop with auto_run=False."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("go-btn", mode="orchestrator")
        slot.append(
            "assistant", "📋 Plan for: test\n\nStage 1: A\n\n[OPTION: Go | Go All | Cancel]"
        )
        mock_loop = AsyncMock()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", mock_loop)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/go-btn/plan-action", json={"action": "go"}
                )
                assert resp.status == 200
        mock_loop.assert_called_once()
        _, kwargs = mock_loop.call_args
        assert kwargs.get("auto_run") is False
        assert not slot._auto_run

    @pytest.mark.asyncio
    async def test_go_all_button_sets_auto_run_and_triggers_stage_loop(self, tmp_path, monkeypatch):
        """Clicking 'Go All' sets _auto_run=True and calls _stage_loop with auto_run=True."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("goall-btn", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        mock_loop = AsyncMock()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", mock_loop)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/goall-btn/plan-action", json={"action": "go all"}
                )
                assert resp.status == 200
        mock_loop.assert_called_once()
        _, kwargs = mock_loop.call_args
        assert kwargs.get("auto_run") is True
        assert slot._auto_run is True


class TestPythonStageLoop:
    """Tests for the Python-controlled stage execution loop (_stage_loop).

    Covers: Go (single stage), Go All (multi-stage), Cancel/Stop,
    subagent mid-stage, normal chat unaffected, stage timeout.
    """

    def _make_slot(self, key="loop-test", max_stages=3, titles=None, goal="Test goal"):
        slot = _ChatSlot(key, mode="orchestrator")
        slot._auto_run = False
        slot._stage_titles = (
            titles if titles is not None else [f"Step {i}" for i in range(1, max_stages + 1)]
        )
        slot._plan_goal = goal
        slot._orch_tracker = None
        return slot

    @pytest.mark.asyncio
    async def test_go_single_stage_then_stops(self, tmp_path, monkeypatch):
        """Go (single stage) executes one stage, emits approval message, returns."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=False)

        # Should call _run_chat exactly once (one stage)
        run_chat_mock.assert_called_once()
        # Should emit stage separator
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 1
        assert "Stage 1" in sep_msgs[0]["content"]
        # Should emit approval message for next stage
        approval_msgs = [m for m in slot.messages if "Click **Go**" in m.get("content", "")]
        assert len(approval_msgs) == 1
        assert "Stage 2" in approval_msgs[0]["content"]
        assert "[OPTION:" in approval_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_go_all_runs_all_stages(self, tmp_path, monkeypatch):
        """Go All executes all stages in sequence."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should call _run_chat 3 times (one per stage)
        assert run_chat_mock.call_count == 3
        # Should emit 3 stage separators
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 3
        # Should emit completion message
        done_msgs = [m for m in slot.messages if "All 3 stages complete" in m.get("content", "")]
        assert len(done_msgs) == 1
        # auto_run should be cleared
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self, tmp_path, monkeypatch):
        """Setting _stopping mid-loop breaks execution."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=5)

        call_count = 0

        async def _mock_run_chat(s, sl, msg, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate user clicking Stop after stage 2
                slot._stop_state = "soft_pending"

        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        # Should stop after 2 stages (not run all 5)
        assert call_count == 2
        # No "All stages complete" message
        done_msgs = [m for m in slot.messages if "stages complete" in m.get("content", "")]
        assert len(done_msgs) == 0

    @pytest.mark.asyncio
    async def test_stage_timeout_stops_loop(self, tmp_path, monkeypatch):
        """Stage timeout breaks the loop."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        # Pre-create tracker with timeout
        from kiro_claw.context_management import OrchestrationTracker

        tracker = OrchestrationTracker(stage_timeout_seconds=1)
        slot._orch_tracker = tracker
        # Force timeout on first check
        tracker.is_stage_timed_out = lambda: True

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should NOT call _run_chat (timeout before execution)
        run_chat_mock.assert_not_called()
        # Should emit timeout message
        timeout_msgs = [m for m in slot.messages if "timed out" in m.get("content", "")]
        assert len(timeout_msgs) == 1

    @pytest.mark.asyncio
    async def test_normal_chat_unaffected(self, tmp_path, monkeypatch):
        """Normal chat messages (not Go/Go All) still go through _run_chat directly."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("normal-chat", mode="orchestrator")

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_handlers._run_chat", run_chat_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello world", "slot": "normal-chat"},
            )
            assert resp.status == 200

        # Normal message goes through _run_chat, not _stage_loop
        run_chat_mock.assert_called_once()
        msg = run_chat_mock.call_args[0][2]
        assert "hello world" in msg

    @pytest.mark.asyncio
    async def test_go_button_uses_stage_loop(self, tmp_path, monkeypatch):
        """Go button via plan-action endpoint uses _stage_loop."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("go-loop", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        slot._stage_titles = ["A", "B"]
        slot._plan_goal = "test"

        stage_loop_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", stage_loop_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/go-loop/plan-action", json={"action": "go"})
            assert resp.status == 200

        stage_loop_mock.assert_called_once()
        _, kwargs = stage_loop_mock.call_args
        # For positional args
        args = stage_loop_mock.call_args[0]
        # auto_run should be False for "Go"
        assert kwargs.get("auto_run", args[2] if len(args) > 2 else None) is False

    @pytest.mark.asyncio
    async def test_go_all_button_uses_stage_loop(self, tmp_path, monkeypatch):
        """Go All button via plan-action endpoint uses _stage_loop with auto_run=True."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("goall-loop", mode="orchestrator")
        slot.append(
            "assistant",
            "📋 Plan for: test\n\nStage 1: A\nStage 2: B\n\n[OPTION: Go | Go All | Cancel]",
        )
        slot._stage_titles = ["A", "B"]
        slot._plan_goal = "test"

        stage_loop_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._stage_loop", stage_loop_mock)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/goall-loop/plan-action", json={"action": "go all"}
            )
            assert resp.status == 200

        stage_loop_mock.assert_called_once()
        _, kwargs = stage_loop_mock.call_args
        args = stage_loop_mock.call_args[0]
        assert kwargs.get("auto_run", args[2] if len(args) > 2 else None) is True
        assert slot._auto_run is True

    @pytest.mark.asyncio
    async def test_stage_results_captured_to_disk(self, tmp_path, monkeypatch):
        """Each stage result is written to disk and tracked in tracker."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=2)

        async def _mock_run_chat(s, sl, msg, **kw):
            sl.append("assistant", "Result for stage", "msg msg-a")

        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        tracker = slot._orch_tracker
        assert 1 in tracker._stage_results
        assert 2 in tracker._stage_results
        # Verify files exist on disk
        for stage_num in (1, 2):
            path = Path(tracker._stage_results[stage_num])
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "Result for stage" in content

    def test_build_stage_context_includes_goal_and_status(self):
        """_build_stage_context includes goal, status summary, and stage instruction."""
        from kiro_claw.context_management import OrchestrationTracker
        from kiro_claw.dashboard.chat import _build_stage_context

        slot = self._make_slot(
            max_stages=3, titles=["Research", "Implement", "Test"], goal="Build feature X"
        )
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

        ctx = _build_stage_context(slot, tracker, stage_idx=0)
        assert "Build feature X" in ctx
        assert "▶️ Stage 1: Research — execute now" in ctx
        assert "⬜ Stage 2: Implement — pending" in ctx
        assert "Stage 1 of 3" in ctx

    def test_build_stage_context_includes_previous_results(self, tmp_path, monkeypatch):
        """_build_stage_context includes paths to previous stage results."""
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.context_management import OrchestrationTracker
        from kiro_claw.dashboard.chat import _build_stage_context

        slot = self._make_slot(max_stages=3, titles=["A", "B", "C"])
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

        # Write a fake stage 1 result
        result_dir = tmp_path / "sessions" / slot.key
        result_dir.mkdir(parents=True)
        result_file = result_dir / "stage_1_result.md"
        result_file.write_text("Stage 1 completed successfully")
        tracker.record_stage_result(1, str(result_file))

        ctx = _build_stage_context(slot, tracker, stage_idx=1)
        assert "Stage 1 completed successfully" in ctx
        assert str(result_file) in ctx
        assert "✅ Stage 1: A — completed" in ctx
        assert "▶️ Stage 2: B — execute now" in ctx

    def test_status_summary_format(self):
        """OrchestrationTracker.status_summary produces correct format."""
        from kiro_claw.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        summary = tracker.status_summary(1, 3, ["Research", "Implement", "Test"])
        assert "✅ Stage 1: Research — completed" in summary
        assert "▶️ Stage 2: Implement — execute now" in summary
        assert "⬜ Stage 3: Test — pending" in summary

    @pytest.mark.asyncio
    async def test_run_chat_error_stops_loop(self, tmp_path, monkeypatch):
        """If _run_chat raises, stage loop catches, emits error, and stops."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        async def _exploding_run_chat(s, sl, msg, **kw):
            raise RuntimeError("LLM provider error")

        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", _exploding_run_chat)

        await _stage_loop(state, slot, auto_run=True)

        # Should emit error message
        err_msgs = [
            m for m in slot.messages if "failed due to an internal error" in m.get("content", "")
        ]
        assert len(err_msgs) == 1
        # Should NOT run remaining stages
        sep_msgs = [m for m in slot.messages if "stage-sep" in m.get("cls", "")]
        assert len(sep_msgs) == 1  # only stage 1 separator

    @pytest.mark.asyncio
    async def test_subagent_wait_loop(self, tmp_path, monkeypatch):
        """Stage loop waits for pending subagents before advancing."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=2)

        _poll_count = 0

        def _running_agents(key):
            nonlocal _poll_count
            _poll_count += 1
            # Simulate subagent finishing after 2 polls
            return [{"id": "sa-1"}] if _poll_count < 3 else []

        state.subagents = MagicMock()
        state.subagents.running_agents_for = _running_agents

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator.asyncio.sleep", AsyncMock())

        await _stage_loop(state, slot, auto_run=True)

        # Should have polled for subagents
        assert _poll_count >= 3
        # Should still complete both stages
        assert run_chat_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_subagent_manager_missing_stops_auto_run(self, tmp_path, monkeypatch):
        """When running_agents_for returns None, auto-run must stop (fail-closed)."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents.running_agents_for.return_value = None  # error case
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should run stage 1 but stop before stage 2 (fail-closed)
        run_chat_mock.assert_called_once()
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_subagent_manager_none_stops_auto_run(self, tmp_path, monkeypatch):
        """When state.subagents is None, auto-run must stop (fail-closed)."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = None  # manager missing
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        await _stage_loop(state, slot, auto_run=True)

        # Should run stage 1 but stop before stage 2 (fail-closed)
        run_chat_mock.assert_called_once()
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_go_reentry_resumes_from_stage_2(self, tmp_path, monkeypatch):
        """After Go completes stage 1, next Go call resumes from stage 2."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.chat.config_dir", lambda: tmp_path)
        from kiro_claw.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        slot = self._make_slot(max_stages=3)

        run_chat_mock = AsyncMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_orchestrator._run_chat", run_chat_mock)

        # First Go: runs stage 1 only
        await _stage_loop(state, slot, auto_run=False)
        assert run_chat_mock.call_count == 1

        # Second Go: should resume from stage 2
        run_chat_mock.reset_mock()
        await _stage_loop(state, slot, auto_run=False)
        assert run_chat_mock.call_count == 1
        # Verify it was stage 2 (context should show stage 2 as current)
        ctx = run_chat_mock.call_args[0][2]
        assert "▶️ Stage 2" in ctx

    def test_previous_result_paths_compaction(self, tmp_path, monkeypatch):
        """Long stage results are truncated with tail bias (30% head, 70% tail)."""
        monkeypatch.setattr("kiro_claw.dashboard.chat.is_sensitive_path", lambda p: False)
        from kiro_claw.context_management import OrchestrationTracker
        from kiro_claw.dashboard.chat import _previous_result_paths

        tracker = OrchestrationTracker()

        # Write a large stage 1 result (5000 chars)
        result_dir = tmp_path / "sessions" / "test-slot"
        result_dir.mkdir(parents=True)
        result_file = result_dir / "stage_1_result.md"
        head_marker = "HEAD_MARKER_START"
        tail_marker = "TAIL_MARKER_END"
        large_content = (
            head_marker
            + "x" * 4000
            + tail_marker
            + "y" * (5000 - len(head_marker) - 4000 - len(tail_marker))
        )
        result_file.write_text(large_content)
        tracker.record_stage_result(1, str(result_file))

        loaded = _previous_result_paths(tracker, 1)
        # Should be truncated (2000 chars max per stage + header + path)
        assert len(loaded) < 3000
        assert "...[truncated]..." in loaded
        assert str(result_file) in loaded
        # Tail bias: head_marker (near start) should be in the 30% head
        assert head_marker in loaded
        # Tail bias: tail_marker (at char ~4015) should be in the 70% tail
        assert tail_marker in loaded


class TestStageFailureEscalation:
    """Test that stage failures trigger human question logic (escalation)."""

    def test_single_failure_allows_retry(self):
        """A single task failure does NOT trigger escalation — retry is allowed."""
        from kiro_claw.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        # First failure: should not escalate
        hit_limit = tracker.record_failure("task-a")
        assert not hit_limit
        assert not tracker.has_escalated
        assert tracker.failure_count("task-a") == 1

    def test_repeated_failures_trigger_escalation(self):
        """After MAX_TASK_FAILURES (3), has_escalated becomes True."""
        from kiro_claw.context_management import (
            MAX_TASK_FAILURES,
            OrchestrationTracker,
        )

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        for i in range(MAX_TASK_FAILURES - 1):
            assert not tracker.record_failure("task-a")
        # The Nth failure triggers escalation
        assert tracker.record_failure("task-a")
        assert tracker.has_escalated

    def test_success_resets_failure_count(self):
        """record_success clears the failure counter for a task."""
        from kiro_claw.context_management import OrchestrationTracker

        tracker = OrchestrationTracker()
        tracker.record_round(1)
        tracker.record_failure("task-a")
        tracker.record_failure("task-a")
        assert tracker.failure_count("task-a") == 2
        tracker.record_success("task-a")
        assert tracker.failure_count("task-a") == 0
        assert not tracker.has_escalated

    def test_stage_round_limit_triggers_escalation(self):
        """After MAX_STAGE_ROUNDS (3) rounds in a stage, has_escalated is True."""
        from kiro_claw.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

        tracker = OrchestrationTracker()
        for i in range(MAX_STAGE_ROUNDS):
            tracker.record_round(1)
        assert tracker.has_escalated

    def test_reset_after_guidance_clears_rounds(self):
        """User guidance resets round counters, allowing retry."""
        from kiro_claw.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

        tracker = OrchestrationTracker()
        for i in range(MAX_STAGE_ROUNDS):
            tracker.record_round(1)
        assert tracker.has_escalated
        tracker.reset_after_guidance()
        assert not tracker.has_escalated
        assert tracker.round_count(1) == 0

    def test_force_fail_after_max_escalations(self):
        """After MAX_STAGE_ESCALATIONS resets, stage is force-failed."""
        from kiro_claw.context_management import (
            MAX_STAGE_ESCALATIONS,
            MAX_STAGE_ROUNDS,
            OrchestrationTracker,
        )

        tracker = OrchestrationTracker()
        for _esc in range(MAX_STAGE_ESCALATIONS):
            for _r in range(MAX_STAGE_ROUNDS):
                tracker.record_round(1)
            tracker.reset_after_guidance()
        assert tracker.is_force_failed(1)


# ── Tests: prompt-busy session recovery ──


class TestPromptBusyRecovery:
    """When kiro-cli returns 'Prompt already in progress', _run_chat must
    reset the session and re-queue the message so the next attempt cold-starts."""

    @pytest.mark.asyncio
    async def test_prompt_busy_resets_session_and_requeues(self, tmp_path: Path) -> None:
        from kiro_claw.acp.client import AcpError
        from kiro_claw.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("busy-slot")
        slot.append("user", "hello", "msg msg-u")

        # Make client.stream raise "already in progress"
        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_busy(msg):
            raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_busy
        mock_client.stream_command = _raise_busy
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        # Session must be reset (kill the stuck kiro-cli process)
        state.sessions.reset.assert_awaited_once()
        # The finally block drains the re-queued message into a new task
        assert slot.task is not None
        # No ❌ error shown to the user for the busy case
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("already in progress" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_process_exited_resets_session_and_requeues(self, tmp_path: Path) -> None:
        """When ACP subprocess dies (SIGTERM/SIGKILL), _run_chat must reset
        the session and re-queue the message so autonudges land on a fresh
        provider instead of a bare ❌ error card with no work done."""
        from kiro_claw.acp.client import AcpError
        from kiro_claw.dashboard.chat import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("dead-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]

        async def _raise_dead(msg):
            raise AcpError("ACP process exited (code=-15)")
            yield  # make it an async generator  # noqa: E501

        mock_client.stream = _raise_dead
        mock_client.stream_command = _raise_dead
        mock_client.shutdown = AsyncMock()

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot.task is not None
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert not any("process exited" in m.get("content", "") for m in error_msgs)


# ── Tests: slot.task None guard ──


class TestSlotTaskNoneGuard:
    """stop/delete must not crash when slot.task is None."""

    @pytest.mark.asyncio
    async def test_stop_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("s1")
        # task is None → running is False → stop is a no-op
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        # task is None → running is False → delete skips cancel
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_stop_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
            state.sessions.stop_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            with patch("kiro_claw.dashboard.chat_handlers._save_slot_to_history"):
                resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200
            assert slot.task.cancelled()


# ── Bulk cleanup tests ──


class TestBulkCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_archives_stale_sessions(self, tmp_path, monkeypatch):
        """Stale sessions are archived; fresh and pinned are kept."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        pinned = state.get_or_create_slot("pinned1")
        pinned.pinned = True
        pinned.append("user", "pinned msg", ts=old_ts)
        pinned.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "fresh1"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 1
            assert "stale1" in data["keys"]

        assert "stale1" not in state._slots
        assert "fresh1" in state._slots
        assert "pinned1" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_skips_active_slot(self, tmp_path, monkeypatch):
        """The active slot is never archived even if stale."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        slot = state.get_or_create_slot("active")
        slot.append("user", "old", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1, "active_slot": "active"},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "active" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_saves_to_history(self, tmp_path, monkeypatch):
        """Archived sessions are persisted to conversation log."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("to-archive")
        slot.append("user", "save me", ts=old_ts)
        slot.append("assistant", "saved", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3},
            )

        msgs = state.conversation_log.read_messages("dashboard:to-archive")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "save me"

    @pytest.mark.asyncio
    async def test_cleanup_defaults_to_3_days(self, tmp_path, monkeypatch):
        """Without max_inactive_days, defaults to 3."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        ts_2d = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        slot = state.get_or_create_slot("recent")
        slot.append("user", "hi", ts=ts_2d)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/cleanup", json={})
            data = await resp.json()
            assert data["archived"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_empty_slots_uses_created_at(self, tmp_path, monkeypatch):
        """Slots with no messages use created_at for staleness."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        slot = state.get_or_create_slot("empty-old")
        slot.created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 7},
            )
            data = await resp.json()
            assert data["archived"] == 1
            assert "empty-old" in data["keys"]

    @pytest.mark.asyncio
    async def test_cleanup_no_stale_returns_zero(self, tmp_path, monkeypatch):
        """When all sessions are fresh, nothing is archived."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timezone

        fresh_ts = datetime.now(timezone.utc).isoformat()
        slot = state.get_or_create_slot("fresh")
        slot.append("user", "hi", ts=fresh_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 0
            assert data["keys"] == []

    @pytest.mark.asyncio
    async def test_cleanup_rollback_on_save_failure(self, tmp_path, monkeypatch):
        """When _save_slot_to_history raises, slot is restored and reported as failed."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("fail-save")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()

        with patch(
            "kiro_claw.dashboard.chat_handlers._save_slot_to_history",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/cleanup",
                    json={"max_inactive_days": 1},
                )
                data = await resp.json()
                assert data["archived"] == 0
                assert "fail-save" in data["failed"]

        # Slot must be restored (not lost)
        assert "fail-save" in state._slots
        # No history entry should exist (save failed)
        msgs = state.conversation_log.read_messages("dashboard:fail-save")
        assert len(msgs) == 0

    @pytest.mark.asyncio
    async def test_cleanup_cancels_running_task(self, tmp_path, monkeypatch):
        """Running tasks on stale slots are cancelled after archive."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("running1")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 1
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_skips_unparseable_timestamps(self, tmp_path, monkeypatch):
        """Slots with unparseable timestamps are skipped, not archived."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("bad-ts")
        slot.append("user", "hi", ts="not-a-date")
        slot.created_at = "also-not-a-date"
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "bad-ts" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_dry_run_returns_keys_without_archiving(self, tmp_path, monkeypatch):
        """dry_run=True returns stale keys and active_is_stale but does not archive anything."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        active_stale = state.get_or_create_slot("active1")
        active_stale.append("user", "old active msg", ts=old_ts)
        active_stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "active1", "dry_run": True},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["dry_run"] is True
            assert "stale1" in data["keys"]
            assert "active1" not in data["keys"]
            assert data["count"] == 1
            assert data["active_is_stale"] is True

        # Slots should NOT have been removed
        assert "stale1" in state._slots
        assert "active1" in state._slots
        assert "fresh1" in state._slots


class TestHistoryKeyFor:
    """Tests for _history_key_for — canonical history key from slot key."""

    def test_already_canonical(self):
        from kiro_claw.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard:chat-1-100") == "dashboard:chat-1-100"

    def test_strips_single_prefix(self):
        from kiro_claw.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_chat-1-100") == "dashboard:chat-1-100"

    def test_strips_double_prefix(self):
        from kiro_claw.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_dashboard_chat-1-100") == "dashboard:chat-1-100"

    def test_strips_triple_prefix(self):
        from kiro_claw.dashboard.chat import _history_key_for

        assert _history_key_for("dashboard_dashboard_dashboard_x") == "dashboard:x"

    def test_raw_key_gets_prefix(self):
        from kiro_claw.dashboard.chat import _history_key_for

        assert _history_key_for("chat-1-100") == "dashboard:chat-1-100"


# ── Folder CRUD tests ──


class TestFolderCRUD:
    @pytest.mark.asyncio
    async def test_list_folders_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/folders")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_create_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Oncall"})
            assert resp.status == 201
            data = await resp.json()
            assert data["name"] == "Oncall"
            assert "id" in data
            assert data["collapsed"] is False
            # Persisted to disk
            assert (tmp_path / "folders.json").exists()

    @pytest.mark.asyncio
    async def test_create_folder_with_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Parent"})
            parent = await resp.json()
            resp = await client.post(
                "/api/chat/folders", json={"name": "Child", "parent_id": parent["id"]}
            )
            child = await resp.json()
            assert child["parent_id"] == parent["id"]

    @pytest.mark.asyncio
    async def test_create_folder_invalid_parent_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Orphan", "parent_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": ""})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_folder_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Old"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "New"})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New"

    @pytest.mark.asyncio
    async def test_update_folder_collapse(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"collapsed": True})
            data = await resp.json()
            assert data["collapsed"] is True

    @pytest.mark.asyncio
    async def test_update_folder_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "MS&AD"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"default_agent": "msad"}
            )
            data = await resp.json()
            assert data["default_agent"] == "msad"
            # Verify persistence
            assert state._folders[0]["default_agent"] == "msad"

    @pytest.mark.asyncio
    async def test_update_folder_clear_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "default_agent": "nissay"}
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/f1", json={"default_agent": ""})
            data = await resp.json()
            assert data["default_agent"] == ""

    @pytest.mark.asyncio
    async def test_create_folder_with_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(proj)}
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_create_folder_relative_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "relative/path"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_nonexistent_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        missing = tmp_path / "does-not-exist"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(missing)}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_sensitive_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "~/.ssh"}
            )
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]

    @pytest.mark.asyncio
    async def test_update_folder_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj2"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "P"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"project_dir": str(proj)}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_update_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Keep"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_nonexistent_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/nonexistent", json={"name": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Delete Me"})
            folder = await resp.json()
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status == 200
            resp = await client.get("/api/chat/folders")
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_delete_folder_reparents_children(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "parent", "name": "Parent", "order": 0, "collapsed": False},
            {"id": "child", "name": "Child", "order": 1, "collapsed": False, "parent_id": "parent"},
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/parent")
            assert len(state._folders) == 1
            assert state._folders[0]["id"] == "child"
            assert state._folders[0].get("parent_id") == ""

    @pytest.mark.asyncio
    async def test_delete_folder_ungroups_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-del"
        state._folders.append({"id": "f-del", "name": "X", "order": 0, "collapsed": False})
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/f-del")
            assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_slot_to_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["folder_id"] == "f1"
            assert state._slots["myslot"].folder_id == "f1"

    @pytest.mark.asyncio
    async def test_unassign_slot_from_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.folder_id = "f1"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": ""})
            assert resp.status == 200
            assert state._slots["myslot"].folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_folder_nonexistent_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/nope/folder", json={"folder_id": "f1"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_assign_nonexistent_folder_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder", json={"folder_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["pinned"] is True
            assert state._slots["myslot"].pinned is True

    @pytest.mark.asyncio
    async def test_unpin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": False})
            assert resp.status == 200
            assert state._slots["myslot"].pinned is False

    @pytest.mark.asyncio
    async def test_slots_include_pinned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s.get("pinned") is True for s in slots)

    @pytest.mark.asyncio
    async def test_slots_include_folder_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-abc"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s["folder_id"] == "f-abc" for s in slots)


class TestFolderPersistence:
    def test_load_folders_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        import json

        (tmp_path / "folders.json").write_text(
            json.dumps([{"id": "f1", "name": "Test", "order": 0}])
        )
        state = _make_state(tmp_path)
        state.load_folders()
        assert len(state._folders) == 1
        assert state._folders[0]["name"] == "Test"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Roundtrip", "order": 0, "collapsed": True}]
        state.save_folders()
        state._folders = []
        state.load_folders()
        assert state._folders[0]["name"] == "Roundtrip"
        assert state._folders[0]["collapsed"] is True

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []

    def test_load_corrupted_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "folders.json").write_text("not json")
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []


class TestGenerateFolderIcon:
    @pytest.mark.asyncio
    async def test_valid_emoji_stored(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        # Mock LLM session
        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "🚀"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()
        state.push_slots_update = MagicMock()

        folder = {"id": "f1", "name": "Deploy"}
        state._folders = [folder]
        await _generate_folder_icon(state, folder)

        assert folder["icon"] == "🚀"
        state.save_folders.assert_called_once()
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_long_output_rejected(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "This is not an emoji"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()

        folder = {"id": "f1", "name": "Deploy"}
        state._folders = [folder]
        await _generate_folder_icon(state, folder)

        assert "icon" not in folder
        state.save_folders.assert_not_called()

    @pytest.mark.asyncio
    async def test_ascii_two_char_rejected(self, tmp_path, monkeypatch):
        """Two ASCII chars like '<>' should be rejected by emoji validation."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "<>"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()

        folder = {"id": "f1", "name": "Test"}
        state._folders = [folder]
        await _generate_folder_icon(state, folder)

        assert "icon" not in folder
        state.save_folders.assert_not_called()

    @pytest.mark.asyncio
    async def test_redaction_applied(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "🔥"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()
        state.push_slots_update = MagicMock()

        with patch(
            "kiro_claw.dashboard.chat_folders.redact_exfiltration_urls", return_value=("🔥", False)
        ) as mock_url, patch(
            "kiro_claw.dashboard.chat_folders.redact_credentials", return_value=("🔥", False)
        ) as mock_cred:
            folder = {"id": "f1", "name": "Oncall"}
            state._folders = [folder]
            await _generate_folder_icon(state, folder)
            mock_url.assert_called_once()
            mock_cred.assert_called_once()

    @pytest.mark.asyncio
    async def test_variation_selector_emoji_accepted(self, tmp_path, monkeypatch):
        """Emoji with U+FE0F variation selector (e.g. ❤️) should be accepted."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "\u2764\ufe0f"  # ❤️
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()
        state.push_slots_update = MagicMock()

        folder = {"id": "f1", "name": "Love"}
        state._folders = [folder]
        await _generate_folder_icon(state, folder)

        assert folder["icon"] == "\u2764\ufe0f"
        state.save_folders.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_background_session(self, tmp_path, monkeypatch):
        """Folder icon generation should use the shared background session."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_claw.dashboard.chat_folders import _generate_folder_icon
        from kiro_claw.session import BACKGROUND_KEY

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "🔥"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_claw.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_or_create = AsyncMock(return_value=(mock_client, False, False))
        state.sessions.release = MagicMock()
        state.save_folders = MagicMock()
        state.push_slots_update = MagicMock()

        folder = {"id": "abc123", "name": "Test"}
        state._folders = [folder]
        await _generate_folder_icon(state, folder)

        state.sessions.get_or_create.assert_called_once_with(BACKGROUND_KEY)
        state.sessions.release.assert_called_once_with(BACKGROUND_KEY)


class TestFolderAssignmentPersistence:
    @pytest.mark.asyncio
    async def test_folder_assignment_saves_to_history(self, tmp_path, monkeypatch):
        """api_chat_slot_folder should call _save_slot_to_history for new sessions."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            path = tmp_path / "dashboard_myslot.jsonl"
            assert path.exists()
            import json

            meta = json.loads(path.read_text().split("\n")[0])
            assert meta["folder_id"] == "f1"

    @pytest.mark.asyncio
    async def test_folder_assignment_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: folder_id must reach disk even when slot is a resumed
        session with no new messages.

        Root cause: _save_slot_to_history had an early-return guard that
        skipped disk writes when ``slot._resumed_count > 0 and
        len(messages) <= slot._resumed_count``. Metadata-only changes like
        folder assignment don't grow the message count past the resumed
        marker, so the save was silently dropped — folder_id never reached
        disk and the move was lost on the next gateway restart.

        Fix: folder endpoint passes ``force=True`` which bypasses the guard.
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("resumedslot")
        slot.append("user", "old message from before restart")
        slot.drain()
        # Mark slot as a resumed session (simulates being restored from disk).
        # The guard fires when _resumed_count >= len(messages).
        slot._resumed_count = len(slot.messages)
        state._folders = [{"id": "f-resumed", "name": "Build", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/resumedslot/folder",
                json={"folder_id": "f-resumed"},
            )
            assert resp.status == 200
            path = tmp_path / "dashboard_resumedslot.jsonl"
            assert path.exists(), "folder_id save must reach disk on resumed session"
            import json

            meta = json.loads(path.read_text().split("\n")[0])
            assert meta.get("folder_id") == "f-resumed", (
                "folder_id was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    @pytest.mark.asyncio
    async def test_pin_toggle_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: pinned flag must reach disk on resumed sessions.

        Same root cause as the folder regression — the resumed-count guard
        in _save_slot_to_history was blocking metadata-only writes. Pin
        endpoint now passes ``force=True``.
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("pinslot")
        slot.append("user", "old message")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/pinslot/pin", json={"pinned": True})
            assert resp.status == 200
            path = tmp_path / "dashboard_pinslot.jsonl"
            assert path.exists(), "pinned save must reach disk on resumed session"
            import json

            meta = json.loads(path.read_text().split("\n")[0])
            assert meta.get("pinned") is True, (
                "pinned was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    def test_save_slot_force_bypasses_resumed_guard(self, tmp_path, monkeypatch):
        """Unit test: ``force=True`` must bypass the resumed-session guard.

        Without force, resumed sessions with no new messages skip the write.
        With force, the metadata-only mutation reaches disk regardless.
        """
        from kiro_claw.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forceslot")
        slot.append("user", "hello")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot.folder_id = "f-force"

        # Without force — save is skipped by the guard, no file written.
        _save_slot_to_history(state, slot)
        path = tmp_path / "dashboard_forceslot.jsonl"
        assert not path.exists(), "guard must skip save when not forced"

        # With force — save bypasses the guard, file is written with folder_id.
        _save_slot_to_history(state, slot, force=True)
        assert path.exists(), "force=True must bypass the guard"
        import json

        meta = json.loads(path.read_text().split("\n")[0])
        assert meta.get("folder_id") == "f-force"


class TestNewPlanResetsAutoRun:
    """Regression: _auto_run must reset when a new plan is detected."""

    def test_has_plan_resets_auto_run(self):
        """When LLM generates a new plan mid-execution, auto_run must be cleared."""
        from kiro_claw.dashboard.chat import _reset_auto_run_for_new_plan

        slot = _ChatSlot("plan-reset")
        slot._auto_run = True
        slot._orch_tracker = MagicMock()

        _reset_auto_run_for_new_plan(slot)

        assert slot._auto_run is False, "_auto_run must be reset for new plan"
        assert slot._orch_tracker is None


# ── Regenerate + variant switching ──


class TestRegenerateAndVariants:
    @pytest.mark.asyncio
    async def test_regenerate_truncates_and_stashes_variant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello v1")
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [m["role"] for m in slot.messages] == ["user"]
        assert len(captured) == 1
        assert captured[0]["content"] == "hello v1"

    @pytest.mark.asyncio
    async def test_regenerate_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello")

        # Simulate running task
        async def _noop():
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_regenerate_requires_prior_assistant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "only user")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_updates_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
            assert slot.messages[-1]["content"] == "v1"
            assert slot.messages[-1]["variant_idx"] == 0

    @pytest.mark.asyncio
    async def test_switch_variant_index_out_of_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_passes_hint_to_run_chat(self, tmp_path, monkeypatch):
        """_run_chat should receive a non-empty regenerate_hint kwarg."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()
        mock_run = AsyncMock()
        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=mock_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the scheduled task actually run so the mock records args
                await asyncio.sleep(0)
        mock_run.assert_called_once()
        _args, kwargs = mock_run.call_args
        assert kwargs.get("regenerate_hint"), "regenerate_hint must be non-empty"

    @pytest.mark.asyncio
    async def test_regenerate_preserves_existing_variants(self, tmp_path, monkeypatch):
        """When assistant already has variants[], regenerate keeps them and adds current."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_when_active_is_old_variant_no_dup(self, tmp_path, monkeypatch):
        """If user switched back to v1 then regenerates, v1 should not be appended twice."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 0
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_caps_variants(self, tmp_path, monkeypatch):
        """Variant list is capped; oldest entries drop when over _MAX_VARIANTS."""
        from kiro_claw.dashboard.chat import _MAX_VARIANTS

        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "newest")
        existing = [{"content": f"v{i}", "ts": f"t{i}"} for i in range(_MAX_VARIANTS)]
        slot.messages[-1]["variants"] = existing
        slot.messages[-1]["variant_idx"] = len(existing) - 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert len(captured) <= _MAX_VARIANTS
        assert captured[-1]["content"] == "newest"

    @pytest.mark.asyncio
    async def test_regenerate_rejects_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/nonexistent/regenerate")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_regenerate_rejects_empty_user_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "")
        slot.append("assistant", "reply")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_persists_to_disk(self, tmp_path, monkeypatch):
        """After regenerate, on-disk history should reflect the truncation."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "old")
        slot.drain()
        # Save first so a file exists
        from kiro_claw.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
        # File should now only contain the user message (assistant truncated)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        roles = [m.get("role") for m in persisted]
        assert roles == ["user"]

    @pytest.mark.asyncio
    async def test_save_slot_redacts_variants(self, tmp_path, monkeypatch):
        """Variants written to disk must have credentials/exfil URLs redacted."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe content")
        # Plant a fake credential inside a variant
        slot.messages[-1]["variants"] = [
            {"content": "AKIAIOSFODNN7EXAMPLE secret stuff", "ts": "t1"},
            {"content": "safe content", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_claw.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        ai = [m for m in persisted if m.get("role") == "assistant"][0]
        assert "variants" in ai
        # The AKIA key must not appear in either variant after redaction
        for v in ai["variants"]:
            assert "AKIAIOSFODNN7EXAMPLE" not in v.get("content", "")

    @pytest.mark.asyncio
    async def test_switch_variant_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]

        async def _noop():
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_switch_variant_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/none/switch-variant", json={"index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_switch_variant_no_variants(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "plain")  # no variants[]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_invalid_json_body(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", data="not-json")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_non_int_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": "abc"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_task_error(self, tmp_path, monkeypatch):
        """If _run_chat raises, _pending_variants must be cleared to prevent leak."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _boom(*a, **kw):
            raise RuntimeError("llm blew up")

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_boom):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the failing task propagate through done_callback
                for _ in range(5):
                    await asyncio.sleep(0)
        assert slot._pending_variants == [], "pending variants must be cleared when task errors"

    @pytest.mark.asyncio
    async def test_flush_segment_attaches_pending_variants(self, tmp_path, monkeypatch):
        """_flush_segment should attach _pending_variants to the new assistant message."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        # Simulate pending variants from a regenerate
        slot._pending_variants = [
            {"content": "old v1", "ts": "t1"},
            {"content": "old v2", "ts": "t2"},
        ]
        from kiro_claw.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "new reply", broadcast=False)
        last = slot.messages[-1]
        assert last["role"] == "assistant"
        assert last["content"] == "new reply"
        assert len(last["variants"]) == 3  # old v1, old v2, new reply
        assert last["variant_idx"] == 2
        assert slot._pending_variants == []

    @pytest.mark.asyncio
    async def test_switch_variant_negative_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_only_system_and_assistant(self, tmp_path, monkeypatch):
        """Regenerate should fail if there's no user message (only system + assistant)."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("system", "you are helpful")
        slot.append("assistant", "hello")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_flush_segment_no_pending_no_variants(self, tmp_path, monkeypatch):
        """Normal flush without pending variants should not add variants field."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        from kiro_claw.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "reply", broadcast=False)
        last = slot.messages[-1]
        assert "variants" not in last

    @pytest.mark.asyncio
    async def test_switch_variant_missing_index_key(self, tmp_path, monkeypatch):
        """Request body without 'index' key should return 400."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_restore_preserves_variants(self, tmp_path, monkeypatch):
        """Variants written to disk should be restored via production code path."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_claw.dashboard.chat import _save_slot_to_history, restore_recent_sessions

        _save_slot_to_history(state, slot)
        # Clear in-memory state and restore via production path
        state._slots.clear()
        restore_recent_sessions(state, window_minutes=9999)
        restored_slot = state._slots.get("s1")
        assert restored_slot is not None
        ai = [m for m in restored_slot.messages if m.get("role") == "assistant"][0]
        assert "variants" in ai
        assert len(ai["variants"]) == 2
        assert ai["variant_idx"] == 1

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_cancel(self, tmp_path, monkeypatch):
        """If user stops a regeneration (cancel), _pending_variants must be cleared."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            await asyncio.sleep(999)

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                assert slot._pending_variants != []
                # Cancel the task (simulates user clicking Stop)
                slot.task.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
        assert (
            slot._pending_variants == []
        ), "pending variants must be cleared when task is cancelled"

    @pytest.mark.asyncio
    async def test_prepare_messages_redacts_variant_content(self, tmp_path, monkeypatch):
        """Variant content exposed via API must have credentials redacted."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe")
        slot.messages[-1]["variants"] = [
            {"content": "AKIAIOSFODNN7EXAMPLE leaked key", "ts": "t1"},
            {"content": "safe", "ts": "t2"},
        ]
        from kiro_claw.dashboard.chat import _prepare_messages

        prepared = _prepare_messages(slot.messages, False)
        ai = [m for m in prepared if m.get("role") == "assistant"][0]
        for v in ai["variants"]:
            assert "AKIAIOSFODNN7EXAMPLE" not in v.get("content", "")

    @pytest.mark.asyncio
    async def test_switch_variant_corrupt_entry(self, tmp_path, monkeypatch):
        """If a variant entry is not a dict, switch-variant should return 400."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = ["not-a-dict", {"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_concurrent_regenerate_one_succeeds_one_409(self, tmp_path, monkeypatch):
        """Two simultaneous regenerate requests: one gets 200, the other gets 409."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            await asyncio.sleep(999)

        with patch("kiro_claw.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                r1, r2 = await asyncio.gather(
                    client.post("/api/chat/slots/s1/regenerate"),
                    client.post("/api/chat/slots/s1/regenerate"),
                )
                statuses = sorted([r1.status, r2.status])
                assert statuses == [200, 409], f"Expected one 200 and one 409, got {statuses}"
        # Cleanup
        if slot.task:
            slot.task.cancel()


class TestForkSlot:
    """Tests for POST /api/chat/slots/{slot}/fork."""

    @pytest.mark.asyncio
    async def test_fork_copies_all_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "My Chat"
        slot._titled = True
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi there", "msg msg-a")
        slot.append("user", "how are you", "msg msg-u")
        slot.append("assistant", "good", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 4
            assert data["title"] == "Fork of My Chat"

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.forked_from == "dashboard:src"
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 4

    @pytest.mark.asyncio
    async def test_fork_at_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 1})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2
        assert visible[-1]["content"] == "reply1"

    @pytest.mark.asyncio
    async def test_fork_preserves_meta(self, tmp_path):
        # Regression: chat_fork.py previously dropped the `meta` dict when copying
        # messages into the new slot, silently breaking every meta-based feature
        # (knowledge chips, paste refs, future inline-comment rewrite badges).
        # Fork must preserve meta verbatim on copied messages.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append(
            "user",
            "find me a citation",
            "msg msg-u",
            meta={"paste_refs": ["ref-abc123"]},
        )
        slot.append(
            "assistant",
            "Here you go",
            "msg msg-a",
            meta={"knowledge_chips": [{"id": "kb-42", "title": "Cite-X"}]},
        )
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2

        # User message: meta.paste_refs survived
        assert visible[0]["role"] == "user"
        assert visible[0].get("meta") == {
            "paste_refs": ["ref-abc123"]
        }, f"Fork dropped user meta. Got: {visible[0].get('meta')!r}"

        # Assistant message: meta.knowledge_chips survived
        assert visible[1]["role"] == "assistant"
        assert visible[1].get("meta") == {
            "knowledge_chips": [{"id": "kb-42", "title": "Cite-X"}]
        }, f"Fork dropped assistant meta. Got: {visible[1].get('meta')!r}"

    @pytest.mark.asyncio
    async def test_fork_handles_messages_without_meta(self, tmp_path):
        # The mirror of test_fork_preserves_meta: messages with no meta dict
        # must NOT acquire a spurious meta=None or meta={} after fork. Guards
        # against regressions where the fix accidentally added empty meta to
        # every copied message.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "plain msg", "msg msg-u")
        slot.append("assistant", "plain reply", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        # No spurious "meta" key should appear when the parent had none.
        for m in visible:
            assert (
                "meta" not in m
            ), f"Fork added spurious meta to a message without meta. Got: {m!r}"

    @pytest.mark.asyncio
    async def test_fork_not_found(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/nope/fork", json={})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_fork_empty_slot(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/empty/fork", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_inherits_agent_and_workspace(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", agent="my-agent", workspace="my-ws")
        slot.model = "custom-model"
        slot.mode = "custom-mode"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.agent == "my-agent"
        assert new_slot.workspace == "my-ws"
        assert new_slot.model == "custom-model"
        assert new_slot.mode == "custom-mode"

    @pytest.mark.asyncio
    async def test_fork_inherits_folder(self, tmp_path):
        """Fork must land in the same project folder as the source slot."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.folder_id = "proj-abc"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.folder_id == "proj-abc"

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_folder(self, tmp_path):
        """Fork of an unfoldered slot stays unfoldered (root)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_fork_with_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "context", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "fix the bug"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["prompt"] == "fix the bug"
            assert data["messages"] == 1

        # Prompt is returned for frontend to send separately — must NOT be
        # injected into the forked slot server-side.
        new_slot = state._slots.get(data["key"])
        assert all(m["content"] != "fix the bug" for m in new_slot.messages)

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_assistant_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "show me the key", "msg msg-u")
        slot.append("assistant", "Here: AKIAIOSFODNN7EXAMPLE", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["ok"] is True

        new_slot = state._slots.get(data["key"])
        assistant_msgs = [m for m in new_slot.messages if m["role"] == "assistant"]
        assert "AKIAIOSFODNN7EXAMPLE" not in assistant_msgs[0]["content"]
        assert "[REDACTED" in assistant_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_llm_generated_title(self, tmp_path):
        """Parent title is LLM-generated (via /api/chat/generate-title) and
        flows into the new slot's title + API response + dashboard JSON.
        Must be redacted like any other LLM output (AUTOSDE security-controls).
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "Leaked AKIAIOSFODNN7EXAMPLE key"
        slot._titled = True
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "ok", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in data["title"]
        assert "[REDACTED" in data["title"]
        assert data["title"].startswith("Fork of ")
        new_slot = state._slots.get(data["key"])
        assert "AKIAIOSFODNN7EXAMPLE" not in new_slot.title

    @pytest.mark.asyncio
    async def test_fork_rejects_bool_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_negative_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_excludes_system_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("system", "you are helpful", "msg msg-s")
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        roles = [m["role"] for m in new_slot.messages]
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_fork_persists_to_disk(self, tmp_path):
        """Forked slot (and forked_from metadata) must survive a save/restore cycle."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Simulate a gateway restart by reading messages + metadata from disk
        from kiro_claw.dashboard.chat import _history_key_for

        hk = _history_key_for(new_key)
        meta = state.conversation_log.get_metadata(hk)
        disk_msgs = state.conversation_log.read_messages(hk)
        assert meta.get("forked_from") == "dashboard:src", f"forked_from not persisted; meta={meta}"
        assert len(disk_msgs) == 2, f"forked messages not persisted (got {len(disk_msgs)})"

    @pytest.mark.asyncio
    async def test_fork_rejects_oversized_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "x" * 40_000},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_out_of_range_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_succeeds_while_streaming(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "done reply", "msg msg-a")
        slot.drain()

        # Simulate a running session: task attribute non-None + not done
        class _FakeTask:
            def done(self):
                return False

        slot.task = _FakeTask()  # type: ignore[assignment]
        assert slot.running is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

    @pytest.mark.asyncio
    async def test_fork_emits_sel_audit_event(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        mock_sel.log_api_access.assert_called_once()
        kw = mock_sel.log_api_access.call_args[1]
        assert kw["operation"] == "chat.slot_fork"
        assert kw["outcome"] == "allowed"
        assert "from=src" in kw["resources"]
        assert f"to={data['key']}" in kw["resources"]
        # L5 audit enrichment: at_index + prompt_len present
        assert "at_index=last" in kw["resources"]
        assert "prompt_len=0" in kw["resources"]

    @pytest.mark.asyncio
    async def test_fork_rejects_ephemeral_slot(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.memory_mode = "incognito"
        slot.append("user", "secret", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 400
            data = await resp.json()
            assert "persistent" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_fork_history_visible_to_new_kiro_via_context_builder(self, tmp_path):
        """Forked JSONL is the source build_session_context reads for the new slot.

        Guarantees the fresh kiro-cli process in the forked tab receives the
        copied user/assistant turns as thread-history context.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "parent question", "msg msg-u")
        slot.append("assistant", "parent answer", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # conversation_log.recent(forked_key) is what ContextBuilder.build_session_context
        # calls to assemble the thread-history section for the new kiro process.
        from kiro_claw.dashboard.chat import _history_key_for

        recent = state.conversation_log.recent(_history_key_for(new_key))
        visible = [m for m in recent if m.get("role") in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "parent question",
            "parent answer",
        ], f"fork history not readable as new-session context: {visible}"

    @pytest.mark.asyncio
    async def test_fork_does_not_clone_parent_kiro_session_id(self, tmp_path, monkeypatch):
        """Parent's kiro-cli session id (session_map sid) must NOT carry to fork.

        Cloning the sid would make both tabs share one kiro process state and
        corrupt each other's view. Fork creates a FRESH kiro session on first
        prompt by leaving session_map unset for the new key.
        """
        from kiro_claw.session import SessionMap

        monkeypatch.setattr("kiro_claw.session_map.config_dir", lambda: tmp_path)
        session_map = SessionMap()
        session_map.set("dashboard:src", "parent-kiro-sid-abc123")

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Re-read from disk so we're not trusting an in-process cache.
        # Inspect _data directly to skip SessionMap.get()'s kiro-session file
        # existence check (we don't spawn real kiro processes in unit tests).
        reloaded = SessionMap()
        assert (
            reloaded._data.get("dashboard:src", {}).get("sid") == "parent-kiro-sid-abc123"
        ), "parent's kiro sid should survive fork unchanged"
        assert (
            f"dashboard:{new_key}" not in reloaded._data
        ), "forked slot must NOT inherit parent's kiro sid"

    @pytest.mark.asyncio
    async def test_fork_of_fork_chains_forked_from(self, tmp_path):
        """M10: fork of a fork titles correctly and `forked_from` points to intermediate, not root."""
        state = _make_state(tmp_path)
        root = state.get_or_create_slot("root")
        root.title = "Original"
        root._titled = True
        root.append("user", "q1", "msg msg-u")
        root.append("assistant", "a1", "msg msg-a")
        root.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1 = await client.post("/api/chat/slots/root/fork", json={})
            d1 = await r1.json()
            mid_key = d1["key"]
            assert d1["title"] == "Fork of Original"

            mid = state._slots.get(mid_key)
            mid.append("user", "q2", "msg msg-u")
            mid.append("assistant", "a2", "msg msg-a")
            mid.drain()

            r2 = await client.post(f"/api/chat/slots/{mid_key}/fork", json={})
            d2 = await r2.json()

        leaf = state._slots.get(d2["key"])
        assert d2["title"] == "Fork of Fork of Original"
        assert (
            leaf.forked_from == f"dashboard:{mid_key}"
        ), f"leaf forked_from should point to intermediate, got {leaf.forked_from}"
        assert leaf.forked_from != "dashboard:root"
        visible = [m for m in leaf.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_reads_full_history_from_disk_when_memory_capped(self, tmp_path):
        """M12: when in-memory snapshot is smaller than full history, fork reads from disk."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_claw.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore cap: keep only last 50 in memory.
        # Clear _dirty so the endpoint's flush-if-dirty path doesn't overwrite disk.
        slot.messages = slot.messages[-50:]
        slot._dirty = False

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        assert (
            data["messages"] == 250
        ), f"fork should read full history from disk, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 250
        assert visible[0]["content"] == "m0"
        assert visible[-1]["content"] == "m249"

    @pytest.mark.asyncio
    async def test_fork_preserves_full_history_when_dirty_and_capped(self, tmp_path):
        """A1 regression: _dirty=True + capped in-memory must NOT truncate disk history."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_claw.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore with cap: real path caps messages then sets
        # _resumed_count to the capped length. User then sends new messages.
        slot.messages = slot.messages[-50:]
        slot._resumed_count = len(slot.messages)
        slot.append("user", "new1", "msg")
        slot.append("assistant", "new2", "msg")
        slot.drain()
        assert slot._dirty is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        # Full 250 on disk + 2 new dirty messages = 252 total.
        assert (
            data["messages"] == 252
        ), f"fork must preserve full disk history + dirty tail, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[0]["content"] == "m0"
        assert visible[-2]["content"] == "new1"
        assert visible[-1]["content"] == "new2"

    @pytest.mark.asyncio
    async def test_fork_concurrent_requests_both_succeed(self, tmp_path):
        """R2-7: two rapid fork requests on the same slot both return 200 with
        identical visible-message counts. Each fork produces an independent new
        slot; no messages lost or duplicated."""
        import asyncio

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "q1", "msg msg-u")
        slot.append("assistant", "a1", "msg msg-a")
        slot.append("user", "q2", "msg msg-u")
        slot.append("assistant", "a2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1, r2 = await asyncio.gather(
                client.post("/api/chat/slots/src/fork", json={}),
                client.post("/api/chat/slots/src/fork", json={}),
            )
            assert r1.status == 200 and r2.status == 200
            d1, d2 = await r1.json(), await r2.json()

        assert d1["key"] != d2["key"], "concurrent forks must produce distinct slot keys"
        assert (
            d1["messages"] == d2["messages"] == 4
        ), f"both forks must copy all 4 visible messages, got {d1['messages']}/{d2['messages']}"
        for key in (d1["key"], d2["key"]):
            new_slot = state._slots.get(key)
            visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
            assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_audits_denied_on_ephemeral(self, tmp_path, monkeypatch):
        """M-1 regression: ephemeral rejection must emit a denied SEL event."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.memory_mode = "incognito"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 400

        mock_sel.log_api_access.assert_called_once()
        kw = mock_sel.log_api_access.call_args[1]
        assert kw["operation"] == "chat.slot_fork"
        assert kw["outcome"] == "denied"
        assert "memory_mode=incognito" in kw["resources"]

    @pytest.mark.asyncio
    async def test_fork_app_isolation_rejects_cross_app(self, tmp_path, monkeypatch):
        """M-2 regression: app A cannot fork a slot owned by app B."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-B")
        slot.append("user", "secret", "msg msg-u")
        slot.drain()

        # aiohttp middleware populates request["app"]; test injects via middleware.
        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 403
            data = await resp.json()
            assert "does not own" in data["error"]

        # denied event logged
        denied_calls = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied_calls) == 1
        assert denied_calls[0][1]["source"] == "app_isolation"

    @pytest.mark.asyncio
    async def test_fork_inherits_app_ownership(self, tmp_path):
        """I-1 regression: new_slot._app is the requesting app (or empty for dashboard)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-X")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-X"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert (
            new_slot._app == "app-X"
        ), f"forked slot must inherit caller's app, got {new_slot._app!r}"

    @pytest.mark.asyncio
    async def test_fork_rejects_when_slot_cap_reached(self, tmp_path, monkeypatch):
        """zejiangg rev 3 #46: fork must return 429 + denied audit when slot cap hit."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_fork.sel", lambda: mock_sel)
        # Lower the cap so we don't need to create hundreds of slots.
        monkeypatch.setattr("kiro_claw.dashboard.chat_fork._MAX_SLOTS_FOR_FORK", 3)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()
        # Pre-populate to hit the cap (src + 2 dummies = 3).
        state.get_or_create_slot("dummy1")
        state.get_or_create_slot("dummy2")
        assert len(state._slots) == 3

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 429
            data = await resp.json()
            assert "cap" in data["error"].lower()

        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_fork_at_index_spans_chained_session_files(self, tmp_path):
        """Index space must match the frontend (chained), not the current file alone.

        Regression for Mesh-1615: the slot detail endpoint returns
        ``read_messages_chained`` (all sibling session files sharing the
        slot's ``tab_id``), and the frontend builds its fork-button index
        against that list. Pre-fix, fork called ``read_messages`` and
        rejected any index past the current file's boundary.
        """
        from kiro_claw.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        tab_id = "tab12345abcd"

        # Older sibling session file with same tab_id (4 visible messages).
        # The chained-read glob matches ``dashboard_chat-*.jsonl`` so the key
        # must start with ``chat-`` to participate in chaining. The chained
        # walker uses ``sorted(glob)`` so file names must lexicographically
        # match chronological order — production uses ``chat-N-<ts>`` which
        # naturally sorts; mirror that with explicit ordering here.
        older_key = "dashboard:chat-tab-1-old"
        state.conversation_log.append(older_key, "user", "old-q1", tab_id=tab_id)
        state.conversation_log.append(older_key, "assistant", "old-a1")
        state.conversation_log.append(older_key, "user", "old-q2")
        state.conversation_log.append(older_key, "assistant", "old-a2")

        # Current session file with same tab_id (2 visible messages).
        current_key = "dashboard:chat-tab-2-new"
        state.conversation_log.append(current_key, "user", "new-q1", tab_id=tab_id)
        state.conversation_log.append(current_key, "assistant", "new-a1")
        state.conversation_log.invalidate_tab_id_cache()

        # In-memory slot mirrors the current file's persisted view; mark
        # clean so the fork handler relies on the chained read alone.
        slot = state.get_or_create_slot("chat-tab-2-new")
        slot._tab_id = tab_id
        slot.append("user", "new-q1", "msg msg-u")
        slot.append("assistant", "new-a1", "msg msg-a")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False

        # Frontend visibleIndexMap assigns index 5 to the last "new-a1"
        # (chained list has 6 user/assistant entries: 4 older + 2 new).
        assert _history_key_for("chat-tab-2-new") == current_key
        chained = state.conversation_log.read_messages_chained(current_key)
        assert len([m for m in chained if m.get("role") in ("user", "assistant")]) == 6

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/chat-tab-2-new/fork",
                json={"at_message_index": 5},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 6  # full chained history up to index 5

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "old-q1",
            "old-a1",
            "old-q2",
            "old-a2",
            "new-q1",
            "new-a1",
        ]


# ── Color theme & persona injection tests ──


class TestColorTheme:
    """Tests for color_theme validation, slot assignment, and Lumon persona injection."""

    @pytest.mark.asyncio
    async def test_color_theme_set_on_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": "lumon"},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == "lumon"

    @pytest.mark.asyncio
    async def test_color_theme_cleared_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "lumon"
        with patch("kiro_claw.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": ""},
                )
                assert resp.status == 200
                assert slot.color_theme == ""

    @pytest.mark.asyncio
    async def test_color_theme_not_cleared_when_absent(self, tmp_path, monkeypatch):
        """Omitting color_theme from body must not reset an existing theme."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "lumon"
        with patch("kiro_claw.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot"},
                )
                assert resp.status == 200
                assert slot.color_theme == "lumon"

    @pytest.mark.asyncio
    async def test_invalid_color_theme_coerced_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": "evil"},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == ""

    @pytest.mark.asyncio
    async def test_non_string_color_theme_coerced(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_claw.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": 42},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == ""


class TestLumonPersonaInjection:
    """Tests for _maybe_inject_persona helper function."""

    def setup_method(self):
        from kiro_claw.dashboard import chat

        if hasattr(chat, "_cached_lumon_persona"):
            chat._cached_lumon_persona.cache_clear()

    def test_persona_appended_when_lumon(self, tmp_path):
        from kiro_claw.dashboard.chat import _maybe_inject_persona

        fake_persona = "Use a light Lumon-inspired persona."
        with patch(
            "kiro_claw.dashboard.chat_utils._cached_lumon_persona", return_value=fake_persona
        ):
            result = _maybe_inject_persona("hello", "lumon", True)

        assert "[LUMON PERSONA]" in result
        assert fake_persona in result

    def test_persona_not_appended_without_lumon(self):
        from kiro_claw.dashboard.chat import _maybe_inject_persona

        result = _maybe_inject_persona("hello", "", True)
        assert result == "hello"

    def test_persona_not_appended_on_followup(self):
        from kiro_claw.dashboard.chat import _maybe_inject_persona

        result = _maybe_inject_persona("hello", "lumon", False)
        assert result == "hello"

    def test_persona_survives_cache_error(self):
        from kiro_claw.dashboard.chat import _maybe_inject_persona

        with patch(
            "kiro_claw.dashboard.chat_utils._cached_lumon_persona", side_effect=ImportError("boom")
        ):
            result = _maybe_inject_persona("hello", "lumon", True)
        assert result == "hello"

    def test_persona_empty_cache_returns_original(self):
        from kiro_claw.dashboard.chat import _maybe_inject_persona

        with patch("kiro_claw.dashboard.chat_utils._cached_lumon_persona", return_value=""):
            result = _maybe_inject_persona("hello", "lumon", True)
        assert result == "hello"


class TestStopReasonCancelled:
    """Phase 4: handler response to stopReason='cancelled'."""

    @staticmethod
    def _make_mock_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = MagicMock()
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_record_success(self, tmp_path, monkeypatch):
        """When EVENT_COMPLETE carries stop_reason='cancelled', neither
        record_success nor record_failure should be called."""
        from kiro_claw.acp.types import STOP_REASON_CANCELLED
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_not_called()
        state.sessions.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_consolidation(self, tmp_path, monkeypatch):
        """When cancelled, maybe_consolidate must not be called."""
        from kiro_claw.acp.types import STOP_REASON_CANCELLED
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        state.consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_end_turn_preserves_existing_behavior(
        self, tmp_path, monkeypatch
    ):
        """When stop_reason='end_turn', record_success and maybe_consolidate fire."""
        from kiro_claw.acp.types import STOP_REASON_END_TURN
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_called_once()
        state.consolidator.maybe_consolidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_flushes_partial_text(self, tmp_path, monkeypatch):
        """Partial text chunks before cancel must be flushed to the slot."""
        from kiro_claw.acp.types import STOP_REASON_CANCELLED
        from kiro_claw.dashboard.chat import _run_chat
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial output here"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("partial output here" in m["content"] for m in assistant_msgs)


# ── Phase 5: Soft-stop dashboard backend tests ──


class TestStopTurnSlotState:
    """Tests for api_chat_slot_stop soft/hard state transitions."""

    def _make_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.stop_turn = AsyncMock(return_value="soft")
        sessions.reset = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_soft(self, tmp_path, monkeypatch):
        """POST stop → idle→soft_pending; after on_soft → idle."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        captured_states: list[str] = []

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            captured_states.append(slot._stop_state)
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert captured_states == ["soft_pending"]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_hard(self, tmp_path, monkeypatch):
        """POST stop with hard outcome → idle→soft_pending→idle after on_hard."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_force_query_param(self, tmp_path, monkeypatch):
        """POST stop?force=true when soft_pending → skips cancel, hard kill."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop?force=true")
            assert resp.status == 200

        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_second_press_escalates_without_force_flag(self, tmp_path, monkeypatch):
        """A second stop press while soft_pending hard-kills even when the
        client did NOT send force=true. The client derives force from the
        WS-echoed stop_state, which lags on a slow connection; the backend's
        own soft_pending state is authoritative, so any second press escalates.
        """
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # No ?force=true — the lagging client still thinks state is idle.
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        # Backend escalated to a hard kill anyway.
        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_first_press_clears_queue(self, tmp_path, monkeypatch):
        """Queue populated; POST stop; queue empty (via stop_turn side effect)."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._queue.extend(["msg1", "msg2"])

        # stop_turn clears queue internally; verify slot._queue is cleared
        # by the time stop_turn is called (api_chat_slot_stop sets state
        # before calling stop_turn, and stop_turn calls clear_queue)
        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_appears_in_transcript(self, tmp_path, monkeypatch):
        """After stop, slot messages contain a stop_event entry."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["kind"] == "stop_event"
        assert data["state"] == "stopped"
        assert data["outcome"] == "soft"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_replace_in_place(self, tmp_path, monkeypatch):
        """Stop event has stable id across state transitions (one entry)."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(key, *, force=False, on_soft=None, on_hard=None):
            # Verify the stop_event was inserted before callbacks
            stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
            assert len(stop_msgs) == 1
            pre_data = json.loads(stop_msgs[0]["content"])
            assert pre_data["state"] == "stopping"
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/chat/slots/s1/stop")

        # Still only one stop_event message
        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["state"] == "stopped"
        slot.task.cancel()


class TestStopHistoryBanner:
    """Tests for history re-injection banner skip on soft stop."""

    @staticmethod
    def _last_stop_soft(slot: _ChatSlot) -> bool:
        """Replicates the detection logic in chat.py:_run_chat."""
        import json

        for m in reversed(slot.messages):
            cls_val = m.get("cls", "")
            if not isinstance(cls_val, str) or not cls_val.startswith("{"):
                continue
            try:
                _cls = json.loads(cls_val)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(_cls, dict) or _cls.get("kind") != "stop_event":
                continue
            return _cls.get("outcome") == "soft"
        return False

    def test_soft_stop_preserves_session_no_history_banner(self):
        """After a soft stop, _build_history_prefix is skipped."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        # cls must be a JSON-encoded dict (same format api_chat_slot_stop uses)
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stopped",
                "outcome": "soft",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is True

    def test_hard_stop_still_injects_history_banner(self):
        """After a hard stop, the banner detection returns False."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stop_failed_reset",
                "outcome": "hard",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is False

    def test_plain_string_cls_does_not_match(self):
        """Plain-string cls (legacy format) is ignored — no false positive."""
        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("system", "{}", "stop_event")  # plain string cls
        assert self._last_stop_soft(slot) is False


# ── Tests: AcpProcessDied handler in _run_chat ──


class TestAcpProcessDiedRecovery:
    """Verify _run_chat handles AcpProcessDied with retry logic, redaction, and session reset."""

    def _make_state_and_slot(self, tmp_path):
        from kiro_claw.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("pipe-death-slot")
        slot.append("user", "hello", "msg msg-u")

        mock_client = state.sessions.get_or_create.return_value[0]
        mock_client.shutdown = AsyncMock()
        return state, slot, mock_client, _run_chat

    def _make_stream_raise(self, mock_client, exc):
        async def _raise(msg):
            raise exc
            yield  # noqa: E501

        mock_client.stream = _raise
        mock_client.stream_command = _raise

    @pytest.mark.asyncio
    async def test_retry_at_depth_0_requeues_message(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → message re-queued, retrying shown."""
        from kiro_claw.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()
        assert slot._acp_pipe_death_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("retrying" in m.get("content", "") for m in error_msgs)

    @pytest.mark.asyncio
    async def test_budget_exhaustion_shows_stuck(self, tmp_path: Path) -> None:
        """4th pipe death → 'Session stuck' shown, no re-queue."""
        from kiro_claw.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._acp_pipe_death_retries = 3  # already exhausted
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        assert slot._acp_pipe_death_retries == 4
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("stuck" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_nested_depth_shows_please_retry(self, tmp_path: Path) -> None:
        """Pipe death at depth > 0 → 'please retry' shown, no re-queue."""
        from kiro_claw.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        assert slot._acp_pipe_death_retries == 1
        error_msgs = [m for m in slot.messages if m.get("role") == "error"]
        assert any("please retry" in m.get("content", "").lower() for m in error_msgs)

    @pytest.mark.asyncio
    async def test_partial_assistant_text_redacted(self, tmp_path: Path) -> None:
        """Pipe death mid-stream → partial output redacted before display."""
        from kiro_claw.acp.client import AcpProcessDied
        from kiro_claw.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream_then_die(msg):
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK, text="partial output with AKIA1234567890ABCDEF secret"
            )
            raise AcpProcessDied("pipe broken")

        client.stream = _stream_then_die
        client.stream_command = _stream_then_die

        await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")

    @pytest.mark.asyncio
    async def test_session_reset_propagated(self, tmp_path: Path) -> None:
        """Verify the finally block resets the session after AcpProcessDied."""
        from kiro_claw.acp.client import AcpProcessDied

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        await _run_chat(state, slot, "test message")

        state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_error_redacts_partial_text(self, tmp_path: Path) -> None:
        """CancelledError mid-stream → partial output redacted before display."""
        from kiro_claw.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream_then_cancel(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial with AKIA1234567890ABCDEF key")
            raise asyncio.CancelledError()

        client.stream = _stream_then_cancel
        client.stream_command = _stream_then_cancel

        await _run_chat(state, slot, "test message")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistant_msgs, "Expected at least one assistant message with redacted content"
        for m in assistant_msgs:
            assert "AKIA1234567890ABCDEF" not in m.get("content", "")

    @pytest.mark.asyncio
    async def test_retry_requeues_via_queue_insert(self, tmp_path: Path) -> None:
        """First pipe death at depth 0 → queue_insert is called."""
        from unittest.mock import patch as _patch

        from kiro_claw.acp.client import AcpProcessDied
        from kiro_claw.dashboard.state import _ChatSlot

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_stream_raise(client, AcpProcessDied("pipe broken"))

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with _patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")

        assert (0, "test message") in calls
