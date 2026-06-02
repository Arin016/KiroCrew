"""Tests for ClaudeCodeProvider (subprocess-based)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_claw.providers.claude_code import (
    ClaudeCodeConnectionError,
    ClaudeCodeProvider,
    ClaudeCodeProviderError,
)


def _ndjson(*events: dict) -> bytes:
    """Build NDJSON bytes from event dicts."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _system_init(session_id: str = "test-sess", model: str = "claude-opus-4-6") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": model,
        "tools": ["Bash", "Read", "Edit"],
        "mcp_servers": [],
    }


def _assistant_text(text: str, session_id: str = "test-sess") -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1000},
        },
        "session_id": session_id,
    }


def _assistant_thinking(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "thinking", "thinking": text}],
            "usage": {"input_tokens": 500},
        },
        "session_id": "test-sess",
    }


def _assistant_tool_use(name: str, inp: dict, tid: str = "tool-1") -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "usage": {"input_tokens": 800},
        },
        "session_id": "test-sess",
    }


def _result(stop_reason: str = "end_turn", session_id: str = "test-sess") -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "stop_reason": stop_reason,
        "session_id": session_id,
        "usage": {"input_tokens": 1000},
        "modelUsage": {"claude-opus-4-6": {"inputTokens": 200, "cacheReadInputTokens": 800, "contextWindow": 1000000}},
    }


async def _mock_subprocess(*events: dict, returncode: int = 0):
    """Create a mock subprocess with NDJSON output."""
    proc = AsyncMock()
    proc.stdout = AsyncMock()
    proc.stdout.__aiter__ = lambda self: self
    lines = [json.dumps(e).encode() + b"\n" for e in events]
    proc.stdout.__anext__ = AsyncMock(side_effect=lines + [StopAsyncIteration()])
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = AsyncMock()
    proc.terminate = AsyncMock()
    return proc


class TestInit:
    def test_defaults(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        assert p._model is None
        assert p._started is False
        assert p._bare is False
        assert p._permission_mode == "bypassPermissions"

    def test_custom_params(self):
        p = ClaudeCodeProvider(
            connection_mode="ephemeral",
            model="opus",
            permission_mode="plan",
            max_turns=10,
            max_budget_usd=5.0,
            channel_id="C123",
        )
        assert p._model == "opus"
        assert p._permission_mode == "plan"
        assert p._max_turns == 10
        assert p._channel_id == "C123"


class TestStart:
    @pytest.mark.asyncio
    async def test_start_success(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            assert p._started
            assert p._claude_bin == "/usr/bin/claude"

    @pytest.mark.asyncio
    async def test_start_no_binary(self):
        with patch("shutil.which", return_value=None):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            with pytest.raises(ClaudeCodeConnectionError, match="not found"):
                await p.start()

    @pytest.mark.asyncio
    async def test_start_with_resume(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            p.set_resume_session_id("prev-session")
            await p.start()
            assert p._session_id == "prev-session"

    @pytest.mark.asyncio
    async def test_start_uses_augmented_path(self):
        """shutil.which receives augmented search path including ~/.toolbox/bin."""
        with patch("shutil.which", return_value="/home/user/.toolbox/bin/claude") as mock_which:
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            mock_which.assert_called_once()
            call_kwargs = mock_which.call_args
            search_path = call_kwargs.kwargs.get("path") or call_kwargs[1].get("path", "")
            assert ".toolbox/bin" in search_path
            assert p._claude_bin == "/home/user/.toolbox/bin/claude"


class TestStream:
    @pytest.mark.asyncio
    async def test_text_and_complete(self):
        events = [_system_init(), _assistant_text("hello"), _result()]

        async def fake_lines():
            for e in events:
                yield json.dumps(e).encode() + b"\n"

        proc = AsyncMock()
        proc.stdout = fake_lines()
        proc.wait = AsyncMock(return_value=0)

        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = [e async for e in p.stream("hi")]

        assert result[0].kind == EVENT_TEXT_CHUNK
        assert result[0].text == "hello"
        assert result[1].kind == EVENT_COMPLETE
        assert p._session_id == "test-sess"

    @pytest.mark.asyncio
    async def test_thinking_event(self):
        events = [
            _system_init(),
            _assistant_thinking("reasoning..."),
            _assistant_text("answer"),
            _result(),
        ]

        async def fake_lines():
            for e in events:
                yield json.dumps(e).encode() + b"\n"

        proc = AsyncMock()
        proc.stdout = fake_lines()
        proc.wait = AsyncMock(return_value=0)

        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = [e async for e in p.stream("think")]

        assert result[0].kind == EVENT_THINKING_CHUNK
        assert result[0].text == "reasoning..."
        assert result[1].kind == EVENT_TEXT_CHUNK

    @pytest.mark.asyncio
    async def test_tool_use_event(self):
        events = [_system_init(), _assistant_tool_use("Bash", {"command": "ls"}), _result()]

        async def fake_lines():
            for e in events:
                yield json.dumps(e).encode() + b"\n"

        proc = AsyncMock()
        proc.stdout = fake_lines()
        proc.wait = AsyncMock(return_value=0)

        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = [e async for e in p.stream("run ls")]

        assert result[0].kind == EVENT_TOOL_CALL
        assert result[0].title == "Bash"
        assert '"command": "ls"' in result[0].tool_input

    @pytest.mark.asyncio
    async def test_not_started_raises(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        with pytest.raises(ClaudeCodeProviderError, match="not started"):
            async for _ in p.stream("hi"):
                pass

    @pytest.mark.asyncio
    async def test_context_tracking(self):
        events = [
            _system_init(model="claude-opus-4.6"),
            _assistant_text("hi"),
            _result(),
        ]

        async def fake_lines():
            for e in events:
                yield json.dumps(e).encode() + b"\n"

        proc = AsyncMock()
        proc.stdout = fake_lines()
        proc.wait = AsyncMock(return_value=0)

        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral", model="claude-opus-4.6")
            await p.start()
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                _ = [e async for e in p.stream("hi")]

        # 1000 input_tokens / 1_000_000 context window = 0.1%
        assert p.context_usage_pct() == pytest.approx(0.1, abs=0.01)


class TestCommandBuilding:
    def test_basic_command(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        assert cmd[0] == "/usr/bin/claude"
        assert "-p" in cmd
        assert "-" in cmd  # stdin mode (message passed via stdin, not arg)
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert "--bare" not in cmd  # bare=False by default (load MCP/plugins)

    def test_resume_command_persistent(self):
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._claude_bin = "/usr/bin/claude"
        p._resume_sid = "sess-123"
        cmd = p._build_persistent_command()
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-123"

    def test_ephemeral_no_resume(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        p._claude_bin = "/usr/bin/claude"
        p._session_id = "sess-123"
        cmd = p._build_ephemeral_command()
        assert "--resume" not in cmd

    def test_model_override(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="opus")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    def test_permission_mode(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", permission_mode="plan")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"

    def test_disallowed_tools(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", disallowed_tools=["Bash(rm *)"], security_deny_patterns=["*danger*"])
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        assert "--disallowedTools" in cmd
        idx = cmd.index("--disallowedTools")
        assert "Bash(rm *)" in cmd[idx + 1]
        assert "*danger*" in cmd[idx + 1]

    def test_max_turns_and_budget(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", max_turns=10, max_budget_usd=5.0)
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        assert "--max-turns" in cmd
        assert "--max-budget-usd" in cmd

    # ─── reasoning_effort  ─────────────────────────────────────
    # Matrix: explicit override wins; empty falls back to opus heuristic;
    # both ephemeral and per_session command builders honour it identically.

    def test_reasoning_effort_explicit_low_overrides_default(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="opus", reasoning_effort="low")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        idx = cmd.index("--effort")
        # Explicit override wins even on opus (which used to force "max")
        assert cmd[idx + 1] == "low"

    def test_reasoning_effort_explicit_on_non_opus(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="sonnet", reasoning_effort="high")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_reasoning_effort_default_preserves_opus_max_fallback(self):
        # No explicit effort on opus → existing "--effort max" heuristic still wires up.
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="opus", reasoning_effort="")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "max"

    def test_reasoning_effort_default_no_flag_on_non_opus(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="sonnet", reasoning_effort="")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        # Sonnet without explicit effort → no --effort flag at all
        assert "--effort" not in cmd

    def test_reasoning_effort_persistent_command(self):
        p = ClaudeCodeProvider(connection_mode="per_session", model="sonnet", reasoning_effort="medium")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_persistent_command()
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "medium"

    def test_reasoning_effort_rejects_invalid_at_subprocess_boundary(self):
        """Defense-in-depth: invalid values are rejected at the subprocess layer."""
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="opus", reasoning_effort="; rm -rf /")
        p._claude_bin = "/usr/bin/claude"
        cmd = p._build_ephemeral_command()
        # Invalid effort was rejected — falls back to opus heuristic (max)
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "max"
        assert "; rm -rf /" not in cmd


class TestLiveness:
    def test_not_started(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        assert not p.is_alive()

    @pytest.mark.asyncio
    async def test_started_is_alive(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            assert p.is_alive()

    @pytest.mark.asyncio
    async def test_always_alive_when_started(self):
        """CC sessions persist on disk — always alive if started (no stale timeout)."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            p._last_activity = time.monotonic() - 7000  # even after hours
            assert p.is_alive()  # disk-persisted sessions never go stale

    def test_touch_activity(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        p._started = True
        p._last_activity = time.monotonic() - 500
        p.touch_activity()
        assert (time.monotonic() - p._last_activity) < 1


class TestCancel:
    @pytest.mark.asyncio
    async def test_no_active_proc(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            result = await p.cancel()
            assert result == "no_turn"

    @pytest.mark.asyncio
    async def test_active_proc_cancel(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = ClaudeCodeProvider(connection_mode="ephemeral")
            await p.start()
            mock_proc = AsyncMock()
            mock_proc.terminate = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.kill = AsyncMock()
            p._active_proc = mock_proc
            result = await p.cancel()
            assert result == "acked"
            assert p._active_proc is None


class TestContextWindow:
    def test_known_model(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="claude-opus-4.6")
        assert p._context_window_tokens == 1_000_000

    def test_unknown_model(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral", model="unknown-xyz")
        assert p._context_window_tokens == 200_000

    def test_no_model(self):
        p = ClaudeCodeProvider(connection_mode="ephemeral")
        assert p._context_window_tokens == 200_000


class TestNoOps:
    @pytest.mark.asyncio
    async def test_approve_noop(self):
        await ClaudeCodeProvider(connection_mode="ephemeral").approve_tool("x")

    @pytest.mark.asyncio
    async def test_reject_noop(self):
        await ClaudeCodeProvider(connection_mode="ephemeral").reject_tool("x")

    @pytest.mark.asyncio
    async def test_wait_for_compaction(self):
        result = await ClaudeCodeProvider(connection_mode="ephemeral").wait_for_compaction()
        assert result["type"] == "completed"


class TestExitCode:
    def test_no_proc(self):
        p = ClaudeCodeProvider(connection_mode="per_session")
        assert p.exit_code is None

    def test_running_proc(self):
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._proc = MagicMock()
        p._proc.returncode = None
        assert p.exit_code is None

    def test_dead_proc(self):
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._proc = MagicMock()
        p._proc.returncode = 137
        assert p.exit_code == 137


class TestEagerReconnect:
    @pytest.mark.asyncio
    async def test_noop_if_proc_alive(self):
        """Eager reconnect is a no-op when process is already running."""
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._proc = MagicMock()
        p._proc.returncode = None
        p._reconnect = AsyncMock()

        await p._eager_reconnect()
        p._reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnects_on_dead_proc(self):
        """Eager reconnect calls _reconnect when process is dead."""
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._proc = MagicMock()
        p._proc.returncode = 1
        p._session_id = "test-sess"
        p._reconnect = AsyncMock()

        await p._eager_reconnect()
        p._reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_reconnect_failure(self):
        """Eager reconnect logs but doesn't raise on failure."""
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._proc = MagicMock()
        p._proc.returncode = 137
        p._session_id = "test-sess"
        p._reconnect = AsyncMock(side_effect=RuntimeError("spawn failed"))

        # Should not raise
        await p._eager_reconnect()
        p._reconnect.assert_called_once()


class TestReaderLoopEagerReconnect:
    @pytest.mark.asyncio
    async def test_schedules_reconnect_on_unexpected_eof(self):
        """Reader loop schedules eager reconnect when process dies (not cancelled)."""
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._event_queue = asyncio.Queue()
        p._eager_reconnect = AsyncMock()

        # Simulate a process that outputs one line then EOF
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # dead

        async def stdout_iter():
            yield json.dumps(_system_init()).encode() + b"\n"

        mock_proc.stdout = stdout_iter()
        p._proc = mock_proc
        p._last_activity = time.monotonic()
        p._session_id = None

        await p._stdout_reader_loop()

        # Should have put None sentinel
        assert await p._event_queue.get() is None
        # create_task schedules the reconnect — let the event loop tick
        await asyncio.sleep(0)
        p._eager_reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_reconnect_on_cancel(self):
        """Reader loop does NOT schedule eager reconnect when cancelled."""
        p = ClaudeCodeProvider(connection_mode="per_session")
        p._event_queue = asyncio.Queue()
        p._eager_reconnect = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.returncode = 1  # dead process — cancelled flag is sole guard

        async def stdout_cancel():
            raise asyncio.CancelledError()
            yield  # noqa: unreachable - makes this an async generator

        mock_proc.stdout = stdout_cancel()
        p._proc = mock_proc
        p._last_activity = time.monotonic()

        await p._stdout_reader_loop()

        # cancelled=True prevents reconnect despite dead process
        p._eager_reconnect.assert_not_called()


class TestStreamPersistentErrorHandling:
    @pytest.mark.asyncio
    async def test_process_death_yields_error_complete(self):
        """When reader loop puts None (process died), stream yields error event."""
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch.object(ClaudeCodeProvider, "_spawn_persistent_process", new_callable=AsyncMock):
            p = ClaudeCodeProvider(connection_mode="per_session")
            await p.start()

        p._event_queue = asyncio.Queue()
        p._turn_done = asyncio.Event()

        # Simulate live proc that accepts stdin; inject None on drain
        # (after the stale-event drain phase has already cleared the queue)
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock(
            side_effect=lambda: p._event_queue.put_nowait(None)
        )
        p._proc = mock_proc

        events = [e async for e in p._stream_persistent("hello")]
        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "error: process died"

    @pytest.mark.asyncio
    async def test_timeout_yields_error_complete(self):
        """When event queue times out, stream yields timeout error."""
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch.object(ClaudeCodeProvider, "_spawn_persistent_process", new_callable=AsyncMock):
            p = ClaudeCodeProvider(connection_mode="per_session")
            await p.start()

        p._event_queue = asyncio.Queue()
        p._turn_done = asyncio.Event()
        p._reconnect = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(side_effect=lambda: setattr(mock_proc, "returncode", -9))
        p._proc = mock_proc

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            events = [e async for e in p._stream_persistent("hello")]

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "error: response timeout"
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()
        p._reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_skips_kill_if_already_dead(self):
        """Timeout skips kill when process already exited."""
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch.object(ClaudeCodeProvider, "_spawn_persistent_process", new_callable=AsyncMock):
            p = ClaudeCodeProvider(connection_mode="per_session")
            await p.start()

        p._event_queue = asyncio.Queue()
        p._turn_done = asyncio.Event()
        p._reconnect = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.kill = MagicMock()
        p._proc = mock_proc

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            events = [e async for e in p._stream_persistent("hello")]

        assert events[0].stop_reason == "error: response timeout"
        mock_proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_initial_reconnect_under_lock(self):
        """_stream_persistent reconnects under lock when proc is dead at start."""
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch.object(ClaudeCodeProvider, "_spawn_persistent_process", new_callable=AsyncMock):
            p = ClaudeCodeProvider(connection_mode="per_session")
            await p.start()

        p._event_queue = asyncio.Queue()
        p._turn_done = asyncio.Event()

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock(
            side_effect=lambda: p._event_queue.put_nowait(None)
        )
        p._proc = mock_proc

        reconnect_called = False

        async def fake_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            mock_proc.returncode = None

        p._reconnect = fake_reconnect

        events = [e async for e in p._stream_persistent("hello")]
        assert events[0].stop_reason == "error: process died"
        assert reconnect_called
