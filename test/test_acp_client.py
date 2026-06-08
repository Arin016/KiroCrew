"""Tests for ACP client."""

import asyncio
import json
import os
import signal
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.acp.client import (
    _CLAUDE_ACP_PKG_ENTRY,
    AcpClient,
    AcpError,
    AcpProcessDied,
    _claude_acp_mcp_servers,
    _format_acp_error,
    _make_unified_diff,
    _resolve_vendored_claude_acp,
    _vendored_claude_acp_roots,
)
from kiro_claw.acp.types import ACP_BACKEND_CLAUDE, AcpPromptStats


class TestVendoredClaudeAcp:
    """Resolve the vendored claude-agent-acp adapter (no npm/network)."""

    def _make_vendored(self, root: Path, *, with_deps: bool = True) -> Path:
        """Create a fake vendored adapter under *root*.

        With *with_deps* (default) also creates the hoisted dependency marker
        ``@agentclientprotocol/sdk`` so the completeness guard accepts it.
        """
        from kiro_claw.acp.client import _CLAUDE_ACP_DEP_MARKER

        entry = root / _CLAUDE_ACP_PKG_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake adapter\n", encoding="utf-8")
        if with_deps:
            (root / _CLAUDE_ACP_DEP_MARKER).mkdir(parents=True, exist_ok=True)
        return entry

    def test_finds_vendored_in_pkg_vendor_dir(self, tmp_path, monkeypatch):
        # Toolbox/pip layout: <pkg_dir>/_vendor/node_modules/<pkg>/dist/index.js.
        # Inject a fake pkg_dir under an isolated home so the real workspace
        # (sibling-website detection / KIROCLAW_PROJECT_DIR) is not consulted.
        monkeypatch.delenv("KIROCLAW_PROJECT_DIR", raising=False)
        pkg_dir = tmp_path / "site-packages" / "kiro_claw"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(pkg_dir / "_vendor" / "node_modules")
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_finds_vendored_under_project_dir(self, tmp_path, monkeypatch):
        # KIROCLAW_PROJECT_DIR/node_modules holds the adapter; pkg_dir has none.
        pkg_dir = tmp_path / "site-packages" / "kiro_claw"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(tmp_path / "proj" / "node_modules")
        (tmp_path / "proj").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCLAW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        # Isolated pkg_dir with no _vendor and a project dir with no adapter.
        monkeypatch.setenv("KIROCLAW_PROJECT_DIR", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        pkg_dir = tmp_path / "site-packages" / "kiro_claw"
        pkg_dir.mkdir(parents=True)
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) is None

    def test_skips_incomplete_copy_missing_deps(self, tmp_path, monkeypatch):
        # Regression: an entry script with no hoisted deps must be rejected
        # (it would crash with ERR_MODULE_NOT_FOUND @agentclientprotocol/sdk),
        # falling through to a complete copy under KIROCLAW_PROJECT_DIR.
        pkg_dir = tmp_path / "site-packages" / "kiro_claw"
        pkg_dir.mkdir(parents=True)
        # Incomplete copy in _vendor (entry only, no deps) — must be skipped.
        self._make_vendored(pkg_dir / "_vendor" / "node_modules", with_deps=False)
        # Complete copy in the project dir — must win.
        (tmp_path / "proj").mkdir()
        good = self._make_vendored(tmp_path / "proj" / "node_modules")
        monkeypatch.setenv("KIROCLAW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(good)

    def test_roots_include_pkg_vendor_dir(self):
        # The toolbox-bundle vendor location must always be the first candidate.
        roots = _vendored_claude_acp_roots()
        assert roots[0].name == "node_modules" and roots[0].parent.name == "_vendor"


class TestAcpClientInit:
    def test_defaults(self):
        client = AcpClient()
        assert not client.is_ready
        assert client._session_id is None

    def test_custom_work_dir(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        assert client._work_dir == tmp_path


class TestAcpClientSessionKey:
    def test_stores_session_key(self):
        client = AcpClient(session_key="test-key")
        assert client._session_key == "test-key"

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_session_key(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="test-key")
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCLAW_SESSION_KEY"] == "test-key"

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_channel_id(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id="C0ABC123")
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCLAW_CHANNEL_ID"] == "C0ABC123"
            assert env["KIROCLAW_SESSION_KEY"] == "k"

    @pytest.mark.asyncio
    async def test_spawn_forwards_claude_config_dir_from_extra_env(self, tmp_path):
        # The loader factory injects CLAUDE_CONFIG_DIR into cc_env (→ extra_env);
        # _spawn must forward it verbatim to the subprocess so the adapter's
        # SettingsManager reads the isolated dir (creds kept, plugins stripped).
        iso = str(tmp_path / "cc-config")
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            extra_env={"CLAUDE_CONFIG_DIR": iso, "CLAUDE_CODE_USE_BEDROCK": "1"},
        )
        with patch(
            "kiro_claw.acp.client._resolve_claude_acp_bin",
            return_value=["/usr/bin/node", "/x/acp.js"],
        ), patch(
            "kiro_claw.acp.client.wrap_argv",
            return_value=(["/usr/bin/node", "/x/acp.js"], None),
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["CLAUDE_CONFIG_DIR"] == iso
            # Bedrock flag must ride alongside (regression guard).
            assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"

    @pytest.mark.asyncio
    async def test_spawn_no_channel_id_env_absent(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id=None)
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert "KIROCLAW_CHANNEL_ID" not in env

    @pytest.mark.asyncio
    async def test_spawn_channel_id_only_no_session_key(self, tmp_path):
        clean_env = {k: v for k, v in os.environ.items() if k != "KIROCLAW_SESSION_KEY"}
        client = AcpClient(work_dir=tmp_path, session_key=None, channel_id="C0ABC123")
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ), patch.dict(
            os.environ, clean_env, clear=True
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCLAW_CHANNEL_ID"] == "C0ABC123"
            assert "KIROCLAW_SESSION_KEY" not in env

    @pytest.mark.asyncio
    async def test_spawn_no_session_key_env_none(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key=None)
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None, "env should be a dict (SSH_AUTH_SOCK resolution)"
            assert "KIROCLAW_SESSION_KEY" not in env


class TestAcpClientBackendSelection:
    """Verify the right backend binary is launched for kiro vs claude."""

    @pytest.fixture(autouse=True)
    def _reset_claude_cache(self):
        import kiro_claw.acp.client as _mod

        _mod._claude_acp_argv_cache = _mod._UNRESOLVED
        yield
        _mod._claude_acp_argv_cache = _mod._UNRESOLVED

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_uses_claude_acp_bin(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with patch(
            "kiro_claw.acp.client._resolve_claude_acp_bin",
            return_value=["/usr/local/bin/node", "/usr/local/lib/claude-agent-acp/index.js"],
        ), patch("kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"), patch(
            "kiro_claw.acp.client.wrap_argv",
            side_effect=lambda argv, mode: (argv, None),
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(mock_exec.call_args.args)
            assert argv == [
                "/usr/local/bin/node",
                "/usr/local/lib/claude-agent-acp/index.js",
            ], "claude backend must spawn node + script explicitly"

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_writes_settings_local(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with patch(
            "kiro_claw.acp.client._resolve_claude_acp_bin",
            return_value=["/usr/local/bin/claude-agent-acp"],
        ), patch(
            "kiro_claw.acp.client.wrap_argv",
            side_effect=lambda argv, mode: (argv, None),
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            settings = tmp_path / ".claude" / "settings.local.json"
            assert settings.exists()
            data = json.loads(settings.read_text())
            # KiroClaw routes every tool decision through session/request_permission
            # so the four-tier protocol (approve / trust_reads / trust / yolo)
            # applies uniformly to claude-agent-acp and kiro-cli.
            assert data["permissions"]["defaultMode"] == "default"

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_missing_bin_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with patch("kiro_claw.acp.client._resolve_claude_acp_bin", return_value=None), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ):
            with pytest.raises(AcpError, match="claude-agent-acp not found"):
                await client._spawn()

    @pytest.mark.asyncio
    async def test_spawn_kiro_backend_unchanged(self, tmp_path):
        """Default (non-claude) backend still spawns `kiro-cli acp --agent <name>`."""
        client = AcpClient(work_dir=tmp_path)
        with patch(
            "kiro_claw.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
        ), patch(
            "kiro_claw.acp.client.wrap_argv",
            side_effect=lambda argv, mode: (argv, None),
        ), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec, patch(
            "kiro_claw.session._track_pid"
        ), patch(
            "kiro_claw.session._track_session_pid"
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(mock_exec.call_args.args)
            assert argv[0] == "/usr/bin/kiro-cli"
            assert argv[1] == "acp"
            assert "--agent" in argv

    @pytest.mark.asyncio
    async def test_initialize_protocol_version_per_backend(self, tmp_path):
        """kiro expects a date string; claude-agent-acp expects an integer."""
        from kiro_claw.acp.client import (
            PROTOCOL_VERSION,
            PROTOCOL_VERSION_CLAUDE,
        )

        for backend, expected in (
            ("", PROTOCOL_VERSION),
            (ACP_BACKEND_CLAUDE, PROTOCOL_VERSION_CLAUDE),
        ):
            client = AcpClient(work_dir=tmp_path, acp_backend=backend)
            client._session_id = "sess-1"  # short-circuit past the new-session call
            sent_params: dict = {}

            async def fake_send_request(method, params, _sent=sent_params):
                if method == "initialize":
                    _sent.update(params)
                return 1

            async def fake_wait(_req_id, timeout=0):
                return {"protocolVersion": expected, "agentCapabilities": {}}

            client._send_request = fake_send_request  # type: ignore[assignment]
            client._wait_for_response = fake_wait  # type: ignore[assignment]
            client._drain_notifications = AsyncMock()  # type: ignore[assignment]

            # Stop after step 1 (initialize) — we only care about the first request.
            try:
                await client._initialize_session()
            except Exception:
                pass
            assert sent_params.get("protocolVersion") == expected, (
                f"backend={backend!r} expected protocolVersion={expected!r}, "
                f"got {sent_params.get('protocolVersion')!r}"
            )


class TestResolveClaudeAcpBin:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _resolve_claude_acp_bin

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(bin_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        result = _resolve_claude_acp_bin()
        assert result is not None
        assert str(bin_path) in result

    def test_path_lookup(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(bin_path) if name == "claude-agent-acp" else None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result is not None
        assert str(bin_path) in result

    def test_mise_which_preferred(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        script = tmp_path / "bin" / "claude-agent-acp"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: str(script))
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(script)]

    def test_mise_installed_script_resolves_node(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _resolve_claude_acp_bin

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "20.18.0"
        node_bin = mise_node / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = (
            mise_node
            / "lib"
            / "node_modules"
            / "@agentclientprotocol"
            / "claude-agent-acp"
            / "dist"
            / "index.js"
        )
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        result = _resolve_claude_acp_bin()
        assert result == [str(node_bin), str(script.resolve())]

    def test_non_executable_script_falls_back_to_path_node(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)

        script = tmp_path / "node_modules" / "claude-agent-acp" / "dist" / "index.js"
        script.parent.mkdir(parents=True)
        script.write_text("console.log('hi')\n")
        script.chmod(0o644)  # NOT executable

        node_bin = tmp_path / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(node_bin) if name == "node" else None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(node_bin), str(script.resolve())]

    def test_mise_glob_fallback(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "22.1.0"
        bin_dir = mise_node / "bin"
        bin_dir.mkdir(parents=True)
        acp_script = bin_dir / "claude-agent-acp"
        acp_script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        acp_script.chmod(0o755)
        node_bin = bin_dir / "node"
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(node_bin), str(acp_script.resolve())]

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result is None


class TestResolveClaudeCodeExecutable:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(exe))
        # mise/PATH must NOT be consulted when the override is a real file.
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/should/not/win")
        assert client_mod._resolve_claude_code_executable() == str(exe)

    def test_env_override_ignored_when_missing(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod

        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(tmp_path / "nope"))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None

    def test_mise_preferred_over_path(self, monkeypatch):
        from kiro_claw.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/mise/bin/claude")
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: "/usr/bin/claude")
        assert client_mod._resolve_claude_code_executable() == "/mise/bin/claude"

    def test_path_lookup(self, monkeypatch):
        from kiro_claw.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: "/home/u/.toolbox/bin/claude" if name == "claude" else None,
        )
        assert client_mod._resolve_claude_code_executable() == "/home/u/.toolbox/bin/claude"

    def test_none_when_absent(self, monkeypatch):
        from kiro_claw.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None


class TestMiseWhich:
    def test_returns_path_on_success(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _mise_which

        script = tmp_path / "claude-agent-acp"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=str(script) + "\n"))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") == str(script)

    def test_returns_none_when_mise_not_installed(self, monkeypatch):
        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _mise_which

        monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_nonzero_exit(self, tmp_path, monkeypatch):
        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_timeout(self, tmp_path, monkeypatch):
        import subprocess

        from kiro_claw.acp import client as client_mod
        from kiro_claw.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_sub = MagicMock()
        mock_sub.run = MagicMock(side_effect=subprocess.TimeoutExpired("mise", 5))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        monkeypatch.setattr(client_mod, "subprocess_mod", mock_sub)
        assert _mise_which("claude-agent-acp") is None


class TestAcpClientReadMessage:
    @pytest.mark.asyncio
    async def test_read_valid_json(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        line = json.dumps(msg_data) + "\n"

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=line.encode())
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert msg.result == {"ok": True}

    @pytest.mark.asyncio
    async def test_read_non_json_skipped(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"not json\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_empty_line(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_eof(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_buffer_overrun_raises_process_died(self, tmp_path):
        """A line exceeding the stdout buffer must surface as AcpProcessDied.

        asyncio's StreamReader.readline() raises ValueError when a single
        line exceeds its limit; the stream is corrupted afterward. The read
        loop must convert that into AcpProcessDied so session recovery
        respawns the process instead of the session freezing.
        """
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(
            side_effect=ValueError("Separator is not found, and chunk exceed the limit")
        )
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        with pytest.raises(AcpProcessDied):
            await client._read_message(timeout=1.0)


class TestAcpClientExtractChunk:
    def test_extract_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "hello"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("hello", False)

    def test_extract_thinking_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "thinking", "text": "let me think"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("let me think", True)

    def test_extract_non_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "tool_call", "title": "exec"}},
        )
        assert client._extract_text_chunk(msg) == (None, False)


class TestAcpClientTrackToolCall:
    def test_tracks_tool_call(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "execute_bash",
                    "kind": "tool_use",
                }
            },
        )
        client._track_tool_call(msg)
        assert ("tool_use", "execute_bash") in client.last_prompt_stats.tool_calls


class TestAcpClientTrackMetadata:
    def test_tracks_context_pct(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 42.5},
        )
        client._track_metadata(msg)
        assert client.last_prompt_stats.context_pct == 42.5


class TestAcpClientTrackUsageUpdate:
    """claude-agent-acp usage_update {used, size}: derives context_pct and
    records the raw token counts for the dashboard token text."""

    def _usage_msg(self, used, size):
        from kiro_claw.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def test_populates_pct_and_tokens(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(50000, 200000))
        stats = client.last_prompt_stats
        assert stats.context_pct == 25.0  # 50000 / 200000 * 100
        assert stats.context_used_tokens == 50000
        assert stats.context_window_tokens == 200000

    def test_missing_fields_leave_tokens_zero(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(None, None))
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0

    def test_tokens_carry_forward_across_prompt_reset(self, tmp_path):
        # The per-prompt reset preserves the last known pct + token counts so
        # the dashboard ring/text doesn't flicker to 0 at the start of a turn.
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(88000, 200000))
        prev_pct = client.last_prompt_stats.context_pct
        prev_used = client.last_prompt_stats.context_used_tokens
        prev_window = client.last_prompt_stats.context_window_tokens
        from kiro_claw.acp.types import AcpPromptStats

        # Mirror the reset sites (send_message_stream / _dispatch_events / etc.)
        client.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=prev_used,
            context_window_tokens=prev_window,
        )
        assert client.last_prompt_stats.context_used_tokens == 88000
        assert client.last_prompt_stats.context_window_tokens == 200000
        assert client.last_prompt_stats.context_pct == 44.0


class TestAcpClientNoProcess:
    @pytest.mark.asyncio
    async def test_send_request_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._send_request("test", {})

    @pytest.mark.asyncio
    async def test_read_message_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._read_message()


# ── Process tree cleanup tests ──


class TestGetChildPids:
    def test_nonexistent_pid(self):
        from kiro_claw.acp.client import _get_child_pids

        assert _get_child_pids(999999) == []

    def test_none_pid(self):
        from kiro_claw.acp.client import _get_child_pids

        assert _get_child_pids(None) == []

    def test_own_pid_returns_list(self):
        import os

        from kiro_claw.acp.client import _get_child_pids

        # May or may not have children, but should not raise
        result = _get_child_pids(os.getpid())
        assert isinstance(result, list)

    def test_recursive_children(self, monkeypatch):
        import kiro_claw.acp.client as client_mod
        from kiro_claw.acp.client import _get_child_pids

        # _direct_children tries /proc first, falls back to pgrep.
        # Mock _direct_children directly to avoid platform-specific /proc behavior.
        tree = {1000: [2000, 3000], 2000: [4000], 3000: [5000]}
        monkeypatch.setattr(client_mod, "_direct_children", lambda pid: tree.get(pid, []))
        # Depth-first: 2000 → 4000, then 3000 → 5000
        assert _get_child_pids(1000) == [2000, 4000, 3000, 5000]


class TestIsOurChild:
    def test_nonexistent_pid(self):
        from kiro_claw.acp.client import _is_our_child

        assert _is_our_child(999999) is False

    def test_own_pid_is_python(self):
        import os

        from kiro_claw.acp.client import _get_start_time, _is_our_child

        pid = os.getpid()
        start = _get_start_time(pid)
        # On build machines the exe may be 'brazilpython' which isn't in the
        # allowlist. Just verify start-time logic works when exe matches.
        result = _is_our_child(pid, expected_start=start)
        # Either True (python in allowlist) or False (wrapper exe) — both valid
        assert isinstance(result, bool)

    def test_known_exe_with_matching_start(self, monkeypatch):
        import sys

        import kiro_claw.acp.client as client_mod
        from kiro_claw.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        # Force macOS branch so subprocess_mod.check_output is used for exe
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"node",
        )
        assert _is_our_child(999, expected_start=42) is True

    def test_init_pid_not_ours(self):
        from kiro_claw.acp.client import _get_start_time, _is_our_child

        start = _get_start_time(1)
        assert _is_our_child(1, expected_start=start) is False

    def test_no_start_time_rejects(self):
        import os

        from kiro_claw.acp.client import _is_our_child

        # No expected_start → fail-closed
        assert _is_our_child(os.getpid()) is False

    def test_start_time_mismatch_rejects(self):
        import os

        from kiro_claw.acp.client import _is_our_child

        # Our own PID with a wrong start time → should reject (recycled)
        assert _is_our_child(os.getpid(), expected_start=-999) is False

    def test_deep_research_binary_recognized(self, monkeypatch):
        """deep-research MCP binary should be recognized as our child."""
        import sys

        import kiro_claw.acp.client as client_mod
        from kiro_claw.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"deep-research",
        )
        assert _is_our_child(999, expected_start=42) is True


class TestKillEscapedChildren:
    def test_empty_dict(self):
        from kiro_claw.acp.client import _kill_escaped_children

        _kill_escaped_children({})

    def test_dead_pids_skipped(self):
        from kiro_claw.acp.client import _kill_escaped_children

        _kill_escaped_children({999998: None, 999999: None})

    def test_reverse_order_and_allowlist(self, monkeypatch):
        import kiro_claw.acp.client as client_mod
        from kiro_claw.acp.client import _kill_escaped_children

        killed: list[int] = []

        def fake_kill(pid, sig):
            if sig == 0:
                return  # alive check
            killed.append(pid)

        def fake_is_our(pid, expected_start=None):
            return pid != 200  # 200 is "recycled"

        monkeypatch.setattr(client_mod.os, "kill", fake_kill)
        monkeypatch.setattr(client_mod, "_is_our_child", fake_is_our)

        _kill_escaped_children({100: None, 200: None, 300: None})
        # 200 skipped (not ours), killed in reverse: 300, 100
        assert killed == [300, 100]


class TestChildPidsField:
    def test_default_empty(self):
        client = AcpClient()
        assert client._child_pids == {}

    def test_cleared_on_reset(self):
        client = AcpClient()
        client._child_pids = {123: None, 456: None}
        client._reset_state()
        assert client._child_pids == {}


class TestProcessMessage:
    """Tests for _process_message action classification."""

    def _make_client(self):
        return AcpClient()

    def test_compaction_status_classified(self):
        client = self._make_client()
        from kiro_claw.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS, params={"status": {"type": "completed"}}
        )
        assert client._process_message(msg, req_id=99) == "compaction"

    def test_clear_status_classified(self):
        client = self._make_client()
        from kiro_claw.acp.types import METHOD_CLEAR_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        assert client._process_message(msg, req_id=99) == "clear"

    def test_agent_switched_classified(self):
        client = self._make_client()
        from kiro_claw.acp.types import METHOD_AGENT_SWITCHED, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        assert client._process_message(msg, req_id=99) == "agent_switched"

    def test_unknown_method_skipped(self):
        client = self._make_client()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="_kiro.dev/unknown/thing")
        assert client._process_message(msg, req_id=99) == "skip"

    def test_mcp_oauth_request_classified(self):
        client = self._make_client()
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "linear", "oauthUrl": "https://mcp.linear.app/authorize?..."},
        )
        assert client._process_message(msg, req_id=99) == "mcp_oauth_request"

    def test_permission_request_with_colliding_id_not_complete(self):
        # Regression: the agent's server→client request_permission id space is
        # independent of our prompt req_id space, so they collide on small
        # integers.  A permission request whose id == the in-flight prompt's
        # req_id must classify as "permission", NOT "complete" — otherwise the
        # turn ends early and the tool blocks forever (stuck Claude Code turn).
        client = self._make_client()
        from kiro_claw.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage

        msg = JsonRpcMessage(
            id=4,
            method=METHOD_REQUEST_PERMISSION,
            params={"toolCall": {"title": "ls"}, "options": []},
        )
        assert client._process_message(msg, req_id=4) == "permission"

    def test_real_response_with_matching_id_completes(self):
        # The genuine prompt response (id + result, no method) still completes.
        client = self._make_client()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=4, result={"stopReason": "end_turn"})
        assert client._process_message(msg, req_id=4) == "complete"


class TestStreamEventsExtension:
    """End-to-end tests for stream_events() yielding extension events."""

    @pytest.mark.asyncio
    async def test_agent_switched_event_fields(self):
        """stream_events extracts agentName from params and yields correct AcpEvent."""
        from kiro_claw.acp.types import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            METHOD_AGENT_SWITCHED,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        # Build the two messages the prompt loop would yield
        switch_msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "agent_switched", switch_msg
            yield "complete", complete_msg

        # Patch internals so stream_events doesn't need a real process
        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_AGENT_SWITCHED
        assert events[0].text == "planner"
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_compaction_event_fields(self):
        """stream_events extracts status.type and summary from compaction params."""
        from kiro_claw.acp.types import (
            EVENT_COMPACTION_STATUS,
            METHOD_COMPACTION_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "3k tokens saved"},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_COMPACTION_STATUS
        assert events[0].text == "completed"
        assert events[0].title == "3k tokens saved"

    @pytest.mark.asyncio
    async def test_clear_event_fields(self):
        """stream_events yields EVENT_CLEAR_STATUS with no extra fields."""
        from kiro_claw.acp.types import (
            EVENT_CLEAR_STATUS,
            METHOD_CLEAR_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        clear_msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "clear", clear_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_CLEAR_STATUS
        assert events[0].text == ""

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_event_fields(self):
        """stream_events extracts serverName + oauthUrl and yields EVENT_MCP_OAUTH_REQUEST."""
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc&state=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", oauth_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_MCP_OAUTH_REQUEST
        assert events[0].server_name == "linear"
        assert events[0].oauth_url == url
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_missing_url_skipped(self):
        """Notifications without oauthUrl are dropped (no event yielded)."""
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        bad_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "broken-server"},  # no oauthUrl
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", bad_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Only the complete event — the malformed oauth notification was dropped.
        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert not any(e.kind == EVENT_MCP_OAUTH_REQUEST for e in events)

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_synthesizes_complete(self):
        """When kiro-cli cancels tools, stream_events completes instead of hanging.

        Regression: kiro-cli's built-in security filter cancels tool uses and emits a
        text chunk "Tool uses were interrupted, waiting for the next user prompt", but
        never sends a session/prompt response. Detect the marker and synthesize a
        complete event so the caller exits cleanly.
        """
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Text chunk yielded to caller, then synthesized EVENT_COMPLETE.
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert "Tool uses were interrupted" in events[0].text
        client._emit_tool_interrupted_sel.assert_called_once_with("_dispatch_events")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_stream_returns(self):
        """send_message_stream returns cleanly (no hang) when kiro-cli cancels tools."""
        from kiro_claw.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — without the fix, the generator would never exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        chunks: list[str] = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert chunks == ["Tool uses were interrupted, waiting for the next user prompt"]
        client._emit_tool_interrupted_sel.assert_called_once_with("send_message_stream")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_returns(self):
        """send_message returns accumulated text instead of raising AcpTimeoutError."""
        from kiro_claw.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — would otherwise raise AcpTimeoutError on loop exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        result = await client.send_message("test", timeout=5.0)
        assert "Tool uses were interrupted" in result
        client._emit_tool_interrupted_sel.assert_called_once_with("_read_prompt_response")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_requires_exact_match(self):
        """Substring-but-not-exact match must NOT trigger early completion.

        Protects against false positives when the model quotes the marker text.
        """
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        quoted_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": (
                            "The message 'Tool uses were interrupted, waiting for the next "
                            "user prompt' means kiro-cli blocked the tool."
                        ),
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", quoted_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Normal flow: text chunk + real complete event (not synthesized early).
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        # Precondition + guard: marker IS a substring, but NOT an exact match.
        marker = "Tool uses were interrupted, waiting for the next user prompt"
        assert marker in events[0].text
        assert events[0].text.strip() != marker
        # Exact-match guard held: SEL must NOT be emitted for a quoted marker.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_ignored_in_thinking_chunk(self):
        """Marker arriving as a thinking chunk must NOT trigger early completion.

        The `_dispatch_events` path guards marker detection with `not is_thinking`.
        If a reasoning/thinking chunk happens to contain the exact marker text,
        it must not be treated as kiro-cli's interrupt signal — only top-level
        agent_message_chunk with non-thinking content type is the real signal.
        """
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_THINKING_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        thinking_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "thinking",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", thinking_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Thinking chunk yielded as EVENT_THINKING_CHUNK, real complete follows.
        assert [e.kind for e in events] == [EVENT_THINKING_CHUNK, EVENT_COMPLETE]
        # Guard held: SEL must NOT be emitted for a thinking-type chunk.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_sel_contract(self):
        """SEL audit fields are pinned: tool_name, outcome, kind, site.

        The other marker tests mock _emit_tool_interrupted_sel itself, which only
        asserts it's invoked — not that the audit event carries the right fields.
        This test patches kiro_claw.sel.sel so the real helper runs, protecting
        against silent regressions in the security-audit contract.
        """
        from kiro_claw.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-xyz")
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        with patch("kiro_claw.sel.sel") as mock_sel:
            async for _ in client.stream_events("test"):
                pass

        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "kiro_cli_security_filter"
        assert kwargs["tool_kind"] == "client_built_in"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "acp"
        assert kwargs["session_key"] == "sess-xyz"
        assert kwargs["metadata"]["site"] == "_dispatch_events"
        assert kwargs["metadata"]["reason"] == "tool_interrupted_marker"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry_point, expected_site",
        [
            ("stream_events", "_dispatch_events"),
            ("send_message_stream", "send_message_stream"),
            ("send_message", "_read_prompt_response"),
        ],
    )
    async def test_tool_interrupted_all_paths_log_and_return_promptly(
        self, entry_point, expected_site, caplog
    ):
        """Across all three entry points: returns in bounded time, logs WARNING
        with the correct site tag, records one SEL audit.

        Pins three properties the other tests don't cover:
        1. Timing — without the fix the call hangs until the 2h prompt timeout.
           An explicit <2s bound documents the "no hang" contract.
        2. Single WARNING per call, tagged with the originating call site.
           On-call greps ``kiro-cli cancelled tool use`` — a silent regression at
           any site would mask real filter firings in production.
        3. SEL audit fires with metadata.site matching the call site, across all
           three sites (not just _dispatch_events).
        """
        import logging
        import time

        from kiro_claw.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-abc")
        client._session_id = "session-pin-1234"

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, " "waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        caplog.set_level(logging.WARNING, logger="kiro_claw.acp.client")

        t0 = time.monotonic()
        with patch("kiro_claw.sel.sel") as mock_sel:
            if entry_point == "stream_events":
                async for _ in client.stream_events("test"):
                    pass
            elif entry_point == "send_message_stream":
                async for _ in client.send_message_stream("test"):
                    pass
            elif entry_point == "send_message":
                await client.send_message("test")
        elapsed = time.monotonic() - t0

        # (1) Timing — no hang.  Generous bound; in practice this is <50ms.
        assert elapsed < 2.0, f"entry_point={entry_point} took {elapsed:.3f}s"

        # (2) Exactly one WARNING, tagged with the originating call site.
        warns = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "kiro-cli cancelled tool use" in r.getMessage()
        ]
        assert len(warns) == 1, (
            f"entry_point={entry_point} expected 1 WARNING, got {len(warns)}: "
            f"{[r.getMessage() for r in warns]}"
        )
        assert f"site={expected_site}" in warns[0].getMessage()

        # (3) SEL audit — one call, metadata.site matches the call site.
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["site"] == expected_site


class TestDrainStderrRedaction:
    """Test 3.1: _drain_stderr logs redacted text but stores raw."""

    @pytest.mark.asyncio
    async def test_raw_stored_redacted_logged(self):
        client = AcpClient()
        raw = "Error: key=AKIAIOSFODNN7EXAMPLE connect failed"

        reader = AsyncMock(spec=["readline"])
        reader.readline = AsyncMock(side_effect=[raw.encode() + b"\n", b""])

        with patch("kiro_claw.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # Raw stored
        assert list(client._stderr_lines) == [raw]
        # Logged call used redacted text (credential replaced)
        logged_text = mock_logger.warning.call_args[0][2]
        assert "AKIAIOSFODNN7EXAMPLE" not in logged_text


class TestReadMessageStderrRedaction:
    """Test 3.2: _read_message redacts stderr in AcpError."""

    @pytest.mark.asyncio
    async def test_acperror_contains_redacted_stderr(self):
        client = AcpClient()
        client._cancelled = False
        client._buffer = MagicMock()
        client._buffer.__bool__ = lambda s: False
        client._process = MagicMock()
        client._process.returncode = 1
        client._process.stdout = MagicMock()
        client._process.stdout.readline = AsyncMock(return_value=b"")
        client._stderr_lines = deque(["secret key=AKIAIOSFODNN7EXAMPLE"])
        client._stderr_task = MagicMock()
        client._stderr_task.done.return_value = True

        with pytest.raises(AcpError, match="ACP process exited") as exc_info:
            await client._read_message(timeout=1.0)

        assert "AKIAIOSFODNN7EXAMPLE" not in str(exc_info.value)


class TestEnsureReadyRetryOnAcpError:
    """Test 2: ensure_ready retries once on AcpError."""

    @pytest.mark.asyncio
    async def test_retries_on_acp_error(self):
        client = AcpClient()
        client._process = None
        client._session_id = None

        call_count = 0

        async def fake_spawn():
            nonlocal call_count
            call_count += 1
            client._process = MagicMock()
            client._process.returncode = None
            client._process.pid = 100 + call_count
            client._process.stderr = None

        async def fake_init():
            if call_count == 1:
                raise AcpError("MCP server crashed")
            client._session_id = "sess-ok"

        def fake_reset():
            client._process = None
            client._session_id = None
            client._pid = None

        client._spawn = fake_spawn
        client._initialize_session = fake_init
        client._kill_process = AsyncMock()
        client._reset_state = fake_reset
        client._snapshot_process_tree = AsyncMock()

        await client.ensure_ready()

        assert call_count == 2
        assert client._session_id == "sess-ok"
        client._kill_process.assert_called_once_with(force=True)


class TestEnsureReadyRecreatesWorkDir:
    @pytest.mark.asyncio
    async def test_recreates_missing_work_dir(self, tmp_path):
        work_dir = tmp_path / "ws"
        client = AcpClient(work_dir=work_dir)
        client._process = MagicMock()
        client._process.returncode = None
        client._session_id = "sess-1"
        client._spawn = AsyncMock()

        assert not work_dir.exists()
        await client.ensure_ready()
        assert work_dir.is_dir()
        client._spawn.assert_not_called()


class TestMakeUnifiedDiff:
    """Tests for _make_unified_diff helper."""

    def test_both_empty_returns_empty(self):
        assert _make_unified_diff("", "", "file.py") == ""

    def test_addition(self):
        result = _make_unified_diff("", "new line\n", "file.py")
        assert "+new line" in result
        assert "--- file.py" in result

    def test_deletion(self):
        result = _make_unified_diff("old line\n", "", "file.py")
        assert "-old line" in result

    def test_modification(self):
        result = _make_unified_diff("old\n", "new\n", "file.py")
        assert "-old" in result
        assert "+new" in result
        assert "@@" in result

    def test_identical_returns_empty(self):
        assert _make_unified_diff("same\n", "same\n", "file.py") == ""

    def test_truncation(self):
        result = _make_unified_diff("", "x\n" * 5000, "file.py", max_len=100)
        assert len(result) <= 100

    def test_no_trailing_newline(self):
        result = _make_unified_diff("old", "new", "file.py")
        assert "-old" in result
        assert "+new" in result


# ── Phase 1: stop_reason and turn_done tests ──


class TestStopReasonPopulated:
    """Tests for stop_reason extraction from prompt response."""

    @pytest.mark.asyncio
    async def test_stop_reason_populated_on_complete(self):
        """Prompt response with stopReason='cancelled' populates event.stop_reason."""
        from kiro_claw.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "cancelled"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_reason_populated_end_turn(self):
        """Prompt response with stopReason='end_turn' populates event.stop_reason."""
        from kiro_claw.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_stop_reason_empty_when_absent(self):
        """Prompt response without stopReason key yields empty stop_reason."""
        from kiro_claw.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"status": "ok"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == ""


class TestWaitTurnDone:
    """Tests for wait_turn_done and has_active_turn."""

    @pytest.mark.asyncio
    async def test_wait_turn_done_returns_reason(self):
        """wait_turn_done returns the stop_reason after turn completes."""
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "cancelled"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        # Consume stream_events to trigger turn_done
        async for _ in client.stream_events("test"):
            pass

        reason = await client.wait_turn_done(timeout=1.0)
        assert reason == "cancelled"

    @pytest.mark.asyncio
    async def test_wait_turn_done_times_out(self):
        """wait_turn_done raises TimeoutError when no complete fires."""
        client = AcpClient()
        client._turn_done.clear()

        with pytest.raises(asyncio.TimeoutError):
            await client.wait_turn_done(timeout=0.05)


class TestHasActiveTurn:
    """Tests for has_active_turn() across its three conditions."""

    def test_has_active_turn_states(self):
        client = AcpClient()
        # Set up happy state: not cancelled, turn not done, process alive
        client._cancelled = False
        client._turn_done.clear()
        client._process = MagicMock()
        client._process.returncode = None
        assert client.has_active_turn() is True

        # Condition 1: process dies
        client._process.returncode = 1
        assert client.has_active_turn() is False
        client._process.returncode = None  # reset

        # Condition 2: cancelled flag set
        client._cancelled = True
        assert client.has_active_turn() is False
        client._cancelled = False  # reset

        # Condition 3: turn_done is set
        client._turn_done.set()
        assert client.has_active_turn() is False
        client._turn_done.clear()  # reset

        # Confirm happy state restored
        assert client.has_active_turn() is True


class TestCancelledGraceWindow:
    """Tests for the _cancelled grace window in _read_message."""

    @pytest.mark.asyncio
    async def test_cancelled_flag_does_not_short_circuit_reads(self):
        """_read_message reads a queued message within the grace window."""
        client = AcpClient()
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=json.dumps(msg_data).encode() + b"\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with recent timestamp (within grace window)
        client._cancelled = True
        client._cancel_ts = time.monotonic()

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancelled_flag_enforces_grace_window(self):
        """_read_message raises AcpError when grace window is exceeded."""
        client = AcpClient()

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with timestamp > 10s ago
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 11.0

        with pytest.raises(AcpError, match="grace window exceeded"):
            await client._read_message(timeout=1.0)


class TestSendPipeErrors:
    """Verify that broken pipe errors on stdin are raised as AcpProcessDied."""

    def _make_client_with_mock_process(self):
        client = AcpClient()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc
        client._next_req_id = MagicMock(return_value=1)
        return client, proc

    @pytest.mark.asyncio
    async def test_send_request_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_request_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_response_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_response_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_request_success_unaffected(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.return_value = None

        req_id = await client._send_request("test/method", {"key": "val"})
        assert req_id == 1
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_request_write_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.write.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    # ── Staleness timeout tests ──────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_stale_turn_synthesizes_complete_after_text(self):
        """When text is streamed but no complete arrives, synthesize EVENT_COMPLETE."""
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Hello world"},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            # No "complete" — simulates kiro-cli going silent after text.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert events[1].stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_tool_call(self):
        """Tool call after text clears _stale_eligible — no synthetic complete."""
        from kiro_claw.acp.client import AcpTimeoutError
        from kiro_claw.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Let me check..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolUseId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", tool_msg
            # No "complete" — tool is running, loop ends (simulates timeout).

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by tool_call
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_permission(self):
        """Permission request after text clears _stale_eligible."""
        from kiro_claw.acp.client import AcpTimeoutError
        from kiro_claw.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "I need to run..."},
                }
            },
        )
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolName": "shell",
                "toolInput": "rm -rf /tmp/test",
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "permission", perm_msg
            # No "complete" — waiting for user approval, loop ends.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by permission
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_re_enabled_after_tool_then_text(self):
        """Text after tool re-enables _stale_eligible — synthetic complete fires."""
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text1 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Checking..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolUseId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )
        text2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Done."},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text1
            yield "update", tool_msg
            yield "update", text2
            # No "complete" — text after tool, stale eligible again.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TEXT_CHUNK in kinds
        assert EVENT_TOOL_CALL in kinds
        assert kinds[-1] == EVENT_COMPLETE
        assert events[-1].stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_passive_update_does_not_clear_stale_eligible(self):
        """Passive updates (usage_update, available_commands) after text must NOT
        reset _stale_eligible — stale detection should still fire.

        Regression: kiro-cli sends a non-text update after the final text chunk
        but never sends complete. The blanket _stale_eligible=False on every event
        disabled the 90s timeout, causing the session to hang until the 2h deadline.
        """
        from kiro_claw.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_USAGE,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "BUILD SUCCEEDED"},
                }
            },
        )
        # Passive update after final text — must NOT reset stale
        usage_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_USAGE,
                    "used": 50000,
                    "size": 200000,
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", usage_msg
            # No "complete" — simulates kiro-cli going silent.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Stale detection should still fire despite the passive update
        kinds = [e.kind for e in events]
        assert kinds == [
            EVENT_TEXT_CHUNK,
            EVENT_COMPLETE,
        ], f"Expected stale detection to synthesize complete after passive update, got {kinds}"
        assert events[-1].stop_reason == STOP_REASON_END_TURN


# ── Coverage push: process lifecycle ──


class TestKillProcess:
    """Tests for _kill_process covering SIGTERM, SIGKILL, and edge cases."""

    @pytest.mark.asyncio
    async def test_noop_when_no_process(self):
        client = AcpClient()
        client._process = None
        await client._kill_process()  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_already_exited(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = 0
        client._process = proc
        await client._kill_process()  # should not raise

    @pytest.mark.asyncio
    async def test_sigterm_success(self):
        """Normal path: SIGTERM → process exits within timeout."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 12345
        client._child_pids = {}

        with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=12345), patch(
            "kiro_claw.acp.client._get_child_pids", return_value=[]
        ), patch("kiro_claw.acp.client._kill_escaped_children") as mock_esc:
            await client._kill_process()
            mock_killpg.assert_called_once_with(12345, signal.SIGTERM)
            mock_esc.assert_called_once()

    @pytest.mark.asyncio
    async def test_sigterm_timeout_then_sigkill(self):
        """SIGTERM times out → falls through to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        # First wait times out, second succeeds
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        client._process = proc
        client._pid = 99
        client._child_pids = {}

        killpg_calls = []

        def fake_killpg(pgid, sig):
            killpg_calls.append(sig)

        with patch("os.killpg", side_effect=fake_killpg), patch(
            "os.getpgid", return_value=99
        ), patch("kiro_claw.acp.client._get_child_pids", return_value=[]), patch(
            "kiro_claw.acp.client._kill_escaped_children"
        ):
            await client._kill_process()

        assert signal.SIGTERM in killpg_calls
        assert signal.SIGKILL in killpg_calls

    @pytest.mark.asyncio
    async def test_force_skips_sigterm(self):
        """force=True goes straight to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 55
        client._child_pids = {}

        killpg_sigs = []

        def fake_killpg(pgid, sig):
            killpg_sigs.append(sig)

        with patch("os.killpg", side_effect=fake_killpg), patch(
            "os.getpgid", return_value=55
        ), patch("kiro_claw.acp.client._get_child_pids", return_value=[]), patch(
            "kiro_claw.acp.client._kill_escaped_children"
        ):
            await client._kill_process(force=True)

        assert signal.SIGTERM not in killpg_sigs
        assert signal.SIGKILL in killpg_sigs

    @pytest.mark.asyncio
    async def test_killpg_process_lookup_error_falls_to_proc_kill(self):
        """When killpg raises ProcessLookupError, falls back to proc.kill()."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 77
        client._child_pids = {}

        with patch("os.killpg", side_effect=ProcessLookupError()), patch(
            "os.getpgid", return_value=77
        ), patch("kiro_claw.acp.client._get_child_pids", return_value=[]), patch(
            "kiro_claw.acp.client._kill_escaped_children"
        ):
            await client._kill_process(force=True)

        proc.kill.assert_called_once()


class TestResetStateExtended:
    """Extended _reset_state tests covering sandbox cleanup and PID untracking."""

    def test_sandbox_cleanup_removes_file(self, tmp_path):
        client = AcpClient()
        sb_file = tmp_path / "sandbox.sb"
        sb_file.write_text("sandbox profile")
        client._sandbox_cleanup = str(sb_file)
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()

        assert not sb_file.exists()
        assert client._sandbox_cleanup is None

    def test_sandbox_cleanup_missing_file_no_error(self):
        client = AcpClient()
        client._sandbox_cleanup = "/nonexistent/path.sb"
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()  # should not raise
        assert client._sandbox_cleanup is None

    def test_untracks_pids(self):
        client = AcpClient()
        client._process = None
        client._pid = 1234
        client._child_pids = {5678: None, 9012: None}

        with patch("kiro_claw.session._untrack_child_pids") as mock_uc, patch(
            "kiro_claw.session._untrack_pid"
        ) as mock_up, patch("kiro_claw.session._untrack_session_pid") as mock_usp:
            client._reset_state()

        mock_uc.assert_called_once_with({5678: None, 9012: None})
        mock_up.assert_called_once_with(1234)
        mock_usp.assert_called_once_with(1234)
        assert client._child_pids == {}
        assert client._pid is None

    def test_cancels_stderr_task(self):
        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {}
        mock_task = MagicMock()
        mock_task.done.return_value = False
        client._stderr_task = mock_task

        client._reset_state()

        mock_task.cancel.assert_called_once()
        assert client._stderr_task is None


# ── Coverage push: session lifecycle ──


class TestInitializeSession:
    """Tests for _initialize_session covering new session, resume, and model set."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so session-file writes are isolated.

        Without this, tests pollute the real ~/.kiro/sessions/cli/ on the
        build host and collide with parallel runs (hardcoded session names).
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _make_client(self, tmp_path, **kwargs):
        client = AcpClient(work_dir=tmp_path, **kwargs)
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._next_req_id = MagicMock(side_effect=range(1, 100))
        return client

    @pytest.mark.asyncio
    async def test_new_session_basic(self, tmp_path):
        """Happy path: initialize → session/new → set_mode → drain."""
        client = self._make_client(tmp_path)
        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {}},
            2: {"sessionId": "sess-abc"},
        }

        async def fake_wait(req_id, timeout=50.0):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert client._session_id == "sess-abc"
        assert client._resumed is False
        client._drain_notifications.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_resume_success(self, tmp_path):
        """session/load succeeds when file exists and kiro-cli supports it."""
        client = self._make_client(tmp_path)
        client._resume_session_id = "old-sess"

        # Create the session file
        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "old-sess.json"
        session_file.write_text("{}")

        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
            2: {"modes": ["chat"]},  # load success
        }

        async def fake_wait(req_id, timeout=50.0):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "old-sess"
            assert client._resumed is True
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_session_resume_fallback_to_new(self, tmp_path):
        """session/load fails → falls back to session/new."""
        from kiro_claw.acp.client import AcpError

        client = self._make_client(tmp_path)
        client._resume_session_id = "bad-sess"

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "bad-sess.json"
        session_file.write_text("{}")

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            if call_idx[0] == 2:
                raise AcpError("session not found")
            if call_idx[0] == 3:
                return {"sessionId": "new-sess"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "new-sess"
            assert client._resumed is False
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_cc_resume_skips_load_when_transcript_missing(self, tmp_path):
        """claude backend: a stale persisted sid with NO transcript on disk
        must fall back to session/new (a fresh start), not replay via
        session/load. Guards against the ~38%-on-'hi' base-context bloat."""
        from kiro_claw.acp.types import ACP_BACKEND_CLAUDE

        client = self._make_client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "ghost-sess"  # no transcript exists for it

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            # session/load must NOT be called; the next request is session/new.
            return {"sessionId": "fresh-sess"}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()
        assert client._session_id == "fresh-sess"
        assert client._resumed is False

    @pytest.mark.asyncio
    async def test_cc_resume_loads_when_transcript_exists(self, tmp_path, monkeypatch):
        """claude backend: when the CC transcript .jsonl exists, session/load
        is attempted and resume succeeds."""
        from kiro_claw.acp.types import ACP_BACKEND_CLAUDE
        from kiro_claw.providers.cleanup import _cc_session_paths

        # Pin the isolated CC config root into tmp_path so planting the transcript
        # and the resume guard's .exists() resolve to the SAME hermetic dir (and
        # we don't pollute the real ~/.kiroclaw/cc-config on the build host).
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc-config"))

        client = self._make_client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "real-sess"
        # Plant the CC transcript at the path the resume guard checks.
        transcript = _cc_session_paths(tmp_path, "real-sess")[0]
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}")

        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
            2: {"modes": ["chat"]},  # load success
        }

        async def fake_wait(req_id, timeout=50.0):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()
        assert client._session_id == "real-sess"
        assert client._resumed is True

    @pytest.mark.asyncio
    async def test_set_model_when_non_default(self, tmp_path):
        """Non-default model triggers set_model request."""
        client = self._make_client(tmp_path)
        client._model = "claude-sonnet"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        # Track all send_request calls
        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" in send_calls

    @pytest.mark.asyncio
    async def test_no_set_model_when_auto(self, tmp_path):
        """Default model 'auto' skips set_model request."""
        client = self._make_client(tmp_path)
        client._model = "auto"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" not in send_calls


# ── Coverage push: JSON-RPC plumbing ──


class TestWaitForResponse:
    """Tests for _wait_for_response covering matching, buffering, and errors."""

    @pytest.mark.asyncio
    async def test_matching_response_returned(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, result={"data": "ok"})
        client._read_message = AsyncMock(return_value=msg)

        result = await client._wait_for_response(5, timeout=5.0)
        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_error_response_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, error={"code": -1, "message": "fail"})
        client._read_message = AsyncMock(return_value=msg)

        with pytest.raises(AcpError, match="JSON-RPC error"):
            await client._wait_for_response(5, timeout=5.0)

    @pytest.mark.asyncio
    async def test_notification_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        notif = JsonRpcMessage(method="mcp/serverReady", params={"name": "builder"})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[notif, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}
        assert len(client._mcp_notifications) == 1

    @pytest.mark.asyncio
    async def test_non_matching_response_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        other = JsonRpcMessage(id=99, result={"other": True})
        target = JsonRpcMessage(id=3, result={"target": True})
        client._read_message = AsyncMock(side_effect=[other, target])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"target": True}
        assert len(client._buffer) == 1

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path):
        from kiro_claw.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpTimeoutError):
            await client._wait_for_response(1, timeout=0.1)

    @pytest.mark.asyncio
    async def test_shutdown_event_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with patch("kiro_claw.shutdown_event") as mock_ev:
            mock_ev.is_set.return_value = True
            with pytest.raises(AcpError, match="Shutdown"):
                await client._wait_for_response(1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_server_request_warned_and_dropped(self, tmp_path):
        """Unexpected server request (method + id) is warned and dropped."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        server_req = JsonRpcMessage(id=50, method="unexpected/request", params={})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[server_req, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}


class TestDrainNotifications:
    """Tests for _drain_notifications covering buffered and live messages."""

    @pytest.mark.asyncio
    async def test_drains_buffered_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "slack-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert len(client._mcp_notifications) == 0

    @pytest.mark.asyncio
    async def test_drains_live_messages(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        live_msg = JsonRpcMessage(method="mcp/serverReady", params={"name": "core"})
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return live_msg
            return None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.2)
        # Should have processed the live message without error

    @pytest.mark.asyncio
    async def test_handles_acp_error_during_drain(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []
        client._read_message = AsyncMock(side_effect=AcpError("process died"))

        await client._drain_notifications(duration=0.1)  # should not raise

    @pytest.mark.asyncio
    async def test_captures_buffered_mcp_oauth_request(self, tmp_path):
        """OAuth notifications buffered during init are captured into pending list."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "linear"
        assert pending[0]["oauthUrl"] == url
        # Drained — second pop returns empty.
        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_captures_live_mcp_oauth_request(self, tmp_path):
        """OAuth notifications arriving live during drain are also captured."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://auth.smithery.ai/neon/authorize?client_id=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "neon", "oauthUrl": url},
        )
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return oauth_msg
            return None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.5)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "neon"
        assert pending[0]["oauthUrl"] == url

    @pytest.mark.asyncio
    async def test_oauth_request_without_url_not_captured(self, tmp_path):
        """Malformed oauth notifications (no oauthUrl) are ignored."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "broken"},  # no oauthUrl key
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_oauth_request_dedupes_per_server(self, tmp_path):
        """kiro-cli may emit oauth_request multiple times per server probe — dedupe."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1, "duplicate oauth_request for same server should be collapsed"

    @pytest.mark.asyncio
    async def test_unsafe_url_does_not_consume_dedupe_slot(self, tmp_path):
        """Unsafe-scheme URL must be rejected *before* the dedupe key is recorded,
        so a later safe retry for the same server still surfaces."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": "javascript:alert(1)"},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize",
                },
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["oauthUrl"] == "https://mcp.linear.app/authorize"

    @pytest.mark.asyncio
    async def test_oauth_request_with_empty_server_name_dropped(self, tmp_path):
        """server_initialized/server_init_failure discard by server_name only —
        recording a banner with empty server_name would create a permanently-
        stuck dedupe entry.  Drop instead."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "", "oauthUrl": "https://example.com/auth"},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []
        assert client._oauth_emitted_servers == set()


# ── Coverage push: prompt loop ──


class TestPromptLoop:
    """Tests for _prompt_loop covering normal flow, process death, and staleness."""

    @pytest.mark.asyncio
    async def test_yields_actions(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msgs = [
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}
                },
            ),
            JsonRpcMessage(id=1, result={"status": "ok"}),
        ]
        idx = [0]

        async def fake_read(timeout=20.0):
            if idx[0] < len(msgs):
                m = msgs[idx[0]]
                idx[0] += 1
                return m
            return None

        client._read_message = fake_read

        actions = []
        async for action, msg in client._prompt_loop(req_id=1, timeout=5.0):
            actions.append(action)
            if action == "complete":
                break

        assert "update" in actions
        assert "complete" in actions

    @pytest.mark.asyncio
    async def test_process_death_raises(self, tmp_path):
        from kiro_claw.acp.client import AcpProcessDied

        client = AcpClient(work_dir=tmp_path)
        proc = MagicMock()
        proc.returncode = 1
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

    @pytest.mark.asyncio
    async def test_stale_turn_exits_early(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._stale_eligible = True
        proc = MagicMock()
        proc.returncode = None
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        # With stale_eligible=True and no data, should exit after _STALE_TURN_TIMEOUT
        # We patch the timeout to be very short
        with patch("kiro_claw.acp.client._STALE_TURN_TIMEOUT", 0.05):
            actions = []
            async for action, msg in client._prompt_loop(req_id=1, timeout=2.0):
                actions.append(action)

        # Should exit cleanly (return, not raise)
        assert actions == []


class TestDispatchEventsExtended:
    """Extended tests for _dispatch_events covering permission, tool events, compaction."""

    @pytest.mark.asyncio
    async def test_permission_event_yielded(self):
        from kiro_claw.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc1"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_PERMISSION_REQUEST in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_tool_event_yielded(self):
        from kiro_claw.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, JsonRpcMessage

        client = AcpClient()
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc1",
                    "input": {"path": "/tmp/x"},
                }
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", tool_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TOOL_CALL in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_extract_agent_from_result(self):
        """extract_agent_from_result=True yields agent_switched from result data."""
        from kiro_claw.acp.types import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(
            id=1, result={"data": {"agent": {"name": "planner"}}, "message": ""}
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(
            req_id=1, timeout=5.0, extract_agent_from_result=True
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_AGENT_SWITCHED in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_error_action_raises(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "boom"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        with pytest.raises(AcpError, match="boom"):
            async for _ in client._dispatch_events(req_id=1, timeout=5.0):
                pass


# ── Coverage push: command wrappers ──


class TestSendCommand:
    """Tests for send_command."""

    @pytest.mark.asyncio
    async def test_send_command_returns_text(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(return_value={"text": "usage: 42%"})

        result = await client.send_command("/usage")
        assert "42%" in result

    @pytest.mark.asyncio
    async def test_send_command_timeout_returns_empty(self, tmp_path):
        from kiro_claw.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(side_effect=AcpTimeoutError())

        result = await client.send_command("/compact")
        assert result == ""

    @pytest.mark.asyncio
    async def test_send_command_redacts_urls(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(
            return_value={"text": "visit https://evil.com/exfil?data=secret"}
        )

        # send_command MUST run raw text through the redactor before returning.
        # The redactor itself is unit-tested separately in test_security.py;
        # here we verify the call site wires it up at all (a vacuous isinstance
        # check would not catch a missed redact step).
        with patch(
            "kiro_claw.security.redact_exfiltration_urls",
            return_value=("[redacted]", ["url"]),
        ) as mock_redact:
            result = await client.send_command("/test")

        mock_redact.assert_called_once_with("visit https://evil.com/exfil?data=secret")
        assert result == "[redacted]"


class TestCancelSession:
    """Tests for cancel_session."""

    @pytest.mark.asyncio
    async def test_cancel_sends_notification(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "sess-1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()

        assert client._cancelled is True
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        data = json.loads(written.decode())
        assert data["method"] == "session/cancel"
        assert data["params"]["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_cancel_no_session_id_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = None
        await client.cancel_session()  # should not raise

    @pytest.mark.asyncio
    async def test_cancel_no_process_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = None
        await client.cancel_session()
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_write_exception_handled(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock(side_effect=BrokenPipeError())
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()  # should not raise
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_raises_grace_window_to_budget(self, tmp_path):
        """A budget above the 10s floor must extend the read-grace window so
        the read loop does not abort the turn early and force a hard kill."""
        from kiro_claw.acp.client import _CANCEL_GRACE_SECS

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session(grace_secs=30.0)
        assert client._cancel_grace_secs == 30.0

        # A budget below the floor never shrinks the window.
        client._cancel_grace_secs = _CANCEL_GRACE_SECS
        await client.cancel_session(grace_secs=2.0)
        assert client._cancel_grace_secs == _CANCEL_GRACE_SECS


class TestWaitForCompaction:
    """Tests for wait_for_compaction."""

    @pytest.mark.asyncio
    async def test_returns_completed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "saved 3k"},
        )
        client._read_message = AsyncMock(side_effect=[None, msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "completed", "summary": "saved 3k"}

    @pytest.mark.asyncio
    async def test_returns_failed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "failed"}, "summary": "error"},
        )
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "failed", "summary": "error"}

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_dict(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        result = await client.wait_for_compaction(timeout=0.1)
        assert result == {"type": "timeout"}

    @pytest.mark.asyncio
    async def test_buffers_non_compaction_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import METHOD_COMPACTION_STATUS, METHOD_METADATA, JsonRpcMessage

        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 55.0})
        other_notif = JsonRpcMessage(method="mcp/something", params={"x": 1})
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "ok"},
        )
        client._read_message = AsyncMock(side_effect=[meta_msg, other_notif, compact_msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result["type"] == "completed"
        assert client.last_prompt_stats.context_pct == 55.0
        assert len(client._mcp_notifications) == 1


# ── Coverage push: tool tracking ──


class TestExtractToolEvent:
    """Tests for _extract_tool_event covering various tool_call shapes."""

    def test_basic_tool_call(self):
        client = AcpClient()
        from kiro_claw.acp.types import EVENT_TOOL_CALL, JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc-1",
                    "input": {"path": "/tmp/file.txt"},
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.kind == EVENT_TOOL_CALL
        assert event.title == "Read"

    def test_tool_call_with_diff_content(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-2",
                    "input": {},
                    "content": [
                        {"type": "diff", "oldText": "old\n", "newText": "new\n", "path": "f.py"}
                    ],
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-old" in event.tool_input or "+new" in event.tool_input

    def test_tool_call_str_replace_fallback(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-3",
                    "input": {
                        "command": "strReplace",
                        "oldStr": "a",
                        "newStr": "b",
                        "path": "x.py",
                    },
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-a" in event.tool_input or "+b" in event.tool_input

    def test_non_tool_call_returns_none(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}},
        )
        event = client._extract_tool_event(msg)
        assert event is None

    def test_tool_purpose_extracted(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "shell",
                    "kind": "tool_use",
                    "toolCallId": "tc-4",
                    "input": {"__tool_use_purpose": "run tests", "command": "pytest"},
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.tool_purpose == "run tests"


class TestBuildPermissionEvent:
    """Tests for _build_permission_event."""

    def test_basic_permission(self):
        client = AcpClient()
        from kiro_claw.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=42,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-5"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        event = client._build_permission_event(msg)
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.request_id == 42
        assert event.title == "shell"
        assert len(event.options) == 2

    def test_default_options_when_empty(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=10,
            method="session/requestPermission",
            params={"toolCall": {"title": "rm"}, "options": []},
        )
        event = client._build_permission_event(msg)
        assert len(event.options) == 2
        assert event.options[0]["id"] == "allow_once"

    def test_cached_tool_input_used(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        client._tool_call_inputs["tc-6"] = '{"cmd": "rm -rf /"}'
        msg = JsonRpcMessage(
            id=11,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-6"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "rm -rf" in event.tool_input
        # Cache consumed
        assert "tc-6" not in client._tool_call_inputs

    def test_fallback_input_from_tool_call(self):
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=12,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "write", "toolCallId": "tc-7", "input": {"path": "/x"}},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "/x" in event.tool_input

    def test_acp_spec_shape_records_optionids(self):
        """ACP-spec shape (optionId/name/kind) populates _permission_options."""
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=20,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[20] == {"once": "allow", "always": "allow_always"}

    def test_legacy_kiro_shape_records_optionids(self):
        """Legacy kiro shape (id/label, no kind) is classified by literal id."""
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=21,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[21] == {
            "once": "allow_once",
            "always": "allow_always",
        }

    def test_reject_option_recorded_even_without_allow(self):
        """A reject option must be recorded so reject_tool can send a clean
        ``selected`` reject (behavior:"deny") instead of ``cancelled`` (which
        the claude-agent-acp adapter turns into "Tool use aborted").
        """
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=22,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[22].get("reject") == "reject_once"

    def test_unknown_legacy_id_not_classified(self):
        """Unknown legacy ids do not get a synthesized kind."""
        client = AcpClient()
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=23,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [{"id": "weird_custom_id", "label": "?"}],
            },
        )
        client._build_permission_event(msg)
        assert 23 not in client._permission_options


class TestApproveTool:
    """Tests for approve_tool always= and recorded-option dispatch."""

    @pytest.mark.asyncio
    async def test_always_uses_recorded_optionid(self, tmp_path):
        from kiro_claw.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[42] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(42, always=True)
        client._send_response.assert_awaited_once_with(
            42,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow_always"}},
        )
        assert 42 not in client._permission_options

    @pytest.mark.asyncio
    async def test_once_uses_recorded_optionid(self, tmp_path):
        from kiro_claw.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[43] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(43)
        client._send_response.assert_awaited_once_with(
            43,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow"}},
        )

    @pytest.mark.asyncio
    async def test_no_recorded_falls_back_to_literal(self, tmp_path):
        from kiro_claw.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.approve_tool(44)
        client._send_response.assert_awaited_with(
            44,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        await client.approve_tool(45, always=True)
        client._send_response.assert_awaited_with(
            45,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )

    @pytest.mark.asyncio
    async def test_explicit_option_id_skips_recorded_pop(self, tmp_path):
        """Explicit option_id bypasses the recorded entry — defensive retries
        with a recorded entry left intact still send the explicit id."""
        from kiro_claw.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[46] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(46, option_id="custom_id")
        client._send_response.assert_awaited_with(
            46,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "custom_id"}},
        )
        assert client._permission_options[46] == {"once": "allow", "always": "allow_always"}

    @pytest.mark.asyncio
    async def test_reject_only_recorded_falls_back_to_literal_on_approve(self, tmp_path):
        """A request that advertised only a reject option records {"reject": ...}
        with no "once"/"always" keys. Approving it must fall back to the canonical
        allow id rather than KeyError-ing on the missing key."""
        from kiro_claw.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[47] = {"reject": "reject_once"}
        client._send_response = AsyncMock()
        await client.approve_tool(47)
        client._send_response.assert_awaited_with(
            47,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        assert 47 not in client._permission_options

        client._permission_options[48] = {"reject": "reject_once"}
        await client.approve_tool(48, always=True)
        client._send_response.assert_awaited_with(
            48,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )


class TestRejectTool:
    """Tests for reject_tool clean-reject vs cancelled dispatch."""

    @pytest.mark.asyncio
    async def test_uses_recorded_reject_optionid(self, tmp_path):
        """When a reject option was advertised, reject_tool sends a clean
        ``selected`` reject so the adapter returns behavior:"deny" rather than
        throwing "Tool use aborted" on a cancelled outcome."""
        from kiro_claw.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        # claude-agent-acp advertises optionId "reject" with kind "reject_once"
        client._permission_options[60] = {"once": "allow", "reject": "reject"}
        client._send_response = AsyncMock()
        await client.reject_tool(60)
        client._send_response.assert_awaited_once_with(
            60,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "reject"}},
        )
        assert 60 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_no_reject_option(self, tmp_path):
        """When no reject option was advertised (kiro-cli), reject_tool falls
        back to the cancelled outcome — kiro handles it as a clean rejection."""
        from kiro_claw.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[61] = {"once": "allow_once", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.reject_tool(61)
        client._send_response.assert_awaited_once_with(
            61,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )
        assert 61 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_nothing_recorded(self, tmp_path):
        """No recorded options at all → cancelled fallback (safe for kiro)."""
        from kiro_claw.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.reject_tool(62)
        client._send_response.assert_awaited_once_with(
            62,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )


class TestHandlePermission:
    """Tests for _handle_permission (auto-approve)."""

    @pytest.mark.asyncio
    async def test_auto_approves(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_claw.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=55,
            method="session/requestPermission",
            params={"toolCall": {"title": "bash"}, "options": []},
        )
        client.approve_tool = AsyncMock()

        await client._handle_permission(msg)
        client.approve_tool.assert_awaited_once_with(55)


class TestReadNewToolResultsSync:
    """Tests for _read_new_tool_results_sync."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so JSONL writes are isolated."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def test_no_session_returns_empty(self):
        client = AcpClient()
        client._session_id = None
        assert client._read_new_tool_results_sync() == []

    def test_missing_file_returns_empty(self):
        client = AcpClient()
        client._session_id = "nonexistent-session-xyz"
        assert client._read_new_tool_results_sync() == []

    def test_reads_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess"
        client._jsonl_pos = 0

        # Create fake JSONL
        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-1",
                            "content": [{"kind": "text", "data": "output here"}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-1"
            assert "output here" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_reads_json_kind_with_stdout(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-2"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-2.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-2",
                            "content": [{"kind": "json", "data": {"stdout": "hello world"}}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert "hello world" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_skips_non_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-3"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-3.jsonl"

        lines = [
            json.dumps({"kind": "Message", "data": {"text": "hi"}}) + "\n",
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-3",
                                    "content": [{"kind": "text", "data": "result"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n",
        ]
        jsonl_path.write_text("".join(lines))

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-3"
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_partial_line_not_consumed(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-4"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-4.jsonl"

        # Write a complete line + partial line (no trailing newline)
        complete = (
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-ok",
                                    "content": [{"kind": "text", "data": "done"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        partial = '{"kind": "ToolResults", "data": {"content": [{"kind": "toolRes'
        jsonl_path.write_text(complete + partial)

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-ok"
        finally:
            jsonl_path.unlink(missing_ok=True)


# ── Coverage push: additional coverage ──


class TestFormatCommandResult:
    """Tests for _format_command_result."""

    def test_structured_data_with_message(self):
        result = AcpClient._format_command_result({"data": {"key": "value"}, "message": "Done"})
        assert "Done" in result
        assert "```json" in result
        assert '"key"' in result

    def test_structured_data_without_message(self):
        result = AcpClient._format_command_result({"data": {"key": "val"}, "message": ""})
        assert "```json" in result
        assert '"key"' in result

    def test_agent_model_filtered(self):
        result = AcpClient._format_command_result(
            {"data": {"agent": "x", "model": "y"}, "message": ""}
        )
        # Only agent/model → display is empty → falls through to message
        assert result == ""

    def test_message_only(self):
        result = AcpClient._format_command_result({"message": "hello"})
        assert result == "hello"

    def test_empty_result(self):
        result = AcpClient._format_command_result({})
        assert result == ""


class TestParseSlashCommand:
    """Tests for _parse_slash_command."""

    def test_simple_command(self):
        name, args = AcpClient._parse_slash_command("/compact")
        assert name == "compact"
        assert args == {}

    def test_command_with_value(self):
        name, args = AcpClient._parse_slash_command("/agent planner")
        assert name == "agent"
        assert args == {"value": "planner"}

    def test_command_with_multi_word_value(self):
        name, args = AcpClient._parse_slash_command("/usage detailed view")
        assert name == "usage"
        assert args == {"value": "detailed view"}


class TestStreamCommand:
    """Tests for stream_command."""

    @pytest.mark.asyncio
    async def test_stream_command_yields_events(self):
        from kiro_claw.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        client._session_id = "s1"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=5)

        complete_msg = JsonRpcMessage(id=5, result={"message": "compacted", "data": {}})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client.stream_command("/compact"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_COMPLETE in kinds


class TestReadPromptResponse:
    """Tests for _read_prompt_response covering text accumulation and timeout."""

    @pytest.mark.asyncio
    async def test_accumulates_text(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient()
        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello "}}
            },
        )
        text_msg2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "world"}}
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", text_msg2
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        from kiro_claw.acp.client import AcpTimeoutError

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # yields nothing — simulates timeout
            return
            yield  # make it an async generator

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpTimeoutError):
            await client._read_prompt_response(req_id=1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_error_raises(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "fail"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpError, match="fail"):
            await client._read_prompt_response(req_id=1, timeout=5.0)


class TestPromptLoopReleasesTurnDone:
    """The core loop must release _turn_done on every exit so a cooperative
    cancel waiter (wait_turn_done) is not left blocking the full budget."""

    @pytest.mark.asyncio
    async def test_process_death_releases_turn_done(self, tmp_path):
        # An exception raised INSIDE the loop (process death) must still set
        # _turn_done via the finally, so a concurrent wait_turn_done returns
        # promptly instead of blocking its whole timeout.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._read_message = AsyncMock(side_effect=AcpProcessDied("boom"))
        client._is_process_alive = lambda: False

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()
        # wait_turn_done resolves immediately, not after the budget.
        reason = await client.wait_turn_done(timeout=5.0)
        assert reason == ""  # no clean stop reason → caller escalates correctly

    @pytest.mark.asyncio
    async def test_cancel_grace_exceeded_releases_turn_done(self, tmp_path):
        # The grace-window AcpError (cancel ack never arrived) must also release
        # the waiter rather than bypassing it.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 1000.0  # well past any grace
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        client._process = proc

        with pytest.raises(AcpError, match="grace window"):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_permission_auto_approved(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={"toolCall": {"title": "shell"}, "options": []},
        )
        complete_msg = JsonRpcMessage(id=1, result={})
        client.approve_tool = AsyncMock()

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        client.approve_tool.assert_awaited_once()
        assert result == ""


class TestSendMessageStreamBranches:
    """Tests for send_message_stream covering metadata and compaction branches."""

    @pytest.mark.asyncio
    async def test_metadata_tracked(self):
        from kiro_claw.acp.types import METHOD_METADATA, JsonRpcMessage

        client = AcpClient()
        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 75.0})
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "metadata", meta_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client.last_prompt_stats.context_pct == 75.0

    @pytest.mark.asyncio
    async def test_compaction_logged(self):
        from kiro_claw.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        client = AcpClient()
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "in_progress"}},
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        # No text chunks expected, just verifying no crash
        assert chunks == []

    @pytest.mark.asyncio
    async def test_timeout_sets_turn_done(self):
        """When prompt loop ends without complete, turn_done is set."""

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # Empty generator — simulates timeout
            return
            yield

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client._turn_done.is_set()


class TestKillProcessPipeClose:
    """Test pipe closing in _kill_process."""

    @pytest.mark.asyncio
    async def test_pipes_closed_before_kill(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        stdin_mock = MagicMock()
        stdout_mock = MagicMock()
        stderr_mock = MagicMock()
        proc.stdin = stdin_mock
        proc.stdout = stdout_mock
        proc.stderr = stderr_mock
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 100
        client._child_pids = {}

        with patch("os.killpg"), patch("os.getpgid", return_value=100), patch(
            "kiro_claw.acp.client._get_child_pids", return_value=[]
        ), patch("kiro_claw.acp.client._kill_escaped_children"):
            await client._kill_process()

        stdin_mock.close.assert_called_once()
        stdout_mock.close.assert_called_once()
        stderr_mock.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipe_close_exception_ignored(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close.side_effect = OSError("already closed")
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 101
        client._child_pids = {}

        with patch("os.killpg"), patch("os.getpgid", return_value=101), patch(
            "kiro_claw.acp.client._get_child_pids", return_value=[]
        ), patch("kiro_claw.acp.client._kill_escaped_children"):
            await client._kill_process()  # should not raise


# ── _extract_tool_call_update tests ──


class TestExtractToolCallUpdate:
    """Tests for real-time tool result extraction from session updates."""

    def _make_msg(self, update):
        from kiro_claw.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_missing_tool_call_id(self):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": ""})
        assert client._extract_tool_call_update(msg) is None

    def test_content_blocks_path(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [
                    {"content": {"type": "text", "text": "hello world"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.kind == "tool_result"
        assert event.tool_call_id == "tc-1"
        assert "hello world" in event.tool_output

    def test_raw_output_stdout_path(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawOutput": {
                    "items": [{"Json": {"stdout": "ls output here"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "ls output here" in event.tool_output

    def test_raw_output_json_fallback(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "rawOutput": {
                    "items": [{"Json": {"key": "value"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "key" in event.tool_output

    def test_content_takes_priority_over_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "content": [{"content": {"type": "text", "text": "from content"}}],
                "rawOutput": {"items": [{"Json": {"stdout": "from raw"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "from content" in event.tool_output
        assert "from raw" not in event.tool_output

    def test_empty_content_falls_through_to_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "content": [],
                "rawOutput": {"items": [{"Json": {"stdout": "fallback"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "fallback" in event.tool_output

    def test_no_output_returns_none(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-6",
                "content": [],
                "rawOutput": {"items": []},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_output_truncated_to_8000(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "content": [
                    {"content": {"type": "text", "text": "x" * 5000}},
                    {"content": {"type": "text", "text": "y" * 5000}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert len(event.tool_output) <= 8000

    def test_redaction_applied(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-8",
                "content": [
                    {"content": {"type": "text", "text": "key=AKIAIOSFODNN7EXAMPLE secret"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_output

    def test_ignores_non_dict_content_blocks(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-9",
                "content": ["not a dict", None, 42],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_non_text_content_type(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-10",
                "content": [{"content": {"type": "image", "url": "http://x"}}],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_none_params(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params=None)
        assert client._extract_tool_call_update(msg) is None


# ── _extract_tool_call_refinement tests ──


class TestExtractToolCallRefinement:
    """claude-agent-acp emits a follow-up tool_call_update once the streamed
    tool input is complete; the refinement carries title / kind / rawInput."""

    def _make_msg(self, update):
        from kiro_claw.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_claw.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_refinement(msg) is None

    def test_ignores_missing_tool_call_id(self):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": ""})
        assert client._extract_tool_call_refinement(msg) is None

    def test_pure_output_update_returns_none(self):
        # tool_call_update carrying ONLY content (no title/kind/rawInput) is
        # the result-only path handled by _extract_tool_call_update.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [{"content": {"type": "text", "text": "out"}}],
            }
        )
        assert client._extract_tool_call_refinement(msg) is None

    def test_refines_title_and_kind(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "title": "ls /tmp",
                "kind": "execute",
                "rawInput": {"command": "ls /tmp"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.kind == "tool_call_update"
        assert event.tool_call_id == "tc-2"
        assert event.title == "ls /tmp"
        assert event.tool_kind == "execute"
        assert "ls /tmp" in event.tool_input

    def test_prefers_rawinput_description_over_title(self):
        # Bash tool emits both `command` and `description` — the description
        # is the human-readable purpose ("List KiroClaw dashboard module
        # files"), and that's what we surface on the pill.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2b",
                "title": "ls /workplace/.../dashboard/",
                "kind": "execute",
                "rawInput": {
                    "command": "ls /workplace/.../dashboard/",
                    "description": "List KiroClaw dashboard module files",
                },
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "List KiroClaw dashboard module files"

    def test_blank_description_falls_back_to_title(self):
        # Whitespace-only description shouldn't override a useful title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2c",
                "title": "ls /tmp",
                "rawInput": {"command": "ls /tmp", "description": "   "},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "ls /tmp"

    def test_caches_input_for_permission_lookup(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "title": "grep foo",
                "rawInput": {"pattern": "foo"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # The refined input is also cached so a later permission request
        # for the same tool_call_id can pick it up.
        assert client._tool_call_inputs.get("tc-3") == event.tool_input

    def test_diff_content_block_replaces_raw_input(self):
        # Edit-style tools send the diff in content; the refinement should
        # surface the unified diff instead of the raw input dict.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "title": "Edit foo.py",
                "kind": "edit",
                "rawInput": {"old_string": "old", "new_string": "new", "file_path": "foo.py"},
                "content": [
                    {
                        "type": "diff",
                        "path": "foo.py",
                        "oldText": "old",
                        "newText": "new",
                    },
                ],
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # _make_unified_diff prefixes file headers
        assert "foo.py" in event.tool_input

    def test_redacts_credentials_in_input(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "title": "Bash",
                "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input

    def test_no_refinement_fields_returns_none(self):
        # Empty update with just tool_call_id should not emit a refinement.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-6",
            }
        )
        assert client._extract_tool_call_refinement(msg) is None

    def test_kind_only_emits_refinement(self):
        # Even a lone `kind` update is worth surfacing — avoids losing the
        # Bash/Edit distinction if the upstream renders it without a title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "kind": "search",
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_kind == "search"


class TestClaudeAcpMcpServers:
    """The session/new mcpServers builder for the claude-agent-acp backend."""

    def test_reads_registry_and_reshapes(self, tmp_path):
        reg = tmp_path / "kiroclaw.mcp.json"
        reg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "builder-mcp": {"command": "/bin/b", "args": ["--x"], "type": "stdio"},
                        "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"},
                    }
                }
            )
        )
        with patch("kiro_claw.acp.client._CC_MCP_FILE", reg):
            servers = _claude_acp_mcp_servers()
        by_name = {s["name"]: s for s in servers}
        assert by_name["builder-mcp"]["command"] == "/bin/b"
        # url server got an explicit http type
        assert by_name["deepwiki"]["type"] == "http"
        # kiroclaw core/cron always injected as stdio
        assert by_name["kiroclaw-core"]["type"] == "stdio"
        assert by_name["kiroclaw-core"]["args"] == ["mcp-core"]
        assert by_name["kiroclaw-cron"]["args"] == ["mcp-cron"]

    def test_missing_file_still_yields_core_cron(self, tmp_path):
        missing = tmp_path / "nope.json"
        with patch("kiro_claw.acp.client._CC_MCP_FILE", missing):
            servers = _claude_acp_mcp_servers()
        names = {s["name"] for s in servers}
        assert names == {"kiroclaw-core", "kiroclaw-cron"}
        assert all(s["type"] == "stdio" for s in servers)

    def test_malformed_json_degrades_to_core_cron(self, tmp_path):
        reg = tmp_path / "bad.json"
        reg.write_text("{not json")
        with patch("kiro_claw.acp.client._CC_MCP_FILE", reg):
            servers = _claude_acp_mcp_servers()
        names = {s["name"] for s in servers}
        assert names == {"kiroclaw-core", "kiroclaw-cron"}

    def test_stale_url_on_core_overwritten_with_stdio(self, tmp_path):
        # The on-disk registry may carry a dead gateway HTTP-MCP url for the
        # managed servers; it must be overwritten with the stdio command.
        reg = tmp_path / "kiroclaw.mcp.json"
        reg.write_text(
            json.dumps(
                {"mcpServers": {"kiroclaw-core": {"url": "http://localhost:8765/api/mcp/core"}}}
            )
        )
        with patch("kiro_claw.acp.client._CC_MCP_FILE", reg):
            servers = _claude_acp_mcp_servers()
        core = next(s for s in servers if s["name"] == "kiroclaw-core")
        assert core["type"] == "stdio"
        assert core["args"] == ["mcp-core"]
        assert "url" not in core

    def test_every_server_satisfies_adapter_required_arrays(self, tmp_path):
        # The claude-agent-acp zod schema requires env (stdio) and headers
        # (http/sse) as arrays. A registry mixing both transports must produce
        # servers that all carry their required array, or session/new fails the
        # whole batch with -32602 Invalid params.
        reg = tmp_path / "kiroclaw.mcp.json"
        reg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "builder-mcp": {"command": "/bin/b", "args": ["--x"], "type": "stdio"},
                        "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"},
                    }
                }
            )
        )
        with patch("kiro_claw.acp.client._CC_MCP_FILE", reg):
            servers = _claude_acp_mcp_servers()
        for s in servers:
            if s.get("type") in ("http", "sse"):
                assert isinstance(s.get("headers"), list), s
            else:
                assert isinstance(s.get("env"), list), s


class TestCaptureAvailableModels:
    """Capturing the backend-advertised model list from session responses."""

    def _client(self):
        return AcpClient(acp_backend=ACP_BACKEND_CLAUDE)

    def test_captures_versioned_models(self):
        c = self._client()
        c._capture_available_models(
            {
                "sessionId": "s",
                "models": {
                    "currentModelId": "claude-opus-4-8-1m",
                    "availableModels": [
                        {"modelId": "claude-opus-4-8-1m", "name": "Opus 4.8", "description": "new"},
                        {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
                    ],
                },
            }
        )
        am = c.available_models()
        assert [m["modelId"] for m in am] == ["claude-opus-4-8-1m", "claude-sonnet-4-6"]
        assert am[1]["description"] == ""  # missing description -> empty string

    def test_no_models_key_leaves_empty(self):
        c = self._client()
        c._capture_available_models({"sessionId": "s"})
        assert c.available_models() == []

    def test_entries_without_modelid_skipped(self):
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"name": "x"}, {"modelId": "ok", "name": "OK"}]}}
        )
        assert [m["modelId"] for m in c.available_models()] == ["ok"]

    def test_value_field_accepted_as_model_id(self):
        # ACP config-option shape uses "value" rather than "modelId".
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"value": "m1", "name": "M1"}]}}
        )
        assert c.available_models()[0]["modelId"] == "m1"


def _scripted_process(lines, *, returncode=None):
    """Build a mock subprocess whose stdout.readline yields *lines* in order.

    Each entry of *lines* is a dict (serialized to a JSON-RPC line) or a raw
    bytes value. After the list is exhausted, readline blocks-then-returns an
    empty timeout-friendly value so _read_message's wait_for sees nothing new.
    """
    queue = deque(lines)

    async def _readline():
        if queue:
            item = queue.popleft()
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
            return (json.dumps(item) + "\n").encode()
        # Nothing left — emulate a quiet stream (no more frames this read).
        await asyncio.sleep(0)
        return b""

    proc = MagicMock()
    stdout = AsyncMock()
    stdout.readline = AsyncMock(side_effect=_readline)
    proc.stdout = stdout
    proc.returncode = returncode
    # stdin used by _send_error / _send_response.
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin
    return proc


class TestWaitForResponseDeferral:
    """F1: _wait_for_response must not spin on inbound server requests or
    foreign-id responses, must not drop them, and must re-inject them."""

    @pytest.mark.asyncio
    async def test_inbound_permission_request_deferred_not_spun(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # Awaiting response for req_id=7. A server->client permission request
        # arrives first carrying id=7 (colliding namespace), THEN the real
        # response for req 7 arrives.
        perm = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "options": [], "toolCall": {"title": "x"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        client._process = _scripted_process([perm, resp])

        result = await asyncio.wait_for(client._wait_for_response(7, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Permission request must be re-injected (not dropped, not spun).
        assert len(client._buffer) == 1
        buffered = client._buffer.popleft()
        assert buffered.method == "session/request_permission"
        assert buffered.id == 7

    @pytest.mark.asyncio
    async def test_foreign_id_response_preserved(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        foreign = {"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        client._process = _scripted_process([foreign, resp])

        result = await asyncio.wait_for(client._wait_for_response(3, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        assert len(client._buffer) == 1
        assert client._buffer.popleft().id == 99

    @pytest.mark.asyncio
    async def test_notification_goes_to_mcp_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"ok": True}}
        client._process = _scripted_process([notif, resp])

        result = await asyncio.wait_for(client._wait_for_response(5, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Notification buffered for drain, NOT re-injected into _buffer.
        assert len(client._buffer) == 0
        assert len(client._mcp_notifications) == 1
        assert client._mcp_notifications[0].method == "session/update"

    @pytest.mark.asyncio
    async def test_deferred_reinjected_in_order_on_timeout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        first = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {},
        }
        second = {"jsonrpc": "2.0", "id": 88, "result": {"other": True}}
        # No matching response ever arrives -> timeout. Deferred frames must
        # still be re-injected in arrival order.
        client._process = _scripted_process([first, second])

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(7, timeout=1.0), timeout=10.0)
        assert len(client._buffer) == 2
        m0 = client._buffer.popleft()
        m1 = client._buffer.popleft()
        assert m0.method == "session/request_permission"
        assert m1.id == 88


class TestWaitForResponseActivityDeadline:
    """Low-A: a steady stream of notifications keeps _wait_for_response alive
    past the base timeout until the real response arrives."""

    @pytest.mark.asyncio
    async def test_streaming_notifications_extend_deadline(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)

        # Virtual clock so the test is fast and deterministic. Each readline
        # advances time by 0.05s; base timeout is 0.1s. Without activity-based
        # extension the call would die after ~2 reads; with it, the stream of
        # notifications keeps pushing the deadline out until the response lands.
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        frames = [notif] * 20 + [{"jsonrpc": "2.0", "id": 4, "result": {"loaded": True}}]
        queue = deque(frames)

        async def _readline():
            if queue:
                clock["t"] += 0.05  # each frame advances the virtual clock
                return (json.dumps(queue.popleft()) + "\n").encode()
            return b""

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_claw.acp.client.time.monotonic", fake_monotonic)

        result = await asyncio.wait_for(client._wait_for_response(4, timeout=0.1), timeout=10.0)
        assert result == {"loaded": True}
        # All 20 notifications were drained while the deadline kept extending.
        assert len(client._mcp_notifications) == 20

    @pytest.mark.asyncio
    async def test_hard_cap_eventually_times_out(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        clock = {"t": 5000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}

        async def _readline():
            # Endless notifications, large time jumps — never a matching
            # response. The absolute hard cap must eventually fire.
            clock["t"] += 30.0
            return (json.dumps(notif) + "\n").encode()

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_claw.acp.client.time.monotonic", fake_monotonic)

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(1, timeout=0.1), timeout=10.0)


class TestProcessMessageUnknownServerRequest:
    """F5: unknown server->client requests are classified for a -32601 reply,
    not silently skipped."""

    def test_unknown_server_request_classified(self, tmp_path):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=12, method="fs/read_text_file", params={"path": "/x"})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_terminal_create_classified(self, tmp_path):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=3, method="terminal/create", params={})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_notification_still_skipped(self, tmp_path):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Unknown notification (method, NO id) is not a request -> skip.
        msg = JsonRpcMessage(method="some/unknown_notification", params={})
        assert client._process_message(msg, req_id=1) == "skip"

    def test_known_permission_request_not_unknown(self, tmp_path):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=2, method="session/request_permission", params={})
        assert client._process_message(msg, req_id=1) == "permission"

    @pytest.mark.asyncio
    async def test_reject_sends_method_not_found_error(self, tmp_path):
        from kiro_claw.acp.client import _JSONRPC_METHOD_NOT_FOUND
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=42, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)

        client._process.stdin.write.assert_called_once()
        written = client._process.stdin.write.call_args[0][0].decode()
        payload = json.loads(written)
        assert payload["id"] == 42
        assert payload["error"]["code"] == _JSONRPC_METHOD_NOT_FOUND
        assert "terminal/create" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_reject_noop_when_no_id(self, tmp_path):
        from kiro_claw.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=None, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)
        client._process.stdin.write.assert_not_called()


class TestFormatAcpError:
    """Tests for _format_acp_error — Bedrock-aware error rewriting.

    Covers the bug filed at task 86089e43 (Mesh-1751): ACP backend errors used
    to be surfaced as the raw JSON-RPC dict (`Prompt error: {'code': -32603,
    ...}`), which dead-ends users when the picker can't expose a valid
    alternative. The helper rewrites known Bedrock failures into actionable
    text while preserving the request_id for support correlation, and scrubs
    embedded credentials / exfiltration URLs as defense-in-depth.
    """

    def test_non_dict_falls_back(self):
        assert _format_acp_error(None) == "Prompt error: None"
        assert _format_acp_error("boom") == "Prompt error: boom"

    def test_unknown_dict_preserves_raw(self):
        err = {"code": -32000, "message": "Something else", "data": "weird"}
        out = _format_acp_error(err)
        assert out.startswith("Prompt error: ")
        assert "Something else" in out
        assert "weird" in out

    def test_model_not_available_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The model 'opus' "
                "is not available. Please use '/model' to select a different "
                "model and try again. (request_id: 3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e)"
            ),
        }
        out = _format_acp_error(err)
        assert "unavailable on Bedrock" in out
        assert "'opus'" in out
        assert "model picker" in out
        assert "settings.json" in out
        # Request id is preserved for support correlation.
        assert "3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e" in out
        # Should not leak the raw dict prefix when we have a real rewrite.
        assert "Prompt error: {" not in out

    def test_throttling_exception_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ThrottlingException: Too many requests "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "wait" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_too_many_requests_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "TooManyRequestsException: rate limited"}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_access_denied_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDeniedException: not authorized to invoke",
        }
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()
        assert "aws sso login" in out.lower()

    def test_expired_token_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "ExpiredToken: signature expired"}
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()

    def test_missing_request_id_omits_suffix(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'sonnet' is not available.",
        }
        out = _format_acp_error(err)
        assert "request_id" not in out
        assert "'sonnet'" in out

    def test_throttle_keyword_in_message(self):
        # Some backends put the trigger word in `message` rather than `data`.
        err = {"code": -32603, "message": "Rate limit exceeded", "data": ""}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_credentials_in_data_are_redacted(self):
        """AWS access keys embedded in upstream errors must not leak to the UI.

        Recognized error patterns (auth/throttle/model-unavailable) already drop
        the `data` field when constructing the rewritten message, so the secret
        is gone simply by virtue of the rewrite. The redaction layer is the
        defense-in-depth fallback for the unknown-shape path; this test pins
        the absence guarantee on the recognized-pattern path.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDenied: AKIAIOSFODNN7EXAMPLE not authorized",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credentials_in_unknown_dict_fallback_are_redacted(self):
        """The fallback path echoes raw dict — must still scrub secrets."""
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # Fallback prefix is preserved so operators can recognize the shape.
        assert "Prompt error:" in out

    def test_exfiltration_url_in_unknown_dict_fallback_is_redacted(self):
        """The fallback path echoes raw dict — exfil URLs must also be scrubbed.

        Pairs with `test_credentials_in_unknown_dict_fallback_are_redacted` to
        cover the second redaction layer (`redact_exfiltration_urls`).
        """
        # Use a URL whose query carries a base64-blob — matches _EXFIL_PATTERNS
        # and is what real provider error payloads tend to look like when they
        # echo signed callback URLs.
        leaked_blob = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVTRUNSRVRBQ0NFU1NLRVk" + "A" * 30
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": f"callback to https://attacker.example.com/exfil?token={leaked_blob}",
        }
        out = _format_acp_error(err)
        assert leaked_blob not in out, "leaked credential blob must be redacted"
        assert "REDACTED" in out

    def test_sensitive_content_emits_log_warning(self, caplog):
        """When the redaction layer scrubs anything, a warning MUST be logged.

        Silent scrubbing hides upstream-leak signals from security review;
        the warning lets operators notice that a provider echoed sensitive
        content back. The warning intentionally includes only counts — never
        the redacted values.
        """
        import logging

        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_claw.acp.client"):
            _format_acp_error(err)

        warnings = [r for r in caplog.records if "sensitive content" in r.getMessage()]
        assert warnings, "expected a redaction warning to be logged"
        assert "AKIAIOSFODNN7EXAMPLE" not in warnings[0].getMessage()


class TestSettingsLocalModelInjection:
    """The per-session settings.local.json must carry the full availableModels
    allowlist so the adapter resolves the [1m] id (1M window) even over a
    polluted user ~/.claude (availableModels=['opus','sonnet'])."""

    def test_settings_local_has_available_models(self, tmp_path):
        import json

        from kiro_claw import model_registry as mr
        from kiro_claw.acp.client import AcpClient
        from kiro_claw.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(
            work_dir=tmp_path,
            model="global.anthropic.claude-opus-4-8[1m]",
            agent="kiroclaw",
            acp_backend=ACP_BACKEND_CLAUDE,
        )
        client._write_claude_local_settings()

        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert data["permissions"]["defaultMode"] == "default"
        for pid in mr.available_models("claude_code"):
            assert pid in data["availableModels"]
        assert data["model"] == "global.anthropic.claude-opus-4-8[1m]"

    def test_settings_local_omits_model_when_auto(self, tmp_path):
        import json

        from kiro_claw.acp.client import AcpClient
        from kiro_claw.acp.types import ACP_BACKEND_CLAUDE

        # DEFAULT_MODEL ("auto") must NOT be written as a literal model value.
        client = AcpClient(
            work_dir=tmp_path,
            model=None,
            agent="kiroclaw",
            acp_backend=ACP_BACKEND_CLAUDE,
        )
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "model" not in data
        assert data["availableModels"]  # allowlist still present
