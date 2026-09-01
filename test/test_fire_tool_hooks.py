"""Tests for fire_tool_hooks helper and global hook store accessor."""

from __future__ import annotations

import asyncio
import json
import platform
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_ON_ERROR_DEFAULT,
    HOOK_ON_ERROR_FAIL_CLOSED,
    HOOK_ON_ERROR_FAIL_OPEN,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
    fire_tool_hooks,
    get_global_hook_store,
    run_script_hook,
    set_global_hook_store,
    validate_hook_fields,
)

_IS_WINDOWS = platform.system() == "Windows"

# A command name that is not on PATH on any host, so the platform shell answers
# "command not found" rather than running something. The exit code it answers
# with is the shell's own (127 on POSIX sh, 9009 on cmd.exe), which is why tests
# assert the fail-direction contract portably and pin the literal 127 on POSIX.
_MISSING_BINARY_COMMAND = "kirocrew-no-such-policy-gate --check"


def _sleep_command(script: Path, seconds: int) -> str:
    """Return a hook command that sleeps *seconds*, spelled for either shell.

    ``sleep 8`` is POSIX-only — cmd.exe has no ``sleep`` and no ``;`` separator,
    so on Windows it fails INSTANTLY with "not recognized" and a timeout test
    written that way never reaches the timeout branch it claims to cover. A
    quoted interpreter plus a quoted script path is the one shape ``/bin/sh -c``
    and ``cmd /c`` parse identically (see ``test_script_hooks._script_command``).
    """
    script.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _patch_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the OS sandbox and cgroup wrappers no-ops for a real hook spawn.

    ``run_script_hook`` routes its ``/bin/sh -c`` / ``cmd /c`` argv through
    ``sandboxed_spawn_argv``, so the process that actually runs is the sandbox
    LAUNCHER, and the exit code ``run_script_hook`` reports is the launcher's
    whenever the launcher refuses. A host that cannot establish a control the
    launcher considers mandatory answers 1 ("sandbox: BLOCKED — sealing
    read-only directory …") and a host whose ``unshare``/seccomp step dies
    answers -1 (signal), in both cases BEFORE the hook command is exec'd. A test
    asserting the hook's own exit code (0 allow / 2 deny / 127 missing binary)
    therefore measures the host's sandbox backend instead of the gate, and it
    reads differently on a developer container, a GitHub Linux runner, and a
    Windows runner.

    Patching both chokepoints to identity spawns the hook command directly, so
    the exit code under assertion is the one the hook produced. The sandbox's own
    refusal is not swept under the rug — it is a distinct, deliberate fail-closed
    input, pinned by ``test_sandbox_refusal_blocks_fail_closed``. Patch the
    ``kiro_crew.sandbox`` module, not the caller: ``run_script_hook`` imports
    these lazily at call time to break a circular import.
    """
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


@pytest.fixture(autouse=True)
def _reset_global_store():
    """Reset global hook store between tests."""
    set_global_hook_store(None)  # type: ignore[arg-type]
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


@pytest.fixture
def hook_store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


class TestGlobalHookStore:
    """Test get/set global hook store accessor."""

    def test_default_is_none(self):
        assert get_global_hook_store() is None

    def test_set_and_get(self, hook_store: ScriptHookStore):
        set_global_hook_store(hook_store)
        assert get_global_hook_store() is hook_store

    def test_overwrite(self, tmp_path: Path):
        store1 = ScriptHookStore(tmp_path / "a")
        store2 = ScriptHookStore(tmp_path / "b")
        set_global_hook_store(store1)
        set_global_hook_store(store2)
        assert get_global_hook_store() is store2


class TestFireToolHooks:
    """Test fire_tool_hooks helper."""

    @pytest.mark.asyncio
    async def test_none_store_is_noop(self):
        # Should not raise
        await fire_tool_hooks(None, "Running: echo hello")

    @pytest.mark.asyncio
    async def test_strips_running_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "Running: echo hello")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="echo hello",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_no_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "@builder-mcp/ReadFile")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="@builder-mcp/ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_parses_tool_input_json(self, hook_store: ScriptHookStore):
        ti = json.dumps({"path": "/tmp/test.txt"})
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", ti)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input={"path": "/tmp/test.txt"},
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_invalid_json_passes_none(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", "not-json")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_empty_title(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_fire_exception_swallowed(self, hook_store: ScriptHookStore):
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await fire_tool_hooks(hook_store, "ReadFile")

    @pytest.mark.asyncio
    async def test_none_tool_input_skipped(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", None)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_passes_subagent_metadata(self, hook_store: ScriptHookStore):
        """When called with subagent_id, parent_session_key, agent_role, those propagate to fire()."""
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(
                hook_store,
                "ReadFile",
                None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )


class TestScriptHookStoreFire:
    """Test ScriptHookStore.fire() emits subagent_id, parent_session_key, agent_role into hook_event.

    These tests register a real hook in the store, patch run_script_hook to capture
    the hook_event payload, and assert that the conditional emission branches in fire()
    add (or omit) the new fields correctly.
    """

    @pytest.fixture
    def fire_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "test-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_fire_emits_subagent_id_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_parent_session_key_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                parent_session_key="dashboard:slot-1",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert "subagent_id" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_agent_role_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["agent_role"] == "utility"
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_all_three_together(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert hook_event["agent_role"] == "utility"

    @pytest.mark.asyncio
    async def test_fire_omits_all_three_when_none(self, fire_store: ScriptHookStore):
        """Backward compatibility: when all three are None (default), payload is byte-identical to pre-CR behavior."""
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile")
            (_, _, hook_event), _ = mock_run.call_args
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event


class TestScriptHookStoreStopContext:
    """Stop hooks receive the final assistant segment on stdin, untruncated.

    The env var ``KIROCREW_HOOK_CONTEXT`` is capped at 500 chars (ARG_MAX
    safety), which drops the tail of the segment. A Stop hook that keys on tail
    content (e.g. the harness ``[OPTIONS:]`` menu line) never sees it via the env
    var. fire() therefore emits the untruncated segment into the stdin
    ``hook_event`` payload as ``assistant_text`` — mirroring the existing
    ``prompt`` key for UserPromptSubmit, but on a dedicated arg so the env value
    can stay bounded.
    """

    @pytest.fixture
    def stop_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_stop_full_context_on_stdin_and_matcher(self, stop_store: ScriptHookStore):
        # The load-bearing marker sits at the tail, past the 500-char env cap.
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context=full)
            (_, ctx_arg, hook_event), _ = mock_run.call_args
            # stdin payload carries the FULL segment, tail marker intact.
            assert hook_event["assistant_text"] == full
            assert "[OPTIONS:" in hook_event["assistant_text"]
            # fire() passes the FULL context downstream (matcher + env source);
            # the env-only 500-cap lives in run_script_hook, not here — a tail
            # matcher must therefore still be able to see the marker.
            assert ctx_arg == full

    @pytest.mark.asyncio
    async def test_stop_tail_matcher_is_not_truncated(self, tmp_path: Path):
        # A Stop hook whose matcher targets tail content must still fire — fire()
        # matches against the full context, not the 500-char env slice.
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "options-stop-hook",
            "event": HOOK_EVENT_STOP,
            "matcher": "*[OPTIONS:*",
            "command": "echo test",
        })
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "options-stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await store.fire(HOOK_EVENT_STOP, context=full)
            assert mock_run.await_count == 1, "tail-matching Stop hook was filtered out by env truncation"

    @pytest.mark.asyncio
    async def test_stop_empty_turn_still_emits_key(self, stop_store: ScriptHookStore):
        # An empty / no-output turn still fires Stop with context="". The key MUST
        # be present (unconditional, not truthiness-gated) so a hook that always
        # reads hook_event["assistant_text"] gets "" rather than KeyError-ing.
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "stop-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await stop_store.fire(HOOK_EVENT_STOP, context="")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["assistant_text"] == ""

    @pytest.mark.asyncio
    async def test_user_prompt_submit_still_uses_prompt_key(self, tmp_path: Path):
        # Regression guard: the Stop change must not bleed into UPS, which keeps
        # delivering its full context under the existing ``prompt`` key.
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "ups-hook",
            "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
            "matcher": "",
            "command": "echo test",
        })
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "ups-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await store.fire(HOOK_EVENT_USER_PROMPT_SUBMIT, context="hello")
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["prompt"] == "hello"
            assert "assistant_text" not in hook_event


class TestRunScriptHookStopEnvCap:
    """run_script_hook caps the Stop env var at 500 chars while the full
    segment still reaches the hook via stdin JSON (ARG_MAX safety)."""

    @pytest.mark.asyncio
    async def test_stop_env_context_capped_but_stdin_full(self) -> None:
        """Stop hook: KIROCREW_HOOK_CONTEXT env is capped at 500; stdin JSON is full.

        The env var is bounded by ARG_MAX (a multi-KB turn there can fail process
        creation), so run_script_hook truncates the ENV copy for Stop only — while
        the full segment still reaches the hook via the stdin ``assistant_text``
        payload that fire() built. Captures both channels off a mocked subprocess.
        """
        full = ("x" * 900) + "\n[OPTIONS: A | B | C]"
        hook = ScriptHook(id="s1", name="stop-hook", event=HOOK_EVENT_STOP, command="cat", timeout=5)
        hook_event = {"hook_event_name": HOOK_EVENT_STOP, "cwd": "/", "assistant_text": full}

        class FakeStdin:
            def __init__(self):
                self.data = bytearray()
                self.closed = False

            def write(self, data):
                self.data.extend(data)

            async def drain(self):
                return None

            def close(self):
                self.closed = True

        fake_proc = MagicMock()
        fake_proc.stdin = FakeStdin()
        fake_proc.stdout = asyncio.StreamReader()
        fake_proc.stdout.feed_eof()
        fake_proc.stderr = asyncio.StreamReader()
        fake_proc.stderr.feed_eof()
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.returncode = 0
        captured: dict = {}

        async def fake_exec(*argv, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return fake_proc

        # Both spawn forms are patched because the choice is platform-dependent:
        # Windows hands the command line to ``create_subprocess_shell`` so
        # cmd.exe parses the operator's quotes verbatim, POSIX execs
        # ``/bin/sh -c`` as an argv. The env cap under test is identical either
        # way, so the test must not assume one host's form.
        with (
            patch("kiro_crew.sandbox.wrap_argv", lambda argv, *a, **k: (argv, None)),
            patch("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("asyncio.create_subprocess_shell", side_effect=fake_exec),
        ):
            await run_script_hook(hook, context=full, hook_event=hook_event)

        # ENV copy is capped at 500 chars — the tail marker is dropped there.
        env_ctx = captured["env"]["KIROCREW_HOOK_CONTEXT"]
        assert len(env_ctx) == 500
        assert "[OPTIONS:" not in env_ctx
        # Assert on what actually reached the subprocess stdin (not the input
        # dict): the full segment, tail marker intact, is serialized to stdin.
        stdin_bytes = bytes(fake_proc.stdin.data)
        assert fake_proc.stdin.closed is True
        parsed = json.loads(stdin_bytes)
        assert parsed["assistant_text"] == full
        assert "[OPTIONS:" in parsed["assistant_text"]


# ── FEAT-004: PreToolUse hooks fail closed (GitHub #7339) ──


class TestPreToolUseFailsClosed:
    """A PreToolUse hook that cannot deliver a verdict blocks under the default
    (fail-closed) direction.

    These run the REAL subprocess path (run_script_hook against tiny shell
    commands) so the assertions exercise the true timeout / missing-binary /
    exit-code branches, mirroring the issue repro. Against the pre-fix code
    there was no ``failed_to_run`` / ``should_block_pre_tool_use`` at all and a
    timed-out or crashed PreToolUse hook silently auto-approved the tool, so
    every ``... is True`` block assertion here would fail before the fix.

    Every test that spawns asserts a signal that identifies WHICH branch ran
    (the exit code, or "Timed out" in ``error``) rather than only
    ``failed_to_run``: any spawn failure at all satisfies ``failed_to_run``, so
    the weaker assertion is green even when the intended branch never executed.
    """

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        _patch_sandbox_passthrough(monkeypatch)

    @pytest.mark.asyncio
    async def test_timeout_blocks_fail_closed(self, tmp_path: Path):
        # The command outlives its 1s timeout, so the timeout branch fires
        # (default exit_code -1) and the hook renders no verdict.
        hook = ScriptHook(
            id="pt-timeout",
            name="policy-timeout",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command=_sleep_command(tmp_path / "slow_gate.py", 10),
            timeout=1,
        )
        result = await run_script_hook(hook)
        # Pins the TIMEOUT branch specifically, not merely "something failed".
        assert "Timed out" in result.error
        assert result.blocked is False  # never reached a clean exit 2
        assert result.failed_to_run is True
        # Default (sentinel) PreToolUse hook resolves fail_closed -> must block.
        assert result.should_block_pre_tool_use() is True
        assert result.should_block_pre_tool_use("fail_closed") is True

    @pytest.mark.asyncio
    async def test_missing_binary_blocks_fail_closed(self):
        hook = ScriptHook(
            id="pt-missing",
            name="policy-missing",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command=_MISSING_BINARY_COMMAND,
            timeout=5,
        )
        result = await run_script_hook(hook)
        # The contract is fail-direction, and it holds on whatever code the
        # platform shell picks for "command not found": neither 0 nor 2 is a
        # verdict, so a fail-closed PreToolUse hook must block.
        assert result.exit_code not in (0, 2)
        assert result.failed_to_run is True
        assert result.should_block_pre_tool_use() is True
        if not _IS_WINDOWS:
            # The exit code ``failed_to_run``'s docstring names for a missing
            # binary. cmd.exe answers 9009, so pin the literal where sh runs.
            assert result.exit_code == 127

    @pytest.mark.asyncio
    async def test_crash_blocks_fail_closed(self, monkeypatch):
        # Exercise the generic-exception branch of run_script_hook by making the
        # spawn itself raise; run_script_hook must still return a result (never
        # propagate) with the default exit_code -1, which is failed_to_run.
        async def _boom(*args, **kwargs):
            raise RuntimeError("spawn exploded")

        # run_script_hook imports sandboxed_spawn_argv_async from
        # kiro_crew.sandbox at call time, so patch it at that source module.
        # ``raising`` stays at its default True: if the symbol is ever renamed,
        # this must go red rather than silently spawn a real shell and pass on a
        # different failure.
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "sandboxed_spawn_argv_async", _boom)
        hook = ScriptHook(
            id="pt-crash",
            name="policy-crash",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="exit 0",
            timeout=5,
        )
        result = await run_script_hook(hook)
        # The injected crash is what produced this result, not an unrelated
        # spawn failure — the command itself would have exited 0 (allow).
        assert "spawn exploded" in result.error
        assert result.exit_code not in (0, 2)
        assert result.failed_to_run is True
        assert result.should_block_pre_tool_use() is True

    @pytest.mark.asyncio
    async def test_sandbox_refusal_blocks_fail_closed(self):
        # ``wrap_argv`` RAISES when no sandbox backend is available and
        # unsandboxed exec is not opted into, and the launcher it builds refuses
        # (non-zero exit, no hook verdict) on a host that cannot establish a
        # mandatory control. Either way the policy hook is unheard, so the gate
        # must deny — a host that cannot confine a hook must not become a host
        # that auto-approves every tool.
        hook = ScriptHook(
            id="pt-nosandbox",
            name="policy-nosandbox",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="exit 0",  # would ALLOW if it ever ran
            timeout=5,
        )
        with patch(
            "kiro_crew.sandbox.wrap_argv",
            side_effect=RuntimeError("no sandbox backend available"),
        ):
            result = await run_script_hook(hook)
        assert "no sandbox backend available" in result.error
        assert result.failed_to_run is True
        assert result.should_block_pre_tool_use() is True

    @pytest.mark.asyncio
    async def test_clean_exit2_blocks_and_is_not_failed_to_run(self):
        hook = ScriptHook(
            id="pt-deny",
            name="policy-deny",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="exit 2",
            timeout=5,
        )
        result = await run_script_hook(hook)
        assert result.exit_code == 2
        assert result.blocked is True
        # A clean, intentional denial is NOT a failed run.
        assert result.failed_to_run is False
        assert result.should_block_pre_tool_use() is True

    @pytest.mark.asyncio
    async def test_clean_exit0_allows(self):
        hook = ScriptHook(
            id="pt-allow",
            name="policy-allow",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="exit 0",
            timeout=5,
        )
        result = await run_script_hook(hook)
        assert result.exit_code == 0
        assert result.succeeded is True
        assert result.failed_to_run is False
        assert result.should_block_pre_tool_use() is False


class TestPerHookFailDirection:
    """The per-hook ``on_error`` field and its event-dependent default."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        _patch_sandbox_passthrough(monkeypatch)

    @pytest.mark.asyncio
    async def test_explicit_fail_open_pretooluse_does_not_block_on_timeout(
        self, tmp_path: Path
    ):
        # An operator who opted a PreToolUse hook into fail_open restores the
        # historic pass-through even when the hook fails to run.
        hook = ScriptHook(
            id="pt-open",
            name="policy-open",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command=_sleep_command(tmp_path / "slow_gate.py", 10),
            timeout=1,
            on_error=HOOK_ON_ERROR_FAIL_OPEN,
        )
        result = await run_script_hook(hook)
        assert "Timed out" in result.error  # the timeout branch, not any failure
        assert result.failed_to_run is True
        # Resolved fail_open -> a failed run must NOT manufacture a block.
        assert result.should_block_pre_tool_use() is False

    def test_default_pretooluse_resolves_fail_closed(self):
        hook = ScriptHook(event=HOOK_EVENT_PRE_TOOL_USE)
        assert hook.on_error == HOOK_ON_ERROR_DEFAULT
        assert hook.effective_on_error() == HOOK_ON_ERROR_FAIL_CLOSED

    def test_default_non_pretooluse_resolves_fail_open(self):
        hook = ScriptHook(event=HOOK_EVENT_USER_PROMPT_SUBMIT)
        assert hook.on_error == HOOK_ON_ERROR_DEFAULT
        assert hook.effective_on_error() == HOOK_ON_ERROR_FAIL_OPEN

    @pytest.mark.asyncio
    async def test_non_gating_event_never_blocks_on_failure(self):
        # A non-PreToolUse hook that fails resolves fail_open and never gates.
        hook = ScriptHook(
            id="ups-fail",
            name="ups-fail",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_MISSING_BINARY_COMMAND,
            timeout=5,
        )
        result = await run_script_hook(hook)
        assert result.exit_code not in (0, 2)
        assert result.failed_to_run is True
        assert result.should_block_pre_tool_use() is False


class TestOnErrorConfigPlumbing:
    """from_dict fail-soft, to_dict round-trip, validate_hook_fields boundary."""

    def test_from_dict_tolerates_junk_string(self):
        hook = ScriptHook.from_dict(
            {"event": HOOK_EVENT_PRE_TOOL_USE, "command": "true", "on_error": "nonsense"}
        )
        # Junk degrades to the sentinel (never raises) which still resolves
        # fail_closed for PreToolUse — more protection, not less.
        assert hook.on_error == HOOK_ON_ERROR_DEFAULT
        assert hook.effective_on_error() == HOOK_ON_ERROR_FAIL_CLOSED

    def test_from_dict_tolerates_non_string(self):
        hook = ScriptHook.from_dict(
            {"event": HOOK_EVENT_PRE_TOOL_USE, "command": "true", "on_error": 123}
        )
        assert hook.on_error == HOOK_ON_ERROR_DEFAULT

    def test_from_dict_keeps_valid_explicit_value(self):
        hook = ScriptHook.from_dict(
            {
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "command": "true",
                "on_error": "fail_open",
            }
        )
        assert hook.on_error == HOOK_ON_ERROR_FAIL_OPEN

    def test_to_dict_round_trip_explicit(self):
        hook = ScriptHook(
            id="rt",
            name="rt",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command="true",
            on_error=HOOK_ON_ERROR_FAIL_OPEN,
        )
        d = hook.to_dict()
        assert d["on_error"] == HOOK_ON_ERROR_FAIL_OPEN
        restored = ScriptHook.from_dict(d)
        assert restored.on_error == HOOK_ON_ERROR_FAIL_OPEN
        assert restored.effective_on_error() == HOOK_ON_ERROR_FAIL_OPEN

    def test_validate_hook_fields_rejects_invalid_on_error(self):
        with pytest.raises(ValueError):
            validate_hook_fields(
                event=HOOK_EVENT_PRE_TOOL_USE,
                timeout=30,
                command="true",
                skills=[],
                matcher="",
                matcher_mode="glob",
                on_error="banana",
            )

    def test_validate_hook_fields_accepts_valid_on_error(self):
        # Sentinel and both explicit spellings must be accepted (no raise).
        for value in (HOOK_ON_ERROR_DEFAULT, HOOK_ON_ERROR_FAIL_CLOSED, HOOK_ON_ERROR_FAIL_OPEN):
            validate_hook_fields(
                event=HOOK_EVENT_PRE_TOOL_USE,
                timeout=30,
                command="true",
                skills=[],
                matcher="",
                matcher_mode="glob",
                on_error=value,
            )


class TestFireToolHooksSurfacesFailClosedGap:
    """The autonomous path (fire_tool_hooks) surfaces a non-silent WARNING for a
    fail-closed PreToolUse hook that blocked or failed to run, and stays
    non-fatal.

    fire_tool_hooks cannot retroactively block (the tool is already running),
    but pre-fix it DISCARDED the results silently. These tests assert the new
    WARNING is emitted for the fail-closed failed-to-run and blocked cases and
    NOT for a clean allow, and that an exception inside fire() never propagates.
    """

    def _result(self, exit_code: int, *, event=HOOK_EVENT_PRE_TOOL_USE, on_error="", error=""):
        return ScriptHookResult(
            hook_id="h1",
            hook_name="policy-hook",
            event=event,
            exit_code=exit_code,
            error=error,
            on_error=on_error,
        )

    @pytest.mark.asyncio
    async def test_failed_to_run_fail_closed_emits_warning(self, hook_store, caplog):
        # exit_code -1 (timeout/crash surrogate) on a default PreToolUse hook.
        result = self._result(-1, error="Timed out after 1s")
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, return_value=[result]
        ):
            with caplog.at_level("WARNING", logger="kiro_crew.hooks"):
                await fire_tool_hooks(hook_store, "Running: ReadFile")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "policy-hook" in msg
        assert "#7339" in msg

    @pytest.mark.asyncio
    async def test_blocked_fail_closed_emits_warning(self, hook_store, caplog):
        result = self._result(2)  # clean deny, but tool already ran
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, return_value=[result]
        ):
            with caplog.at_level("WARNING", logger="kiro_crew.hooks"):
                await fire_tool_hooks(hook_store, "Running: ReadFile")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "would have BLOCKED" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_clean_exit0_emits_no_warning(self, hook_store, caplog):
        result = self._result(0)
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, return_value=[result]
        ):
            with caplog.at_level("WARNING", logger="kiro_crew.hooks"):
                await fire_tool_hooks(hook_store, "Running: ReadFile")
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    @pytest.mark.asyncio
    async def test_fail_open_failed_to_run_emits_no_warning(self, hook_store, caplog):
        # An explicit fail_open PreToolUse hook that failed to run is not a gap.
        result = self._result(-1, on_error=HOOK_ON_ERROR_FAIL_OPEN, error="boom")
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, return_value=[result]
        ):
            with caplog.at_level("WARNING", logger="kiro_crew.hooks"):
                await fire_tool_hooks(hook_store, "Running: ReadFile")
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    @pytest.mark.asyncio
    async def test_fire_exception_is_non_fatal(self, hook_store, caplog):
        # An exception inside fire() must NOT propagate out of fire_tool_hooks
        # (the "informational hooks must never break dispatch" contract).
        with patch.object(
            hook_store,
            "fire",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level("WARNING", logger="kiro_crew.hooks"):
                await fire_tool_hooks(hook_store, "Running: ReadFile")
        # No WARNING (the gap-surfacing path is skipped) and, crucially, no raise.
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []


class TestOnErrorApiSchema:
    """The dashboard API write path must accept ``on_error`` and thread it to
    the store.

    ``validate_tool_args`` rejects unknown fields, so before ``on_error`` was
    declared on ``HOOK_CREATE_SCHEMA`` / ``HOOK_UPDATE_SCHEMA`` a request
    carrying it got a 400 and the per-hook override was reachable only by
    hand-editing ``hooks.json``. These tests exercise the exact schema the
    dashboard handlers call so the gap cannot silently reopen.
    """

    def test_create_schema_accepts_on_error(self):
        from kiro_crew.validation import HOOK_CREATE_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {
                "name": "gate",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "on_error": HOOK_ON_ERROR_FAIL_OPEN,
            },
            HOOK_CREATE_SCHEMA,
        )
        assert cleaned["on_error"] == HOOK_ON_ERROR_FAIL_OPEN

    def test_create_schema_defaults_on_error_to_sentinel(self):
        from kiro_crew.validation import HOOK_CREATE_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {"name": "gate", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE},
            HOOK_CREATE_SCHEMA,
        )
        # Absent -> included with the sentinel default (not treated as unknown).
        assert cleaned["on_error"] == HOOK_ON_ERROR_DEFAULT

    def test_create_schema_rejects_invalid_on_error(self):
        from kiro_crew.validation import (
            HOOK_CREATE_SCHEMA,
            ValidationError,
            validate_tool_args,
        )

        with pytest.raises(ValidationError):
            validate_tool_args(
                {
                    "name": "gate",
                    "command": "true",
                    "event": HOOK_EVENT_PRE_TOOL_USE,
                    "on_error": "banana",
                },
                HOOK_CREATE_SCHEMA,
            )

    def test_update_schema_accepts_on_error(self):
        from kiro_crew.validation import HOOK_UPDATE_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {"on_error": HOOK_ON_ERROR_FAIL_CLOSED}, HOOK_UPDATE_SCHEMA
        )
        assert cleaned["on_error"] == HOOK_ON_ERROR_FAIL_CLOSED

    def test_update_schema_omits_on_error_when_absent(self):
        from kiro_crew.validation import HOOK_UPDATE_SCHEMA, validate_tool_args

        # Optional on update: absent stays absent (partial-update semantics),
        # so it never clobbers an existing value.
        cleaned = validate_tool_args({"name": "x"}, HOOK_UPDATE_SCHEMA)
        assert "on_error" not in cleaned

    def test_create_schema_output_flows_to_store(self, hook_store):
        from kiro_crew.validation import HOOK_CREATE_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {
                "name": "gate",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "on_error": HOOK_ON_ERROR_FAIL_OPEN,
            },
            HOOK_CREATE_SCHEMA,
        )
        # The handler passes `cleaned` straight to store.create; assert the
        # field survives end to end onto the persisted hook.
        hook = hook_store.create(cleaned)
        assert hook.on_error == HOOK_ON_ERROR_FAIL_OPEN


class TestOnErrorStoreNormalizationParity:
    """``create`` and ``update`` must normalize ``on_error`` identically.

    Pre-fix ``update`` set the raw request value while ``create`` normalized via
    ``from_dict``, so ``" Fail_Closed "`` passed create but was rejected by
    update's strict ``validate_hook_fields``. Both paths must now trim,
    lowercase, and degrade junk to the sentinel the same way.
    """

    def test_update_normalizes_mixed_case_whitespace(self, hook_store):
        hook = hook_store.create(
            {"name": "gate", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE}
        )
        updated = hook_store.update(hook.id, {"on_error": " Fail_Open "})
        assert updated is not None
        assert updated.on_error == HOOK_ON_ERROR_FAIL_OPEN

    def test_update_degrades_junk_to_sentinel(self, hook_store):
        hook = hook_store.create(
            {"name": "gate", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE}
        )
        updated = hook_store.update(hook.id, {"on_error": "garbage"})
        assert updated is not None
        # Junk degrades to the sentinel (matches create/from_dict), which still
        # resolves fail_closed for PreToolUse — never raises, never fails open.
        assert updated.on_error == HOOK_ON_ERROR_DEFAULT

    def test_create_and_update_agree_for_same_input(self, tmp_path):
        # Same mixed-case/whitespace input must yield the same stored value
        # whether it arrives via create or via update.
        raw = " FAIL_closed "
        store_a = ScriptHookStore(tmp_path / "a")
        created = store_a.create(
            {
                "name": "gate",
                "command": "true",
                "event": HOOK_EVENT_PRE_TOOL_USE,
                "on_error": raw,
            }
        )

        store_b = ScriptHookStore(tmp_path / "b")
        base = store_b.create(
            {"name": "gate", "command": "true", "event": HOOK_EVENT_PRE_TOOL_USE}
        )
        updated = store_b.update(base.id, {"on_error": raw})

        assert updated is not None
        assert created.on_error == updated.on_error == HOOK_ON_ERROR_FAIL_CLOSED
