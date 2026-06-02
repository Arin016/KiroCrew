"""Tests for process tree tracking, recursive kill, and session cleanup."""

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.acp.client import (
    AcpClient,
    _direct_children,
    _get_child_pids,
    _is_our_child,
    _kill_escaped_children,
)

# ── 1. _get_child_pids: visited-set prevents infinite loops ──


class TestGetChildPidsVisitedSet:
    def test_cycle_terminates(self):
        """A→B→A cycle must not recurse infinitely."""
        call_count = 0

        def fake_direct(pid):
            nonlocal call_count
            call_count += 1
            return {1: [2], 2: [1]}.get(pid, [])

        with patch("kiro_claw.acp.client._direct_children", side_effect=fake_direct):
            result = _get_child_pids(1)
        assert result == [2]
        assert call_count <= 3

    def test_self_loop(self):
        with patch("kiro_claw.acp.client._direct_children", return_value=[42]):
            assert _get_child_pids(42) == []

    def test_diamond_deduplicates(self):
        tree = {1: [2, 3], 2: [4], 3: [4]}
        with patch("kiro_claw.acp.client._direct_children", side_effect=lambda p: tree.get(p, [])):
            assert sorted(_get_child_pids(1)) == [2, 3, 4]

    def test_none_pid(self):
        assert _get_child_pids(None) == []

    def test_no_children(self):
        with patch("kiro_claw.acp.client._direct_children", return_value=[]):
            assert _get_child_pids(999) == []

    def test_deep_chain(self):
        tree = {1: [2], 2: [3], 3: [4], 4: [5]}
        with patch("kiro_claw.acp.client._direct_children", side_effect=lambda p: tree.get(p, [])):
            assert _get_child_pids(1) == [2, 3, 4, 5]


# ── 2. _kill_escaped_children: handles dead PIDs and kills bottom-up ──


class TestKillEscapedChildren:
    def test_already_dead_pid(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            _kill_escaped_children({999: 100})  # should not raise

    def test_kills_verified_child(self):
        def fake_kill(pid, sig):
            if sig == 0:
                return
            assert sig == signal.SIGKILL

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_claw.acp.client._is_our_child", return_value=True),
        ):
            _kill_escaped_children({42: 100})

    def test_skips_recycled_pid(self):
        kills = []

        def fake_kill(pid, sig):
            kills.append((pid, sig))
            if sig == 0:
                return

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_claw.acp.client._is_our_child", return_value=False),
        ):
            _kill_escaped_children({42: 100})
        assert all(sig == 0 for _, sig in kills)

    def test_kills_leaf_first(self):
        killed = []

        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                killed.append(pid)

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_claw.acp.client._is_our_child", return_value=True),
        ):
            _kill_escaped_children({10: 1, 20: 2, 30: 3})
        assert killed == [30, 20, 10]


# ── 3. _is_our_child: allowlist and start-time verification ──


class TestIsOurChild:
    @pytest.fixture(autouse=True)
    def _force_linux(self):
        with patch("kiro_claw.acp.client.sys") as mock_sys:
            mock_sys.platform = "linux"
            yield

    def test_rejects_missing_proc(self):
        with patch("kiro_claw.acp.client.Path") as mock_path_cls:
            mock_path_cls.return_value.exists.return_value = False
            assert _is_our_child(999, expected_start=1) is False

    def test_rejects_unknown_binary(self):
        with patch("kiro_claw.acp.client.Path") as mock_path_cls:
            inst = mock_path_cls.return_value
            inst.exists.return_value = True
            inst.read_bytes.return_value = b"postgres\x00--flag"
            assert _is_our_child(999, expected_start=1) is False

    def test_rejects_start_time_mismatch(self):
        with (
            patch("kiro_claw.acp.client.Path") as mock_path_cls,
            patch("kiro_claw.acp.client._get_start_time", return_value=200),
        ):
            inst = mock_path_cls.return_value
            inst.exists.return_value = True
            inst.read_bytes.return_value = b"kiro-cli\x00acp"
            assert _is_our_child(999, expected_start=100) is False

    def test_accepts_matching_kiro(self):
        with (
            patch("kiro_claw.acp.client.Path") as mock_path_cls,
            patch("kiro_claw.acp.client._get_start_time", return_value=100),
        ):
            inst = mock_path_cls.return_value
            inst.exists.return_value = True
            inst.read_bytes.return_value = b"kiro-cli\x00acp"
            assert _is_our_child(999, expected_start=100) is True

    def test_accepts_mcp_in_name(self):
        with (
            patch("kiro_claw.acp.client.Path") as mock_path_cls,
            patch("kiro_claw.acp.client._get_start_time", return_value=50),
        ):
            inst = mock_path_cls.return_value
            inst.exists.return_value = True
            inst.read_bytes.return_value = b"builder-mcp\x00serve"
            assert _is_our_child(999, expected_start=50) is True

    def test_none_start_time_denied(self):
        with patch("kiro_claw.acp.client.Path") as mock_path_cls:
            inst = mock_path_cls.return_value
            inst.exists.return_value = True
            inst.read_bytes.return_value = b"kiro-cli\x00acp"
            assert _is_our_child(999, expected_start=None) is False


# ── 4. _direct_children: /proc and pgrep fallback ──


class TestDirectChildren:
    def test_proc_children_parsed(self):
        with (
            patch("kiro_claw.acp.client.sys") as mock_sys,
            patch("kiro_claw.acp.client.Path") as mock_path_cls,
        ):
            mock_sys.platform = "linux"
            mock_path = MagicMock()
            mock_path_cls.return_value = mock_path
            mock_path.is_dir.return_value = True
            child_file = MagicMock()
            child_file.exists.return_value = True
            child_file.read_text.return_value = "200 300 "
            tid = MagicMock()
            tid.__truediv__ = lambda self, x: child_file
            mock_path.iterdir.return_value = [tid]
            result = _direct_children(100)
        assert result == [200, 300]


# ── 5. _snapshot_process_tree: captures full descendant tree ──


class TestSnapshotProcessTree:
    @pytest.mark.asyncio
    async def test_tracks_all_descendants(self, tmp_path):
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        client._child_pids = {}

        with (
            patch("kiro_claw.acp.client._get_child_pids", return_value=[200, 300, 400]),
            patch("kiro_claw.acp.client._get_start_time", side_effect=lambda p: p * 10),
            patch("kiro_claw.session_pid.config_dir", return_value=tmp_path),
        ):
            await client._snapshot_process_tree()

        assert client._child_pids == {200: 2000, 300: 3000, 400: 4000}
        # Verify child:parent lines written to kiro_pids.txt
        content = (tmp_path / "kiro_pids.txt").read_text()
        lines = {ln.strip() for ln in content.splitlines() if ln.strip()}
        assert lines == {"200:100", "300:100", "400:100"}

    @pytest.mark.asyncio
    async def test_no_descendants_no_tracking(self):
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        client._child_pids = {}

        with patch("kiro_claw.acp.client._get_child_pids", return_value=[]):
            await client._snapshot_process_tree()

        assert client._child_pids == {}

    @pytest.mark.asyncio
    async def test_merges_early_and_late_snapshots(self, tmp_path):
        """Early snapshot from _spawn + late snapshot from _snapshot_process_tree merge."""
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        # Simulate early snapshot already captured PID 200
        client._child_pids = {200: 2000}

        with (
            patch("kiro_claw.acp.client._get_child_pids", return_value=[200, 300]),
            patch("kiro_claw.acp.client._get_start_time", side_effect=lambda p: p * 10),
            patch("kiro_claw.session_pid.config_dir", return_value=tmp_path),
        ):
            await client._snapshot_process_tree()

        # PID 200 keeps original start_time, PID 300 is new
        assert client._child_pids == {200: 2000, 300: 3000}


# ── 6. Session cleanup on cancellation ──


class TestSessionCleanupOnCancellation:
    """Verify _cleanup_run_sessions resets all session keys for a run."""

    def _make_mock_taskrunner(self, session_keys):
        """Create a minimal mock TaskRunner with fake sessions."""
        from unittest.mock import AsyncMock

        tr = MagicMock()
        tr._sessions = MagicMock()
        tr._sessions.get_pid = MagicMock(return_value=None)
        tr._sessions._sessions = {k: MagicMock() for k in session_keys}
        tr._sessions.cancel_current = AsyncMock()
        tr._sessions.release = MagicMock()
        tr._sessions.reset = AsyncMock()
        return tr

    @pytest.mark.asyncio
    async def test_cleanup_resets_all_matching_keys(self):
        """All sessions with the run prefix get cancelled and reset."""
        run = MagicMock()
        run.task_id = "abc123"
        keys = ["taskrunner:abc123:task0", "taskrunner:abc123:task1", "taskrunner:abc123:task2"]
        tr = self._make_mock_taskrunner(keys)

        # Import the real method and bind it
        from kiro_claw.taskrunner import TaskRunner

        cleanup = TaskRunner._cleanup_run_sessions

        await cleanup(tr, run)

        assert tr._sessions.cancel_current.call_count == 3
        assert tr._sessions.reset.call_count == 3
        for key in keys:
            tr._sessions.reset.assert_any_await(key)

    @pytest.mark.asyncio
    async def test_cleanup_ignores_other_runs(self):
        """Sessions from other runs are not touched."""
        run = MagicMock()
        run.task_id = "abc123"
        keys = ["taskrunner:abc123:task0", "taskrunner:other:task0"]
        tr = self._make_mock_taskrunner(keys)

        from kiro_claw.taskrunner import TaskRunner

        await TaskRunner._cleanup_run_sessions(tr, run)

        # Only 1 key matches prefix "taskrunner:abc123:"
        assert tr._sessions.cancel_current.call_count == 1
        assert tr._sessions.reset.call_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_handles_cancel_failure(self):
        """If cancel_current raises, reset is still called."""
        run = MagicMock()
        run.task_id = "abc123"
        keys = ["taskrunner:abc123:task0"]
        tr = self._make_mock_taskrunner(keys)
        tr._sessions.cancel_current = AsyncMock(side_effect=Exception("boom"))

        from kiro_claw.taskrunner import TaskRunner

        await TaskRunner._cleanup_run_sessions(tr, run)

        # reset still called despite cancel failure
        tr._sessions.reset.assert_awaited_once()


# ── 7. _track_pid / _untrack_pid file operations ──


class TestPidTracking:
    def test_track_and_untrack(self, tmp_path):
        """_track_pid appends, _untrack_pid removes."""
        from kiro_claw.session_pid import _track_pid, _untrack_pid

        pid_file = tmp_path / "pids.txt"
        with patch("kiro_claw.session_pid._pid_file_path", return_value=pid_file):
            _track_pid(100)
            _track_pid(200)
            _track_pid(300)
            assert "100" in pid_file.read_text()
            assert "200" in pid_file.read_text()

            _untrack_pid(200)
            content = pid_file.read_text()
            assert "200" not in content
            assert "100" in content
            assert "300" in content
