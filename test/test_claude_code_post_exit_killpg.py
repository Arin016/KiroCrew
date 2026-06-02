"""Tests for the post-exit killpg sweep in
``ClaudeCodeProvider._kill_persistent_process``.

After SIGTERM/wait/kill of the parent claude-code process, MCP stdio
children sometimes survive because they don't always exit on stdin close.
The fix sends a final ``killpg(SIGKILL)`` post-exit to reap any
stragglers — but ONLY when the process group is ours (spawned with
``start_new_session=True``).  These tests assert:

- killpg is called when the process group is owned (pgid != current pgid)
- killpg is NOT called when pgid resolution fails (no group to target)
- killpg is NOT called when the spawned process shares the current group
- ProcessLookupError / OSError from killpg are swallowed
- The post-exit sweep happens after the parent has exited (returncode set)

Tests use ``asyncio.run`` directly (rather than ``pytest.mark.asyncio``)
so they're portable across pytest-asyncio versions / availability.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from kiro_claw.providers.claude_code import ClaudeCodeProvider


@pytest.fixture
def provider():
    """Return a ClaudeCodeProvider with internal state pre-populated so
    we can drive ``_kill_persistent_process`` without spawning a real
    subprocess."""
    p = ClaudeCodeProvider(connection_mode="per_session")
    p._reader_task = None
    p._stderr_task = None
    p._sandbox_cleanup = None
    return p


class _FakeProc:
    """Plain class that mimics ``asyncio.subprocess.Process`` for the
    parts ``_kill_persistent_process`` reads.

    A standalone class avoids mutating ``unittest.mock.MagicMock`` (a
    shared parent type — assigning ``type(MagicMock_instance).returncode``
    pollutes every other MagicMock created in the same test session).
    """

    def __init__(self, returncode_after_terminate: int = 0, pid: int = 12345) -> None:
        self.pid = pid
        self._rc: int | None = None
        self._returncode_after_terminate = returncode_after_terminate
        self.terminate_calls = 0
        self.kill_calls = 0

    @property
    def returncode(self) -> int | None:
        return self._rc

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._rc = self._returncode_after_terminate

    def kill(self) -> None:
        self.kill_calls += 1
        self._rc = -9

    async def wait(self) -> int | None:
        return self._rc


def _make_fake_proc(returncode_after_terminate: int = 0, pid: int = 12345) -> _FakeProc:
    return _FakeProc(returncode_after_terminate=returncode_after_terminate, pid=pid)


def _run_kill_persistent(provider, fake_pgid_for_pid, killpg_side_effect=None):
    """Drive ``_kill_persistent_process`` synchronously with patched os
    interactions and return the captured killpg call args list."""
    import kiro_claw.providers.claude_code as cc

    killpg_calls: list = []

    def _killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        if killpg_side_effect is not None:
            raise killpg_side_effect

    original_getpgid = cc.os.getpgid
    original_killpg = cc.os.killpg
    original_remove = cc.os.remove
    cc.os.getpgid = fake_pgid_for_pid
    cc.os.killpg = _killpg
    # Avoid touching real paths.
    cc.os.remove = lambda *a, **kw: None
    try:
        asyncio.run(provider._kill_persistent_process())
    finally:
        cc.os.getpgid = original_getpgid
        cc.os.killpg = original_killpg
        cc.os.remove = original_remove
    return killpg_calls


def test_killpg_called_post_exit_when_group_is_owned(provider):
    """After SIGTERM exits cleanly, killpg(pgid, SIGKILL) is sent again."""
    proc = _make_fake_proc()
    provider._proc = proc

    fake_pgid = 99999
    current_pgid = 11111

    def getpgid(pid):
        return fake_pgid if pid == proc.pid else current_pgid

    calls = _run_kill_persistent(provider, getpgid)
    sigkill_calls = [c for c in calls if c == (fake_pgid, signal.SIGKILL)]
    assert sigkill_calls, "post-exit killpg(SIGKILL) was never called"


def test_killpg_skipped_when_pgid_matches_current(provider):
    """If pgid == os.getpgid(0), the group is shared with the parent —
    must not killpg or we'd kill the gateway itself."""
    proc = _make_fake_proc()
    provider._proc = proc

    same_pgid = 12345

    def getpgid(pid):
        return same_pgid

    calls = _run_kill_persistent(provider, getpgid)
    sigkill_calls = [c for c in calls if c[1] == signal.SIGKILL]
    assert not sigkill_calls, (
        f"killpg fired against shared group (own gateway pgid): {sigkill_calls}"
    )


def test_killpg_skipped_when_pgid_unresolvable(provider):
    """If os.getpgid() raises (process already dead), pgid is None and
    no killpg should fire."""
    proc = _make_fake_proc()
    provider._proc = proc

    def getpgid(pid):
        if pid == proc.pid:
            raise ProcessLookupError("dead")
        return 11111

    calls = _run_kill_persistent(provider, getpgid)
    assert not calls


def test_killpg_swallows_process_lookup_error(provider):
    """ProcessLookupError from killpg is swallowed — the post-exit sweep
    is best-effort."""
    proc = _make_fake_proc()
    provider._proc = proc

    def getpgid(pid):
        return 77777 if pid == proc.pid else 11111

    # Should not raise.
    calls = _run_kill_persistent(
        provider, getpgid, killpg_side_effect=ProcessLookupError("empty")
    )
    # killpg was attempted at least once.
    assert calls
    # Provider state cleaned up.
    assert provider._proc is None


def test_killpg_swallows_os_error(provider):
    """OSError from killpg is swallowed."""
    proc = _make_fake_proc()
    provider._proc = proc

    def getpgid(pid):
        return 77777 if pid == proc.pid else 11111

    calls = _run_kill_persistent(
        provider, getpgid, killpg_side_effect=OSError("permission denied")
    )
    assert calls
    assert provider._proc is None


def test_post_exit_sweep_runs_even_when_terminate_succeeds(provider):
    """The defense-in-depth comment promises the killpg fires even when
    SIGTERM exited the parent cleanly within the 3s wait."""
    proc = _make_fake_proc(returncode_after_terminate=0)
    provider._proc = proc

    fake_pgid = 88888

    def getpgid(pid):
        return fake_pgid if pid == proc.pid else 11111

    calls = _run_kill_persistent(provider, getpgid)
    sigkill = [c for c in calls if c == (fake_pgid, signal.SIGKILL)]
    assert sigkill, "post-exit killpg sweep did not run after clean SIGTERM"
