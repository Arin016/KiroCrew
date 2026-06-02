"""Tests for Claude Code session cleanup.

Validates:
- _encode_cc_project_dir encoding logic
- _cc_session_paths returns correct targets (excludes memory/)
- _cleanup_cc_session deletes files/dirs, leaves memory/ intact
- Idempotent: calling twice doesn't raise
- Path traversal defense
- ClaudeCodeProvider.cleanup_session integration
- AcpProvider.cleanup_session for claude backend
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_claw.providers.cleanup import (
    _cc_session_paths,
    _cleanup_cc_session,
    _encode_cc_project_dir,
)

# ══════════════════════════════════════════════════════════════════════
# _encode_cc_project_dir
# ══════════════════════════════════════════════════════════════════════


class TestEncodeCcProjectDir:
    """Encoding replaces every non-alphanumeric char with '-'."""

    def test_basic_unix_path(self):
        assert _encode_cc_project_dir("/Users/me/proj") == "-Users-me-proj"

    def test_hyphens_replaced(self):
        # Hyphens are non-alphanumeric, so they become '-' (no-op visually)
        assert _encode_cc_project_dir("/home/user/work-space") == "-home-user-work-space"

    def test_spaces_replaced(self):
        assert _encode_cc_project_dir("/home/user/my project") == "-home-user-my-project"

    def test_dots_replaced(self):
        result = _encode_cc_project_dir("/home/user/.config/app")
        assert result == "-home-user--config-app"

    def test_underscores_replaced(self):
        result = _encode_cc_project_dir("/tmp/my_workspace")
        assert result == "-tmp-my-workspace"

    def test_pure_alphanumeric_segment(self):
        # Only the leading '/' gets replaced
        result = _encode_cc_project_dir("/abc123")
        assert result == "-abc123"

    def test_path_object_input(self):
        result = _encode_cc_project_dir(Path("/Users/me/proj"))
        assert result == "-Users-me-proj"


# ══════════════════════════════════════════════════════════════════════
# _cc_session_paths
# ══════════════════════════════════════════════════════════════════════


class TestCcSessionPaths:
    """_cc_session_paths returns correct deletion targets.

    Paths are built under an explicit ``config_root`` so they're deterministic
    regardless of the ambient CC-isolation env. Coverage for the default
    (isolated vs ~/.claude) resolution lives in TestCcSessionPathsRoot below.
    """

    _ROOT = Path("/fake/cc-config")

    def test_returns_three_paths(self):
        paths = _cc_session_paths("/home/user/proj", "abc123", config_root=self._ROOT)
        assert len(paths) == 3

    def test_jsonl_transcript(self):
        paths = _cc_session_paths("/home/user/proj", "abc123", config_root=self._ROOT)
        encoded = _encode_cc_project_dir("/home/user/proj")
        expected = self._ROOT / "projects" / encoded / "abc123.jsonl"
        assert paths[0] == expected

    def test_session_subdir(self):
        paths = _cc_session_paths("/home/user/proj", "abc123", config_root=self._ROOT)
        encoded = _encode_cc_project_dir("/home/user/proj")
        expected = self._ROOT / "projects" / encoded / "abc123"
        assert paths[1] == expected

    def test_file_history_dir(self):
        paths = _cc_session_paths("/home/user/proj", "abc123", config_root=self._ROOT)
        expected = self._ROOT / "file-history" / "abc123"
        assert paths[2] == expected

    def test_does_not_include_memory(self):
        paths = _cc_session_paths("/home/user/proj", "abc123", config_root=self._ROOT)
        for p in paths:
            assert "memory" not in str(p)


class TestCcSessionPathsRoot:
    """The default config_root follows cc_config_root() (isolation-aware)."""

    def test_default_uses_isolated_root_when_enabled(self, monkeypatch, tmp_path):
        # Isolation ON (default) + CLAUDE_CONFIG_DIR override → paths under it.
        monkeypatch.delenv("KIROCLAW_CC_ISOLATE", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-config"))
        paths = _cc_session_paths("/home/user/proj", "abc123")
        assert str(paths[0]).startswith(str(tmp_path / "cc-config"))

    def test_default_falls_back_to_dot_claude_when_disabled(self, monkeypatch):
        # Isolation OFF → legacy ~/.claude behavior preserved.
        monkeypatch.setenv("KIROCLAW_CC_ISOLATE", "0")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        paths = _cc_session_paths("/home/user/proj", "abc123")
        expected = Path.home() / ".claude" / "projects"
        assert str(paths[0]).startswith(str(expected))


# ══════════════════════════════════════════════════════════════════════
# _cleanup_cc_session
# ══════════════════════════════════════════════════════════════════════


class TestCleanupCcSession:
    """_cleanup_cc_session deletes expected paths, leaves memory/ intact."""

    def test_deletes_jsonl_and_dirs(self, tmp_path):
        """Full cleanup: JSONL + session dir + file-history."""
        fake_home = tmp_path
        cwd = "/home/user/proj"
        sid = "test-session-id"
        encoded = _encode_cc_project_dir(cwd)

        # Create fake .claude structure
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)

        # Create session JSONL
        jsonl = project_dir / f"{sid}.jsonl"
        jsonl.write_text('{"test": true}\n')

        # Create session subdir (subagents + tool-results)
        session_dir = project_dir / sid
        session_dir.mkdir()
        (session_dir / "subagents").mkdir()
        (session_dir / "subagents" / "transcript.jsonl").write_text("")
        (session_dir / "tool-results").mkdir()
        (session_dir / "tool-results" / "result.json").write_text("{}")

        # Create file-history
        file_hist = fake_home / ".claude" / "file-history" / sid
        file_hist.mkdir(parents=True)
        (file_hist / "snapshot.txt").write_text("old content")

        # Create memory/ (must NOT be deleted)
        memory_dir = project_dir / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("# Memories")

        _cleanup_cc_session(cwd, sid, config_root=fake_home / ".claude")

        # Verify deleted
        assert not jsonl.exists()
        assert not session_dir.exists()
        assert not file_hist.exists()

        # Verify memory/ preserved
        assert memory_dir.exists()
        assert (memory_dir / "MEMORY.md").exists()

    def test_idempotent(self, tmp_path):
        """Calling cleanup twice doesn't raise."""
        fake_home = tmp_path
        cwd = "/home/user/proj"
        sid = "gone-session"

        # Create minimal structure
        encoded = _encode_cc_project_dir(cwd)
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)
        jsonl = project_dir / f"{sid}.jsonl"
        jsonl.write_text("")

        _cleanup_cc_session(cwd, sid, config_root=fake_home / ".claude")
        # Second call — everything already deleted
        _cleanup_cc_session(cwd, sid, config_root=fake_home / ".claude")

        assert not jsonl.exists()

    def test_empty_session_id_noop(self, tmp_path):
        """Empty session_id does nothing."""
        fake_home = tmp_path
        (fake_home / ".claude").mkdir(parents=True)
        root = fake_home / ".claude"

        _cleanup_cc_session("/home/user/proj", "", config_root=root)
        _cleanup_cc_session("/home/user/proj", ".", config_root=root)
        _cleanup_cc_session("/home/user/proj", "..", config_root=root)

    def test_partial_missing_paths(self, tmp_path):
        """Works when only some paths exist."""
        fake_home = tmp_path
        cwd = "/home/user/proj"
        sid = "partial-session"
        encoded = _encode_cc_project_dir(cwd)

        # Only create the JSONL, no session dir or file-history
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)
        jsonl = project_dir / f"{sid}.jsonl"
        jsonl.write_text("")

        _cleanup_cc_session(cwd, sid, config_root=fake_home / ".claude")

        assert not jsonl.exists()

    def test_path_traversal_blocked(self, tmp_path):
        """Traversal attempts are blocked (session_id with ../)."""
        fake_home = tmp_path
        cwd = "/home/user/proj"
        # This would try to escape .claude/ — should be blocked
        sid = "../../etc/passwd"

        (fake_home / ".claude" / "projects").mkdir(parents=True)

        # Should not raise and should not delete anything outside the root
        _cleanup_cc_session(cwd, sid, config_root=fake_home / ".claude")


# ══════════════════════════════════════════════════════════════════════
# ClaudeCodeProvider.cleanup_session
# ══════════════════════════════════════════════════════════════════════


class TestClaudeCodeProviderCleanup:
    """ClaudeCodeProvider.cleanup_session delegates to _cleanup_cc_session."""

    @pytest.mark.asyncio
    async def test_calls_cleanup_helper(self, tmp_path):
        """cleanup_session passes work_dir and session_id to helper."""
        from kiro_claw.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider(work_dir=tmp_path)

        with patch(
            "kiro_claw.providers.claude_code._cleanup_cc_session"
        ) as mock_cleanup:
            await provider.cleanup_session("test-uuid-123")

        mock_cleanup.assert_called_once_with(tmp_path, "test-uuid-123")

    @pytest.mark.asyncio
    async def test_empty_session_id_noop(self, tmp_path):
        """Empty session_id is handled gracefully (no-op in _cleanup_cc_session)."""
        from kiro_claw.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider(work_dir=tmp_path)

        with patch(
            "kiro_claw.providers.claude_code._cleanup_cc_session"
        ) as mock_cleanup:
            await provider.cleanup_session("")

        # _cleanup_cc_session is still called but returns immediately
        mock_cleanup.assert_called_once_with(tmp_path, "")


# ══════════════════════════════════════════════════════════════════════
# AcpProvider.cleanup_session (claude backend)
# ══════════════════════════════════════════════════════════════════════


class TestAcpProviderClaudeBackendCleanup:
    """AcpProvider.cleanup_session for claude backend uses CC cleanup."""

    @pytest.mark.asyncio
    async def test_claude_backend_calls_cc_cleanup(self, tmp_path):
        """Claude backend cleanup uses _cleanup_cc_session."""
        from kiro_claw.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path, acp_backend="claude")

        with (
            patch(
                "kiro_claw.providers.acp._cleanup_cc_session"
            ) as mock_cleanup,
            patch.object(provider._client, "_work_dir", tmp_path),
        ):
            await provider.cleanup_session("cc-session-456")

        mock_cleanup.assert_called_once_with(tmp_path, "cc-session-456")

    @pytest.mark.asyncio
    async def test_kiro_backend_not_affected(self, tmp_path):
        """Kiro backend cleanup is unchanged (does not call CC cleanup)."""
        from kiro_claw.providers.acp import AcpProvider

        provider = AcpProvider(work_dir=tmp_path)

        sessions_dir = tmp_path / ".kiro" / "sessions" / "cli"
        sessions_dir.mkdir(parents=True)
        json_file = sessions_dir / "kiro-session-id.json"
        jsonl_file = sessions_dir / "kiro-session-id.jsonl"
        json_file.write_text("{}")
        jsonl_file.write_text("")

        with patch("pathlib.Path.home", return_value=tmp_path):
            await provider.cleanup_session("kiro-session-id")

        assert not json_file.exists()
        assert not jsonl_file.exists()


class TestSubagentCcDetection:
    """SubagentManager._is_cc_provider must recognize the REAL default claude
    backend (AcpProvider acp_backend='claude'), not only the dead standalone
    ClaudeCodeProvider — otherwise CC subagent transcripts leak (cleanup hits
    the wrong ~/.kiro path)."""

    def test_is_cc_provider_true_for_acp_claude_backend(self, tmp_path):
        from kiro_claw.providers.acp import AcpProvider
        from kiro_claw.subagent import SubagentManager

        provider = AcpProvider(work_dir=tmp_path, acp_backend="claude")
        assert SubagentManager._is_cc_provider(provider) is True

    def test_is_cc_provider_false_for_kiro_backend(self, tmp_path):
        from kiro_claw.providers.acp import AcpProvider
        from kiro_claw.subagent import SubagentManager

        provider = AcpProvider(work_dir=tmp_path)  # default = kiro backend
        assert SubagentManager._is_cc_provider(provider) is False


class TestSubagentCcCleanupTargetsClaudePath:
    """A CC subagent must record provider='claude_code' + cwd so orphan/
    tombstone cleanup deletes the ~/.claude transcript, not the ~/.kiro path."""

    def test_recorded_cc_state_routes_cleanup_to_claude(self, tmp_path, monkeypatch):
        import kiro_claw.subagent_persistence as sp

        monkeypatch.setattr(sp, "_SUBAGENTS_DIR", tmp_path)
        work_dir = tmp_path / "ws"
        work_dir.mkdir()

        sp.create_agent_folder("cc1", task="t", agent="kiroclaw-lite")
        sp.update_state("cc1", session_id="sid-1", provider="claude_code", cwd=str(work_dir))

        state = sp.read_state("cc1")
        assert state["provider"] == "claude_code"
        assert state["cwd"] == str(work_dir)

        # Cleanup with the recorded CC state must route to _cleanup_cc_session
        # (the ~/.claude path), NOT the ~/.kiro unlink branch.
        with patch.object(sp, "_cleanup_cc_session") as mock_cc:
            sp._cleanup_session_files_sync("sid-1", "claude_code", cwd=str(work_dir))
        mock_cc.assert_called_once_with(str(work_dir), "sid-1")
