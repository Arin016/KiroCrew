"""Red->green harness for the KiroClaw installer fixes.

These tests pin down the two defects that silently degrade the *packaged*
(PyInstaller / Electron) install — the dev/source tree masks both:

Defect A — frozen-app binary resolution
    ``_resolve_kiroclaw_bin()`` walks the source tree for ``bin/kiroclaw`` and
    consults ``PATH``, but never looks at ``sys.executable``.  In the shipped
    app the package lives at ``.../kiroclaw-backend/_internal/kiro_claw`` and
    the executable is ``kiroclaw-backend`` (not ``bin/kiroclaw``), so every
    step misses and it falls back to the bare string ``"kiroclaw"``.  Because
    that bare command is not on ``PATH``, ``build_agent_config`` /
    ``rebuild_agent_config`` then DROP ``kiroclaw-core`` and ``kiroclaw-cron``
    — taking ``spawn_run``, ``cron_add``, ``learn_add`` … offline.

    NOTE: a live ``kiroclaw mcp-core`` stdio handshake would PASS even with the
    bug present (the server code is healthy — it just never gets launched).
    The real regression gate is at the *resolution / wiring* level, which is
    what these tests assert: the managed-server command must be an absolute,
    existing, executable path so the validation loop keeps it.

Defect B — stale predecessor MCP entries
    ``clean_stale_managed_mcp()`` only removes ``kiroclaw-*`` entries, so the
    MeshClaw predecessor's ``meshclaw-core`` / ``meshclaw-cron`` entries (left
    in ``~/.kiro/settings/mcp.json`` by the rename) are never purged.

Both tests FAIL against the pre-fix code, proving they catch the real bug.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import kiro_claw.agent as agent
import kiro_claw.mcp_cleanup as mcp_cleanup


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fake_frozen_exe(tmp_path: Path) -> Path:
    """A real, executable stand-in for the bundled ``kiroclaw-backend``."""
    exe = tmp_path / "kiroclaw-backend"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def _bundled_defaults(tmp_path: Path) -> Path:
    """Minimal bundled defaults.json + prompt; returns the config dir."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    defaults = {
        "model": "claude-default",
        "tools": [],
        "allowedTools": [],
        "mcpServers": {},
        "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
        "hooks": {"preToolUse": "audit"},
    }
    (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
    (cfg_dir / "prompt.md").write_text("system prompt")
    return cfg_dir


def _simulate_frozen_app(monkeypatch, exe: Path) -> None:
    """Make the running process look like the shipped PyInstaller app:
    a real ``sys.executable``, nothing usable in the source tree, and no
    ``kiroclaw`` on PATH."""
    monkeypatch.setattr(agent, "_KIROCLAW_BIN", "", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    # No `bin/kiroclaw` / `.venv/bin/kiroclaw` is usable (force the dev tree
    # candidates to be rejected so the test is independent of where it runs).
    monkeypatch.setattr(agent, "_bin_is_usable", lambda p: str(p) == str(exe))
    # `kiroclaw` is not on PATH; only absolute paths resolve.
    monkeypatch.setattr(
        agent.shutil, "which", lambda c, **kw: c if str(c).startswith("/") else None
    )


# --------------------------------------------------------------------------
# Defect A — resolver
# --------------------------------------------------------------------------
def test_resolver_prefers_frozen_executable(tmp_path, monkeypatch):
    """In a frozen app, the resolver must return ``sys.executable`` (the
    bundled ``kiroclaw-backend``) instead of the bare ``"kiroclaw"``."""
    exe = _fake_frozen_exe(tmp_path)
    _simulate_frozen_app(monkeypatch, exe)

    resolved = agent._resolve_kiroclaw_bin()

    assert resolved == str(exe), (
        "frozen app must resolve to sys.executable, not bare 'kiroclaw' "
        f"(got {resolved!r})"
    )


# --------------------------------------------------------------------------
# Defect A — managed servers survive (no longer dropped)
# --------------------------------------------------------------------------
def test_managed_servers_survive_in_frozen_app(tmp_path, monkeypatch):
    """build_agent_config() must give kiroclaw-core/kiroclaw-cron an absolute,
    existing, executable command — the exact predicate the rebuild validation
    loop uses to KEEP (vs. drop) a server."""
    exe = _fake_frozen_exe(tmp_path)
    cfg_dir = _bundled_defaults(tmp_path)
    _simulate_frozen_app(monkeypatch, exe)

    with ExitStack() as stack:
        stack.enter_context(
            patch("kiro_claw.agent._shipped_defaults", return_value=cfg_dir / "defaults.json")
        )
        stack.enter_context(
            patch.multiple(
                "kiro_claw.agent",
                _BUNDLED_CFG_DIR=cfg_dir,
                _USER_OVERRIDES=tmp_path / "missing_overrides.json",
            )
        )
        stack.enter_context(
            patch("kiro_claw.agent._prompt_path", return_value=cfg_dir / "prompt.md")
        )
        stack.enter_context(
            patch("kiro_claw.agent._mc_config_path", return_value=tmp_path / "missing_mc.json")
        )
        config = agent.build_agent_config()

    servers = config.get("mcpServers", {})
    for name in ("kiroclaw-core", "kiroclaw-cron"):
        assert name in servers, f"{name} missing from generated config"
        cmd = servers[name]["command"]
        assert cmd == str(exe), f"{name} command should be the frozen exe, got {cmd!r}"
        # This is the literal keep-condition from rebuild_agent_config's
        # validation loop; if it fails the server would be DROPPED.
        assert (
            os.path.isabs(cmd) and os.path.isfile(cmd) and os.access(cmd, os.X_OK)
        ), f"{name} command {cmd!r} would be DROPPED by validation in the frozen app"


# --------------------------------------------------------------------------
# Defect B — stale meshclaw-* purge
# --------------------------------------------------------------------------
def test_cleanup_purges_meshclaw_predecessor_entries(tmp_path, monkeypatch):
    """clean_stale_managed_mcp() must remove the predecessor's meshclaw-core /
    meshclaw-cron entries (and the kiroclaw-* ones) while preserving genuine
    user-installed servers."""
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kiroclaw-core": {"command": "kiroclaw", "args": ["mcp-core"]},
                    "kiroclaw-cron": {"command": "kiroclaw", "args": ["mcp-cron"]},
                    "meshclaw-core": {
                        "command": "/old/MeshClaw/bin/meshclaw",
                        "args": ["mcp-core"],
                    },
                    "meshclaw-cron": {
                        "command": "/old/MeshClaw/bin/meshclaw",
                        "args": ["mcp-cron"],
                    },
                    "ai-community-slack-mcp": {
                        "command": "ai-community-slack-mcp",
                        "args": [],
                    },
                }
            },
            indent=2,
        )
    )
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", mcp_path)

    removed = mcp_cleanup.clean_stale_managed_mcp()
    remaining = set(json.loads(mcp_path.read_text())["mcpServers"])

    assert "meshclaw-core" not in remaining, "stale meshclaw-core not purged"
    assert "meshclaw-cron" not in remaining, "stale meshclaw-cron not purged"
    assert "kiroclaw-core" not in remaining
    assert "kiroclaw-cron" not in remaining
    # genuine user-installed server must be preserved
    assert "ai-community-slack-mcp" in remaining, "purge must not touch user servers"
    assert {"meshclaw-core", "meshclaw-cron"} <= set(removed)


# --------------------------------------------------------------------------
# Shim install — mirrors install.sh for install paths that skip it (the app)
# --------------------------------------------------------------------------
def test_ensure_kiroclaw_on_path_creates_shim(tmp_path, monkeypatch):
    """A frozen app with no `kiroclaw` on PATH must get a shim pointing at the
    bundled binary."""
    exe = _fake_frozen_exe(tmp_path)
    _simulate_frozen_app(monkeypatch, exe)
    bin_dir = tmp_path / "localbin"

    created = agent.ensure_kiroclaw_on_path(bin_dir=bin_dir)

    link = bin_dir / "kiroclaw"
    assert created == str(link)
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(exe)
    assert os.access(link, os.X_OK), "shim must be executable"


def test_ensure_kiroclaw_on_path_idempotent(tmp_path, monkeypatch):
    """Re-running setup when the shim is already correct is a no-op."""
    exe = _fake_frozen_exe(tmp_path)
    _simulate_frozen_app(monkeypatch, exe)
    bin_dir = tmp_path / "localbin"

    assert agent.ensure_kiroclaw_on_path(bin_dir=bin_dir) is not None
    assert agent.ensure_kiroclaw_on_path(bin_dir=bin_dir) is None
    assert os.path.realpath(bin_dir / "kiroclaw") == os.path.realpath(exe)


# --------------------------------------------------------------------------
# First-run auto-delivery (gateway path) — shim every start, purge once.
# The desktop app launches `kiroclaw gateway` (never `kiroclaw setup`), so
# run_first_run_setup() delivers both automatically.
# --------------------------------------------------------------------------
def _seed_global_mcp(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {
        "kiroclaw-core": {"command": "kiroclaw", "args": ["mcp-core"]},
        "kiroclaw-cron": {"command": "kiroclaw", "args": ["mcp-cron"]},
        "meshclaw-core": {"command": "/old/MeshClaw/bin/meshclaw", "args": ["mcp-core"]},
        "meshclaw-cron": {"command": "/old/MeshClaw/bin/meshclaw", "args": ["mcp-cron"]},
        "ai-community-slack-mcp": {"command": "ai-community-slack-mcp", "args": []},
    }}, indent=2))


def _sandbox_first_run(tmp_path, monkeypatch, exe):
    """Point first-run's home-derived + module-constant paths into tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))          # shim default ~/.local/bin
    mig = tmp_path / ".migrations"
    marker = mig / "stale_managed_mcp_purged"
    mcp = tmp_path / ".kiro" / "settings" / "mcp.json"
    monkeypatch.setattr(agent, "_MIGRATIONS_DIR", mig)
    monkeypatch.setattr(agent, "_STALE_MCP_PURGE_MARKER", marker)
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", mcp)
    _simulate_frozen_app(monkeypatch, exe)
    return marker, mcp


def test_first_run_delivers_shim_and_purge(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    marker, mcp = _sandbox_first_run(tmp_path, monkeypatch, exe)
    _seed_global_mcp(mcp)

    agent.run_first_run_setup()

    # shim created under sandbox ~/.local/bin
    link = tmp_path / ".local" / "bin" / "kiroclaw"
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(exe)
    # stale managed entries purged, genuine user server preserved
    remaining = set(json.loads(mcp.read_text())["mcpServers"])
    assert {"meshclaw-core", "meshclaw-cron", "kiroclaw-core", "kiroclaw-cron"}.isdisjoint(remaining)
    assert "ai-community-slack-mcp" in remaining
    # one-time marker written
    assert marker.exists()


def test_first_run_purge_is_one_time(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    marker, mcp = _sandbox_first_run(tmp_path, monkeypatch, exe)
    # Already migrated: marker present.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n")
    # A stale entry reappears after migration (e.g. user re-imports old config).
    _seed_global_mcp(mcp)
    before = mcp.read_text()

    agent.run_first_run_setup()

    # purge must NOT run again — global mcp.json untouched, stale entries stay
    assert mcp.read_text() == before
    assert "meshclaw-core" in set(json.loads(mcp.read_text())["mcpServers"])
    # but the shim is still ensured on every start
    assert (tmp_path / ".local" / "bin" / "kiroclaw").is_symlink()


def test_first_run_is_best_effort(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    marker, mcp = _sandbox_first_run(tmp_path, monkeypatch, exe)
    _seed_global_mcp(mcp)

    def _raise(*a, **k):
        raise OSError("shim boom")

    def _raise_purge():
        raise RuntimeError("purge boom")

    monkeypatch.setattr(agent, "ensure_kiroclaw_on_path", _raise)
    monkeypatch.setattr("kiro_claw.mcp_cleanup.clean_stale_managed_mcp", _raise_purge)

    # Must not propagate — gateway startup cannot be broken by setup failures.
    agent.run_first_run_setup()


# --------------------------------------------------------------------------
# Resolver: the frozen branch must NOT hijack non-frozen (source/dev) installs
# --------------------------------------------------------------------------
def test_resolver_nonfrozen_ignores_sys_executable(tmp_path, monkeypatch):
    path_bin = tmp_path / "kiroclaw"
    path_bin.write_text("#!/bin/sh\n")
    path_bin.chmod(0o755)
    interp = tmp_path / "python-interp"  # sys.executable when NOT frozen
    interp.write_text("x")
    interp.chmod(0o755)
    monkeypatch.setattr(agent, "_KIROCLAW_BIN", "", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(interp))
    # venv + bin-walk find nothing usable; only PATH resolves to path_bin.
    monkeypatch.setattr(agent, "_bin_is_usable", lambda p: str(p) == str(path_bin))
    monkeypatch.setattr(agent.shutil, "which", lambda c, **kw: str(path_bin) if c == "kiroclaw" else None)

    resolved = agent._resolve_kiroclaw_bin()
    assert resolved == str(path_bin)
    assert resolved != str(interp), "frozen branch must not fire when not frozen"


# --------------------------------------------------------------------------
# ensure_kiroclaw_on_path — edge cases
# --------------------------------------------------------------------------
def test_ensure_shim_noop_when_no_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_KIROCLAW_BIN", "", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(agent, "_bin_is_usable", lambda p: False)
    monkeypatch.setattr(agent.shutil, "which", lambda c, **kw: None)
    bin_dir = tmp_path / "localbin"
    assert agent.ensure_kiroclaw_on_path(bin_dir=bin_dir) is None
    assert not (bin_dir / "kiroclaw").exists()


def test_ensure_shim_refreshes_stale_symlink(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    _simulate_frozen_app(monkeypatch, exe)
    bin_dir = tmp_path / "localbin"
    bin_dir.mkdir()
    stale = tmp_path / "old-binary"
    stale.write_text("x")
    stale.chmod(0o755)
    (bin_dir / "kiroclaw").symlink_to(stale)

    created = agent.ensure_kiroclaw_on_path(bin_dir=bin_dir)
    assert created == str(bin_dir / "kiroclaw")
    assert os.path.realpath(bin_dir / "kiroclaw") == os.path.realpath(exe)


def test_ensure_shim_noop_when_already_on_path(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    monkeypatch.setattr(agent, "_KIROCLAW_BIN", "", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(agent, "_bin_is_usable", lambda p: str(p) == str(exe))
    # `kiroclaw` already resolves on PATH to the SAME binary.
    monkeypatch.setattr(agent.shutil, "which", lambda c, **kw: str(exe) if c == "kiroclaw" else None)
    bin_dir = tmp_path / "localbin"
    assert agent.ensure_kiroclaw_on_path(bin_dir=bin_dir) is None
    assert not (bin_dir / "kiroclaw").exists()


# --------------------------------------------------------------------------
# clean_stale_managed_mcp — edge cases + command-based (playwright) purge
# --------------------------------------------------------------------------
def test_clean_stale_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", tmp_path / "nope.json")
    assert mcp_cleanup.clean_stale_managed_mcp() == []


def test_clean_stale_malformed_json_untouched(tmp_path, monkeypatch):
    p = tmp_path / "mcp.json"
    p.write_text("{ not valid json ")
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", p)
    assert mcp_cleanup.clean_stale_managed_mcp() == []
    assert p.read_text() == "{ not valid json "  # left untouched


def test_clean_stale_no_stale_leaves_file_untouched(tmp_path, monkeypatch):
    p = tmp_path / "mcp.json"
    content = json.dumps({"mcpServers": {"ai-community-slack-mcp": {"command": "x", "args": []}}}, indent=2)
    p.write_text(content)
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", p)
    assert mcp_cleanup.clean_stale_managed_mcp() == []
    assert p.read_text() == content  # not rewritten when nothing to remove


def test_clean_stale_purges_meshclaw_command_playwright(tmp_path, monkeypatch):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {
        "npm:@playwright/mcp": {
            "command": "/Users/x/workspace/MeshClaw/env/MeshClaw-1.0/runtime/bin/meshclaw",
            "args": ["mcp-playwright-proxy", "--config", "/Users/x/.meshclaw/playwright-config.json"],
        },
        "@playwright/mcp": {"command": "kiroclaw", "args": ["mcp-playwright-proxy"]},
        "ai-community-slack-mcp": {"command": "ai-community-slack-mcp", "args": []},
    }}, indent=2))
    monkeypatch.setattr(mcp_cleanup, "_KIRO_MCP_JSON", p)

    removed = mcp_cleanup.clean_stale_managed_mcp()
    remaining = set(json.loads(p.read_text())["mcpServers"])

    assert "npm:@playwright/mcp" not in remaining  # stale meshclaw-command entry purged
    assert "npm:@playwright/mcp" in removed
    assert "@playwright/mcp" in remaining          # the live kiroclaw proxy kept
    assert "ai-community-slack-mcp" in remaining   # user server kept


def test_first_run_no_global_mcp(tmp_path, monkeypatch):
    exe = _fake_frozen_exe(tmp_path)
    marker, mcp = _sandbox_first_run(tmp_path, monkeypatch, exe)
    # No global mcp.json at all (clean fresh install).
    agent.run_first_run_setup()
    assert (tmp_path / ".local" / "bin" / "kiroclaw").is_symlink()
    assert marker.exists()          # marker written even with nothing to purge
    assert not mcp.exists()         # purge must not create a global mcp.json
