"""Tests for hook script mirroring: .kiro/hooks → .claude/hooks/.mirror."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from kiro_claw.mirror import (
    _copy_hook_scripts,
    _extract_hook_commands,
    _read_mirror_meta,
    _rewrite_hook_commands,
    mirror_kiro_to_cc,
)


@pytest.fixture
def hook_dirs(tmp_path: Path):
    """Set up fake kiro_root and cc_root with a hooks directory."""
    kiro_root = tmp_path / ".kiro"
    cc_root = tmp_path / ".claude"
    hooks_dir = kiro_root / "hooks"
    hooks_dir.mkdir(parents=True)
    cc_root.mkdir(parents=True)
    return {"kiro_root": kiro_root, "cc_root": cc_root, "hooks_dir": hooks_dir}


@pytest.fixture
def home_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up fake ~/.kiro and ~/.claude under tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    kiro = fake_home / ".kiro"
    claude = fake_home / ".claude"
    kiro.mkdir()
    claude.mkdir()
    return {"home": fake_home, "kiro": kiro, "claude": claude}


class TestCopyHookScripts:
    """Unit tests for _copy_hook_scripts."""

    def test_simple_path_copied_and_rewritten(self, hook_dirs):
        """Hook command ~/.kiro/hooks/check.sh -> copied + command rewritten."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
        script.chmod(0o755)

        command = str(script)
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        assert command in rename_map
        new_path = rename_map[command]
        assert ".claude/hooks/.mirror/check.sh" in new_path

        # File was actually copied
        dest = Path(new_path)
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == "#!/bin/bash\necho ok\n"

        # Mode bits preserved (executable)
        assert dest.stat().st_mode & stat.S_IXUSR

    def test_tilde_path_copied_and_rewritten(self, hook_dirs, monkeypatch):
        """Hook command with ~ prefix is handled correctly."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        # Monkeypatch home to match our tmp dirs
        monkeypatch.setenv("HOME", str(kiro_root.parent))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: kiro_root.parent))

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho tilde\n", encoding="utf-8")
        script.chmod(0o755)

        command = "~/.kiro/hooks/check.sh"
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        assert command in rename_map
        new_path = rename_map[command]
        assert "~/.claude/hooks/.mirror/check.sh" == new_path

    def test_multi_token_command_only_path_rewritten(self, hook_dirs):
        """Hook command with flags: only the path token is rewritten."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho multi\n", encoding="utf-8")
        script.chmod(0o755)

        command = f"bash {script} --foo"
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        # Only the path token should be in the map, not "bash" or "--foo"
        assert str(script) in rename_map
        assert "bash" not in rename_map
        assert "--foo" not in rename_map

    def test_non_kiro_path_passes_through_unchanged(self, hook_dirs):
        """Commands that don't reference kiro hooks are not touched."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]

        command = "/usr/local/bin/check.sh --verbose"
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        assert rename_map == {}

    def test_dry_run_reports_without_writing(self, hook_dirs):
        """dry_run=True reports what would copy without writing."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho dry\n", encoding="utf-8")
        script.chmod(0o755)

        command = str(script)
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root, dry_run=True
        )

        # Rename map is populated (for command rewriting)
        assert command in rename_map

        # But no file was written
        dest = Path(rename_map[command])
        assert not dest.exists()

        # No metadata file either
        meta_dir = cc_root / "hooks" / ".mirror"
        assert not meta_dir.exists()

    def test_existing_file_overwritten_safely(self, hook_dirs):
        """Existing .claude/hooks/.mirror/check.sh is overwritten on re-run."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho v1\n", encoding="utf-8")
        script.chmod(0o755)

        command = str(script)

        # First run
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )
        dest = Path(rename_map[command])
        assert dest.read_text(encoding="utf-8") == "#!/bin/bash\necho v1\n"

        # Modify source
        script.write_text("#!/bin/bash\necho v2\n", encoding="utf-8")

        # Second run overwrites
        rename_map2 = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )
        dest2 = Path(rename_map2[command])
        assert dest2.read_text(encoding="utf-8") == "#!/bin/bash\necho v2\n"

    def test_path_traversal_blocked(self, hook_dirs):
        """Symlinks that escape the hooks directory are refused."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        # Create a file outside hooks dir
        outside = kiro_root / "secrets" / "creds.sh"
        outside.parent.mkdir(parents=True)
        outside.write_text("SECRET=abc\n", encoding="utf-8")

        # Create a symlink inside hooks dir pointing outside
        link = hooks_dir / "evil.sh"
        link.symlink_to(outside)

        command = str(link)
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        # Should be refused — symlink escapes hooks dir
        assert command not in rename_map

    def test_sensitive_source_path_refused(self, hook_dirs, monkeypatch):
        """Defense-in-depth: a hook script whose resolved path is sensitive
        (credential path) is refused before being read/copied, even if it
        passes the under-hooks-dir check."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "looks-ok.sh"
        script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
        script.chmod(0o755)

        # Force the sensitivity check to flag this exact path.
        monkeypatch.setattr(
            "kiro_claw.mirror.is_sensitive_path",
            lambda p: p == str(script),
        )

        command = str(script)
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        assert command not in rename_map
        # And nothing was copied into the mirror dir.
        assert not (cc_root / "hooks" / ".mirror" / "looks-ok.sh").exists()

    def test_nonexistent_source_not_rewritten(self, hook_dirs):
        """If the source script doesn't exist (non-dry-run), it must NOT be
        added to rename_map — rewriting the agent markdown to a mirrored path
        that was never created produces a broken hook at runtime."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        # Path looks like it's under hooks but file doesn't exist
        command = str(hooks_dir / "missing.sh")
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        # Not rewritten: the original command stands rather than pointing at a
        # .mirror/ file that was never copied.
        assert command not in rename_map

    def test_nonexistent_source_still_previewed_in_dry_run(self, hook_dirs):
        """Dry-run still reports the intended rewrite (preview), even when the
        source is missing — it touches no disk and is informational only."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        command = str(hooks_dir / "missing.sh")
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root, dry_run=True
        )

        assert command in rename_map
        assert not Path(rename_map[command]).exists()

    def test_subdirectory_preserved(self, hook_dirs):
        """Scripts in subdirectories keep their relative path."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        sub = hooks_dir / "pre" / "lint.sh"
        sub.parent.mkdir(parents=True)
        sub.write_text("#!/bin/bash\nlint\n", encoding="utf-8")
        sub.chmod(0o755)

        command = str(sub)
        rename_map = _copy_hook_scripts(
            [command], kiro_root=kiro_root, cc_root=cc_root
        )

        assert command in rename_map
        dest = Path(rename_map[command])
        assert dest.is_file()
        assert "pre/lint.sh" in str(dest)

    def test_metadata_written(self, hook_dirs):
        """Mirror metadata JSON tracks copied files."""
        kiro_root = hook_dirs["kiro_root"]
        cc_root = hook_dirs["cc_root"]
        hooks_dir = hook_dirs["hooks_dir"]

        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho meta\n", encoding="utf-8")
        script.chmod(0o755)

        _copy_hook_scripts(
            [str(script)], kiro_root=kiro_root, cc_root=cc_root
        )

        meta = _read_mirror_meta(cc_root / "hooks")
        assert "check.sh" in meta
        assert len(meta["check.sh"]) == 64  # sha256 hex


class TestRewriteHookCommands:
    """Unit tests for _rewrite_hook_commands."""

    def test_rewrites_path_in_yaml(self):
        md = "---\nhooks:\n  command: /home/user/.kiro/hooks/check.sh\n---\n"
        result = _rewrite_hook_commands(
            md, {"/home/user/.kiro/hooks/check.sh": "/home/user/.claude/hooks/.mirror/check.sh"}
        )
        assert "/home/user/.claude/hooks/.mirror/check.sh" in result
        assert "/home/user/.kiro/hooks/check.sh" not in result

    def test_no_match_unchanged(self):
        md = "---\nhooks:\n  command: /usr/bin/check.sh\n---\n"
        result = _rewrite_hook_commands(md, {"/other/path": "/new/path"})
        assert result == md


class TestExtractHookCommands:
    """Unit tests for _extract_hook_commands."""

    def test_extracts_commands(self):
        data = {
            "hooks": {
                "preToolUse": [
                    {"matcher": "Bash", "command": "~/.kiro/hooks/check.sh"},
                    {"matcher": "Write", "command": "~/.kiro/hooks/lint.sh --strict"},
                ],
                "stop": [{"command": "/usr/bin/notify.sh"}],
            }
        }
        commands = _extract_hook_commands(data)
        assert "~/.kiro/hooks/check.sh" in commands
        assert "~/.kiro/hooks/lint.sh --strict" in commands
        assert "/usr/bin/notify.sh" in commands
        assert len(commands) == 3

    def test_empty_hooks(self):
        assert _extract_hook_commands({"hooks": {}}) == []
        assert _extract_hook_commands({}) == []

    def test_malformed_hooks_handled(self):
        assert _extract_hook_commands({"hooks": "not a dict"}) == []
        assert _extract_hook_commands({"hooks": {"event": "not a list"}}) == []


class TestMirrorIntegration:
    """Integration tests: mirror_kiro_to_cc copies and rewrites hook scripts."""

    def test_full_mirror_copies_hook_scripts(self, home_dirs):
        """End-to-end: agent with hook command -> script copied + path rewritten."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        # Create a hook script
        hooks_dir = kiro / "hooks"
        hooks_dir.mkdir()
        script = hooks_dir / "pre-check.sh"
        script.write_text("#!/bin/bash\necho pre-check\n", encoding="utf-8")
        script.chmod(0o755)

        # Create an agent with hooks referencing the script
        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        agent_data = {
            "name": "test-agent",
            "prompt": "Test prompt.",
            "hooks": {
                "preToolUse": [
                    {
                        "matcher": "Bash",
                        "command": str(script),
                    }
                ]
            },
        }
        (agents_dir / "test-agent.json").write_text(
            json.dumps(agent_data), encoding="utf-8"
        )

        result = mirror_kiro_to_cc(dry_run=False, force=True)

        # Agent was mirrored
        assert any(
            a["name"] == "test-agent" and a["action"] == "mirrored"
            for a in result["agents"]
        )

        # Script was copied to .claude/hooks/.mirror/
        mirror_script = claude / "hooks" / ".mirror" / "pre-check.sh"
        assert mirror_script.is_file()
        assert mirror_script.read_text(encoding="utf-8") == "#!/bin/bash\necho pre-check\n"
        assert mirror_script.stat().st_mode & stat.S_IXUSR

        # Agent markdown has rewritten path
        agent_md = claude / "agents" / "test-agent.md"
        assert agent_md.is_file()
        content = agent_md.read_text(encoding="utf-8")
        assert str(script) not in content
        assert str(mirror_script) in content

    def test_dry_run_no_files_written(self, home_dirs):
        """dry_run=True: no hook scripts copied, no agent files written."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        hooks_dir = kiro / "hooks"
        hooks_dir.mkdir()
        script = hooks_dir / "check.sh"
        script.write_text("#!/bin/bash\necho dry\n", encoding="utf-8")
        script.chmod(0o755)

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        agent_data = {
            "name": "dry-agent",
            "prompt": "Dry.",
            "hooks": {
                "preToolUse": [{"matcher": "Bash", "command": str(script)}]
            },
        }
        (agents_dir / "dry-agent.json").write_text(
            json.dumps(agent_data), encoding="utf-8"
        )

        result = mirror_kiro_to_cc(dry_run=True)

        assert any(
            a["name"] == "dry-agent" and a["action"] == "mirrored"
            for a in result["agents"]
        )
        # No files written
        assert not (claude / "hooks" / ".mirror" / "check.sh").exists()
        assert not (claude / "agents" / "dry-agent.md").exists()
