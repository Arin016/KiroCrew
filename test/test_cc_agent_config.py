"""Tests for cc_agent.py config bridge."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from kiro_claw.cc_agent import (
    _CC_AVAILABLE_MODELS,
    _CC_DEFAULT_MODEL,
    _CC_DENY_PATTERNS,
    _translate_matcher,
    _translate_tool_list,
    acp_servers_from_cc_map,
    build_acp_mcp_servers,
    generate_cc_agent_markdown,
    generate_mcp_json,
    install_cc_agent,
    install_cc_global_deny_settings,
)


class TestGenerateMarkdown:
    def test_basic(self):
        cfg = {"name": "kiroclaw", "model": "opus", "prompt": "Hello."}
        md = generate_cc_agent_markdown(cfg)
        assert md.startswith("---\n")
        assert "name: kiroclaw" in md
        assert "model: opus" in md
        assert "Hello." in md

    def test_no_model(self):
        md = generate_cc_agent_markdown({"name": "x", "prompt": "y"})
        assert "model:" not in md

    def test_tools_and_servers(self):
        cfg = {"name": "x", "allowedTools": ["a", "b"], "mcpServers": {"s1": {}}, "prompt": "p"}
        md = generate_cc_agent_markdown(cfg)
        assert "- a\n" in md
        assert "- s1\n" in md

    def test_sensitive_file_uri_blocked(self, tmp_path: Path):
        # The sensitivity check is now centralized in hooks.safe_read_file,
        # which raises PermissionError on a credential path. A real file is
        # needed so the is_file() guard passes and the read is attempted.
        secret = tmp_path / "creds"
        secret.write_text("AWS_SECRET=xyz", encoding="utf-8")
        # _resolve_prompt_content lazily imports safe_read_file from hooks, so
        # patch it at the source module.
        with patch("kiro_claw.hooks.safe_read_file", side_effect=PermissionError("blocked")):
            md = generate_cc_agent_markdown({"name": "t", "prompt": f"file://{secret}"})
        assert "You are t, an autonomous AI agent." in md

    def test_file_uri_loads(self, tmp_path: Path):
        f = tmp_path / "p.md"
        f.write_text("loaded", encoding="utf-8")
        md = generate_cc_agent_markdown({"name": "t", "prompt": f"file://{f}"})
        assert "loaded" in md

    def test_empty_prompt_fallback(self):
        md = generate_cc_agent_markdown({"name": "bot"})
        assert "You are bot, an autonomous AI agent." in md


class TestGenerateMcpJson:
    def test_includes_kiroclaw_servers(self):
        cfg = {
            "mcpServers": {"kiroclaw-core": {"command": "mc", "args": ["mcp-core"]}, "other": {}}
        }
        r, _, _ = generate_mcp_json(cfg)
        assert "kiroclaw-core" in r["mcpServers"]
        assert "kiroclaw-cron" in r["mcpServers"]
        assert "other" not in r["mcpServers"]

    def test_defaults_when_missing(self):
        r, _, _ = generate_mcp_json({"mcpServers": {}})
        assert r["mcpServers"]["kiroclaw-core"]["args"] == ["mcp-core"]
        assert r["mcpServers"]["kiroclaw-cron"]["args"] == ["mcp-cron"]

    def test_managed_servers_forced_to_stdio_over_stale_url(self):
        # A stale url on a managed server (abandoned gateway HTTP-MCP endpoint)
        # must be overwritten with the canonical stdio command, else core/cron
        # silently fail to load.
        cfg = {
            "mcpServers": {
                "kiroclaw-core": {"url": "http://localhost:8765/api/mcp/core"},
                "kiroclaw-cron": {"url": "http://localhost:8765/api/mcp/cron"},
            }
        }
        r, _, _ = generate_mcp_json(cfg)
        for name, args in (("kiroclaw-core", ["mcp-core"]), ("kiroclaw-cron", ["mcp-cron"])):
            assert r["mcpServers"][name].get("type") == "stdio"
            assert r["mcpServers"][name].get("command")
            assert r["mcpServers"][name].get("args") == args
            assert "url" not in r["mcpServers"][name]


class TestAcpServersFromCcMap:
    """The CC map → ACP session/new array reshaper for the claude backend."""

    def test_stdio_server_with_env(self):
        cc = {"srv": {"command": "/bin/x", "args": ["a"], "type": "stdio", "env": {"K": "v"}}}
        out = acp_servers_from_cc_map(cc)
        assert out == [
            {
                "name": "srv",
                "command": "/bin/x",
                "args": ["a"],
                "type": "stdio",
                "env": [{"name": "K", "value": "v"}],
            }
        ]

    def test_url_server_gets_explicit_http_type(self):
        # .mcp.json drops the type for remote servers; the adapter only treats
        # a server as remote when type is http/sse, so we must re-add it. The
        # adapter's zod schema requires headers as an array, so an empty list is
        # always emitted.
        cc = {"remote": {"url": "https://mcp.example.com/mcp"}}
        out = acp_servers_from_cc_map(cc)
        assert out == [
            {
                "name": "remote",
                "type": "http",
                "url": "https://mcp.example.com/mcp",
                "headers": [],
            }
        ]

    def test_url_server_preserves_sse_and_headers(self):
        cc = {
            "r": {
                "url": "https://x/mcp",
                "type": "sse",
                "headers": {"Authorization": "Bearer t"},
            }
        }
        out = acp_servers_from_cc_map(cc)
        assert out[0]["type"] == "sse"
        assert out[0]["headers"] == [{"name": "Authorization", "value": "Bearer t"}]

    def test_skips_entries_without_command_or_url(self):
        cc = {"bad": {"type": "stdio"}, "ok": {"command": "/bin/x"}}
        out = acp_servers_from_cc_map(cc)
        assert [s["name"] for s in out] == ["ok"]

    def test_env_values_coerced_to_string(self):
        cc = {"s": {"command": "/bin/x", "env": {"PORT": 8080}}}
        out = acp_servers_from_cc_map(cc)
        assert out[0]["env"] == [{"name": "PORT", "value": "8080"}]

    def test_stdio_without_env_emits_empty_array(self):
        # The adapter's zMcpServerStdio.env is z.array(...) — required. A stdio
        # server with no env must still carry env: [] or session/new rejects the
        # whole batch with -32602 (expected array, received undefined).
        cc = {"core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]}}
        out = acp_servers_from_cc_map(cc)
        assert out[0]["env"] == []

    def test_http_without_headers_emits_empty_array(self):
        # zMcpServerHttp.headers is z.array(...) — required. A url server with no
        # headers must carry headers: [] for the same reason.
        cc = {"remote": {"url": "https://mcp.example.com/mcp"}}
        out = acp_servers_from_cc_map(cc)
        assert out[0]["headers"] == []

    def test_build_pipeline_servers_carry_required_arrays(self):
        # Every server produced for session/new must satisfy the adapter schema:
        # stdio entries carry env (array), remote entries carry headers (array).
        out = build_acp_mcp_servers({"mcpServers": {}})
        for s in out:
            if s.get("type") in ("http", "sse") or "url" in s:
                assert isinstance(s.get("headers"), list)
            else:
                assert isinstance(s.get("env"), list)

    def test_build_pipeline_guarantees_core_cron_stdio(self):
        # build_acp_mcp_servers runs generate_mcp_json first, so core/cron are
        # always present and stdio even from an empty config.
        out = build_acp_mcp_servers({"mcpServers": {}})
        by_name = {s["name"]: s for s in out}
        assert by_name["kiroclaw-core"]["type"] == "stdio"
        assert by_name["kiroclaw-core"]["args"] == ["mcp-core"]
        assert by_name["kiroclaw-cron"]["args"] == ["mcp-cron"]


class TestInstall:
    def test_creates_file(self, tmp_path: Path):
        d = tmp_path / "agents"
        with patch("kiro_claw.cc_agent.CC_AGENTS_DIR", d):
            p = install_cc_agent({"name": "x", "prompt": "hi"})
        assert p.exists()
        assert p.name == "kiroclaw.md"


# ---------------------------------------------------------------------------
# install_cc_agent_config: writes to ~/.claude/agents/, never ~/.mcp.json
# ---------------------------------------------------------------------------


class TestInstallCcAgentConfigRenderer:
    """Verify the CC renderer writes to the symmetric .claude/agents/ location."""

    def _set_cc_provider(self, stack):
        """Patch config loader to report claude_code provider."""
        from unittest.mock import MagicMock

        mock_cfg = MagicMock()
        mock_cfg.agent.provider = "claude_code"
        mock_loader = MagicMock()
        mock_loader.load.return_value = mock_cfg
        stack.enter_context(patch("kiro_claw.config.loader.KiroClawConfig", mock_loader))

    def test_writes_mcp_json_to_claude_agents_dir(self, tmp_path: Path):
        """The MCP registry must land at ~/.claude/agents/kiroclaw.mcp.json."""
        import json
        from contextlib import ExitStack

        from kiro_claw.agent import install_cc_agent_config

        claude_agents = tmp_path / "claude-agents"
        mcp_file = claude_agents / "kiroclaw.mcp.json"
        merged = {"name": "kiroclaw", "mcpServers": {"slack-mcp": {"command": "slack", "args": []}}}

        with ExitStack() as stack:
            self._set_cc_provider(stack)
            stack.enter_context(patch("kiro_claw.agent.CC_MCP_FILE", mcp_file))
            stack.enter_context(patch("kiro_claw.cc_agent.CC_AGENTS_DIR", claude_agents))
            stack.enter_context(
                patch("kiro_claw.agent._toolbox_cc_defaults_dir", return_value=None)
            )
            install_cc_agent_config(merged)

        assert mcp_file.exists(), "CC MCP registry must be written"
        data = json.loads(mcp_file.read_text())
        assert "slack-mcp" in data["mcpServers"]
        assert (claude_agents / "kiroclaw.md").exists(), "CC agent markdown must be written"

    def test_does_not_write_user_mcp_json(self, tmp_path: Path, monkeypatch):
        """~/.mcp.json must never be touched by the renderer."""
        from contextlib import ExitStack

        from kiro_claw.agent import install_cc_agent_config

        # Create a canary file that the old code would have clobbered.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        canary = fake_home / ".mcp.json"
        canary.write_text('{"touched": "by-someone-else"}', encoding="utf-8")

        monkeypatch.setenv("HOME", str(fake_home))
        merged = {"name": "kiroclaw", "mcpServers": {"x": {"command": "x", "args": []}}}
        claude_agents = fake_home / ".claude" / "agents"
        mcp_file = claude_agents / "kiroclaw.mcp.json"

        with ExitStack() as stack:
            self._set_cc_provider(stack)
            stack.enter_context(patch("kiro_claw.agent.CC_MCP_FILE", mcp_file))
            stack.enter_context(patch("kiro_claw.cc_agent.CC_AGENTS_DIR", claude_agents))
            stack.enter_context(
                patch("kiro_claw.agent._toolbox_cc_defaults_dir", return_value=None)
            )
            install_cc_agent_config(merged)

        assert (
            canary.read_text() == '{"touched": "by-someone-else"}'
        ), "Renderer must not touch ~/.mcp.json"
        assert mcp_file.exists()

    def test_renders_regardless_of_active_provider(self, tmp_path):
        """CC artifacts are always rendered so switching providers is instant.

        Regression: the previous implementation short-circuited when
        provider != claude_code, leaving the CC agent file stale. That
        caused AIM-installed servers not to appear in CC sessions after
        switching providers.
        """
        from contextlib import ExitStack

        from kiro_claw.agent import install_cc_agent_config

        mcp_file = tmp_path / "kiroclaw.mcp.json"
        claude_agents = tmp_path / "claude_agents"
        claude_agents.mkdir()
        merged = {"model": "claude", "mcpServers": {"x": {"command": "x"}}}
        with ExitStack() as stack:
            stack.enter_context(patch("kiro_claw.agent.CC_MCP_FILE", mcp_file))
            stack.enter_context(patch("kiro_claw.cc_agent.CC_AGENTS_DIR", claude_agents))
            stack.enter_context(
                patch("kiro_claw.agent._toolbox_cc_defaults_dir", return_value=None)
            )
            result = install_cc_agent_config(merged)

        # Even though no provider check fires, artifacts are written.
        assert result is not None
        assert mcp_file.exists()
        assert (claude_agents / "kiroclaw.md").exists()


# ---------------------------------------------------------------------------
# Translation tables and helpers
# ---------------------------------------------------------------------------


class TestTranslateToolList:
    """Verify kiro tool names translate to CC equivalents."""

    def test_standard_tools(self):
        result = _translate_tool_list(["fs_read", "execute_bash", "@kiroclaw-cron"])
        assert result == ["Read", "Bash", "mcp__kiroclaw-cron"]

    def test_drops_use_aws(self):
        result = _translate_tool_list(["fs_read", "use_aws", "grep"])
        assert result == ["Read", "Grep"]
        assert "use_aws" not in result

    def test_unknown_tool_passes_through(self):
        result = _translate_tool_list(["fs_read", "some_future_tool"])
        assert result == ["Read", "some_future_tool"]

    def test_mcp_server_prefix(self):
        result = _translate_tool_list(["@builder-mcp", "@slack-mcp"])
        assert result == ["mcp__builder-mcp", "mcp__slack-mcp"]

    def test_shell_maps_to_bash(self):
        result = _translate_tool_list(["shell"])
        assert result == ["Bash"]


class TestTranslateMatcher:
    """Verify glob-to-regex matcher translation."""

    def test_exact_match_tool_rename(self):
        assert _translate_matcher("execute_bash") == "Bash"

    def test_wildcard_star(self):
        assert _translate_matcher("*") == ".*"

    def test_glob_with_dot(self):
        # Dots should be escaped
        result = _translate_matcher("aws.s3*")
        assert result == "aws\\.s3.*"

    def test_question_mark(self):
        result = _translate_matcher("test?name")
        assert result == "test.name"

    def test_empty_pattern(self):
        assert _translate_matcher("") == ""

    def test_unknown_exact_passes_through(self):
        assert _translate_matcher("some_custom_tool") == "some_custom_tool"

    def test_at_server_matcher_translated(self):
        # An @server exact-match matcher must become mcp__server, else the CC
        # hook never fires.
        assert _translate_matcher("@kiroclaw-core") == "mcp__kiroclaw-core"
        assert _translate_matcher("@builder-mcp") == "mcp__builder-mcp"


# ---------------------------------------------------------------------------
# Hook translation
# ---------------------------------------------------------------------------


class TestHookTranslation:
    """Verify kiro hooks translate to CC nested hook block shape."""

    def test_kiro_hooks_to_cc_nested_shape(self):
        cfg = {
            "name": "test",
            "hooks": {
                "preToolUse": [{"matcher": "execute_bash", "command": "echo pre", "timeout": 10}],
                "postToolUse": [{"matcher": "*", "command": "echo post"}],
            },
        }
        md = generate_cc_agent_markdown(cfg)
        assert "PreToolUse" in md
        assert "PostToolUse" in md
        # Matcher should be translated: execute_bash → Bash
        assert "Bash" in md
        # Wildcard * → .*
        assert ".*" in md
        # Command preserved
        assert "echo pre" in md
        assert "echo post" in md

    def test_agent_spawn_becomes_session_start_with_startup_matcher(self):
        cfg = {
            "name": "test",
            "hooks": {
                "agentSpawn": [{"command": "echo hello"}],
            },
        }
        md = generate_cc_agent_markdown(cfg)
        assert "SessionStart" in md
        assert "startup" in md

    def test_hooks_with_non_default_timeout(self):
        cfg = {
            "name": "test",
            "hooks": {
                "userPromptSubmit": [{"command": "echo submit", "timeout": 60}],
            },
        }
        md = generate_cc_agent_markdown(cfg)
        assert "timeout: 60" in md

    def test_hooks_with_default_timeout_omits_field(self):
        cfg = {
            "name": "test",
            "hooks": {
                "userPromptSubmit": [{"command": "echo submit", "timeout": 30}],
            },
        }
        md = generate_cc_agent_markdown(cfg)
        # Default timeout (30) should not appear in output
        assert "timeout:" not in md


# ---------------------------------------------------------------------------
# File URI prompt resolution
# ---------------------------------------------------------------------------


class TestFileUriPrompt:
    """Verify file:// URI prompt content is inlined into markdown body."""

    def test_file_uri_loads_content(self, tmp_path: Path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("Custom system prompt content.", encoding="utf-8")
        cfg = {"name": "agent", "prompt": f"file://{prompt_file}"}
        md = generate_cc_agent_markdown(cfg)
        assert "Custom system prompt content." in md

    def test_missing_file_uses_fallback(self):
        cfg = {"name": "bot", "prompt": "file:///nonexistent/path.md"}
        md = generate_cc_agent_markdown(cfg)
        assert "You are bot, an autonomous AI agent." in md

    def test_explicit_prompt_body_overrides(self, tmp_path: Path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("From file.", encoding="utf-8")
        cfg = {"name": "agent", "prompt": f"file://{prompt_file}"}
        md = generate_cc_agent_markdown(cfg, prompt_body="Override body.")
        assert "Override body." in md
        assert "From file." not in md


# ---------------------------------------------------------------------------
# MCP disabled / autoApprove / disabledTools
# ---------------------------------------------------------------------------


class TestMcpDisabledAutoApprove:
    """Verify disabled, autoApprove, and disabledTools handling."""

    def test_disabled_entry_not_emitted(self):
        cfg = {
            "mcpServers": {
                "disabled-srv": {
                    "command": "nope",
                    "args": [],
                    "disabled": True,
                },
                "active-srv": {"command": "yes", "args": ["run"]},
            }
        }
        mcp_data, _, _ = generate_mcp_json(cfg)
        assert "disabled-srv" not in mcp_data["mcpServers"]
        assert "active-srv" in mcp_data["mcpServers"]

    def test_auto_approve_populates_settings_allow(self):
        cfg = {
            "mcpServers": {
                "arcc": {
                    "command": "arcc",
                    "args": [],
                    "autoApprove": ["search_arcc", "list_docs"],
                },
            }
        }
        _, allow_list, _ = generate_mcp_json(cfg)
        assert "mcp__arcc__search_arcc" in allow_list
        assert "mcp__arcc__list_docs" in allow_list

    def test_disabled_tools_populates_disallowed(self):
        cfg = {
            "mcpServers": {
                "builder-mcp": {
                    "command": "builder-mcp",
                    "args": [],
                    "disabledTools": ["SkillsTool", "WorkspaceSearch"],
                },
            }
        }
        _, _, disallowed = generate_mcp_json(cfg)
        assert "mcp__builder-mcp__SkillsTool" in disallowed
        assert "mcp__builder-mcp__WorkspaceSearch" in disallowed

    def test_pre_existing_settings_allow_preserved(self):
        cfg = {
            "mcpServers": {
                "srv": {"command": "x", "args": [], "autoApprove": ["foo"]},
            }
        }
        _, allow_list, _ = generate_mcp_json(cfg, settings_allow=["existing_perm"])
        assert "existing_perm" in allow_list
        assert "mcp__srv__foo" in allow_list


# ---------------------------------------------------------------------------
# Full markdown generation with description and permissionMode
# ---------------------------------------------------------------------------


class TestFullMarkdownGeneration:
    """Verify complete CC agent markdown output."""

    def test_description_emitted(self):
        cfg = {"name": "test", "description": "A test agent", "prompt": "Hi."}
        md = generate_cc_agent_markdown(cfg)
        assert "description:" in md
        assert "A test agent" in md

    def test_permission_mode_emitted(self):
        cfg = {"name": "test", "permissionMode": "auto", "prompt": "Hi."}
        md = generate_cc_agent_markdown(cfg)
        assert "permissionMode: auto" in md

    def test_backward_compat_no_hooks(self):
        """Callers without hooks still get a working CC agent."""
        cfg = {"name": "simple", "model": "opus", "prompt": "Do stuff."}
        md = generate_cc_agent_markdown(cfg)
        assert md.startswith("---\n")
        assert "hooks:" not in md
        assert "Do stuff." in md


class TestInstallCcGlobalDenySettings:
    def test_writes_permissions_deny(self, tmp_path: Path):
        target = tmp_path / "settings.json"
        install_cc_global_deny_settings(target)
        data = json.loads(target.read_text())
        assert "permissions" in data
        assert "deny" in data["permissions"]
        assert len(data["permissions"]["deny"]) == len(_CC_DENY_PATTERNS)
        # Spot-check one of the actual bundled patterns survives
        assert "Bash(aws configure get*)" in data["permissions"]["deny"]

    def test_preserves_other_keys(self, tmp_path: Path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"theme": "dark", "permissions": {"allow": ["Read"]}}))
        install_cc_global_deny_settings(target)
        data = json.loads(target.read_text())
        assert data["theme"] == "dark"
        assert data["permissions"]["allow"] == ["Read"]
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)

    def test_idempotent(self, tmp_path: Path):
        target = tmp_path / "settings.json"
        install_cc_global_deny_settings(target)
        first = target.read_text()
        install_cc_global_deny_settings(target)
        assert target.read_text() == first

    def test_corrupt_existing_overwritten(self, tmp_path: Path):
        target = tmp_path / "settings.json"
        target.write_text("not valid json {")
        install_cc_global_deny_settings(target)
        data = json.loads(target.read_text())
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "deeply" / "nested" / "settings.json"
        install_cc_global_deny_settings(target)
        assert target.exists()

    def test_does_not_write_model_keys_to_user_file(self, tmp_path: Path):
        # Model config is injected via the KiroClaw-owned per-session
        # settings.local.json — NOT the user's ~/.claude. install_cc_global_deny
        # must write deny + marker only.
        target = tmp_path / "settings.json"
        install_cc_global_deny_settings(target)
        data = json.loads(target.read_text())
        assert "availableModels" not in data
        assert "model" not in data
        assert "permissions.deny" in data.get("_kiroclaw_managed", [])

    def test_does_not_touch_existing_user_model(self, tmp_path: Path):
        # An operator's explicit model is left exactly as-is (not removed, not
        # clobbered) — install writes only deny + marker.
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"model": "claude-opus-4-7", "availableModels": ["opus"]}))
        install_cc_global_deny_settings(target)
        data = json.loads(target.read_text())
        assert data["model"] == "claude-opus-4-7"
        assert data["availableModels"] == ["opus"]
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)

    def test_isolated_seed_layer_writes_model_allowlist(self):
        # The KiroClaw-OWNED isolated dir DOES get the full allowlist (1M ids)
        # + default model + deny + marker, so a spawn resolves 1M even without
        # per-session settings.local.json.
        from kiro_claw.cc_agent import _apply_deny_and_models_for_isolated

        data: dict = {}
        _apply_deny_and_models_for_isolated(data)
        assert data["availableModels"] == list(_CC_AVAILABLE_MODELS)
        assert "global.anthropic.claude-opus-4-8[1m]" in data["availableModels"]
        assert data["model"] == _CC_DEFAULT_MODEL
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)
        assert "permissions.deny" in data["_kiroclaw_managed"]


class TestCcConfigRoot:
    """cc_config_root() resolution: env override > isolation default > ~/.claude."""

    def test_claude_config_dir_override_wins(self, monkeypatch, tmp_path):
        from kiro_claw.cc_agent import cc_config_root

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "x"))
        assert cc_config_root() == tmp_path / "x"

    def test_isolation_disabled_falls_back_to_dot_claude(self, monkeypatch):
        from kiro_claw.cc_agent import cc_config_root

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("KIROCLAW_CC_ISOLATE", "0")
        assert cc_config_root() == Path.home() / ".claude"

    def test_isolation_default_uses_config_dir_cc_config(self, monkeypatch, tmp_path):
        from kiro_claw.cc_agent import cc_config_root

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("KIROCLAW_CC_ISOLATE", raising=False)
        monkeypatch.setenv("KIROCLAW_HOME", str(tmp_path / "mc"))
        root = cc_config_root()
        # config_dir() does .expanduser().resolve(), so on macOS the literal
        # tmp_path (/tmp/...) resolves through the /private symlink. Compare
        # against the resolved expected root so the hard == holds on both OSes.
        assert root == (tmp_path / "mc").resolve() / "cc-config"

    def test_isolation_enabled_flag_values(self, monkeypatch):
        from kiro_claw.cc_agent import cc_isolation_enabled

        for off in ("0", "false", "no", "FALSE", "No"):
            monkeypatch.setenv("KIROCLAW_CC_ISOLATE", off)
            assert cc_isolation_enabled() is False
        for on in ("1", "yes", "true", ""):
            monkeypatch.setenv("KIROCLAW_CC_ISOLATE", on)
            # empty string → falls through to default "1" semantics? No: explicit
            # empty is not in the off-set, so enabled.
            assert cc_isolation_enabled() is True
        monkeypatch.delenv("KIROCLAW_CC_ISOLATE", raising=False)
        assert cc_isolation_enabled() is True  # default ON


class TestSeedIsolatedCcConfig:
    """seed_isolated_cc_config: keep creds/models, strip plugins, never early-return."""

    def _seed(self, monkeypatch, tmp_path, user_settings: dict | None):
        """Run seed with a fake ~/.claude/settings.json source → isolated root."""
        from kiro_claw import cc_agent

        user_root = tmp_path / "user-claude"
        user_root.mkdir(parents=True)
        if user_settings is not None:
            (user_root / "settings.json").write_text(json.dumps(user_settings))
        # Redirect the seed SOURCE constant to our fake user file.
        monkeypatch.setattr(cc_agent, "_USER_CC_ROOT", user_root)
        iso_root = tmp_path / "cc-config"
        cc_agent.seed_isolated_cc_config(root=iso_root)
        return json.loads((iso_root / "settings.json").read_text())

    def test_keeps_creds_strips_plugins(self, monkeypatch, tmp_path):
        data = self._seed(
            monkeypatch,
            tmp_path,
            {
                "awsCredentialExport": "/bin/claude default-credential-export",
                "env": {"AWS_REGION": "us-west-2"},
                "model": "global.anthropic.claude-opus-4-8[1m]",
                "effortLevel": "xhigh",
                "enabledPlugins": {"AIPowerUserCapabilities-research@aim": True},
                "extraKnownMarketplaces": {"aim": {}},
                "theme": "dark",
                "permissions": {
                    "defaultMode": "dontAsk",
                    "allow": ["Read"],
                    "ask": ["Write(*)"],
                },
            },
        )
        # KEPT
        assert data["awsCredentialExport"] == "/bin/claude default-credential-export"
        assert data["env"] == {"AWS_REGION": "us-west-2"}
        assert data["model"] == "global.anthropic.claude-opus-4-8[1m]"
        # effortLevel is no longer in _CC_SEED_STRIP_KEYS — the user's configured
        # reasoning effort must survive into the isolated config (else effort
        # silently downgrades below the user's level when no override is set).
        assert data["effortLevel"] == "xhigh"
        # STRIPPED
        assert "enabledPlugins" not in data
        assert "extraKnownMarketplaces" not in data
        assert "theme" not in data
        # permissions.allow/ask/defaultMode are all stripped so EVERY tool routes
        # through the host canUseTool gate (see security regression test below).
        assert "defaultMode" not in data["permissions"]
        assert "allow" not in data["permissions"]
        assert "ask" not in data["permissions"]
        # LAYERED (deny + 1M allowlist)
        assert data["availableModels"] == list(_CC_AVAILABLE_MODELS)
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)

    def test_strips_allow_and_ask_so_host_gate_is_authoritative(self, monkeypatch, tmp_path):
        """Inherited permissions.allow/ask must NOT reach the isolated seed.

        CC's native permission engine auto-approves any tool matched by an
        ``allow`` (or, in non-interactive flows, ``ask``) entry WITHOUT calling
        the adapter's ``canUseTool`` — so an inherited ``allow`` wildcard like
        ``Bash(*)``/``Edit(*)`` would silently bypass KiroClaw's host-side
        deny/approve gate (``hooks.on_tool_call`` → ``reject_tool``). Stripping
        allow/ask keeps the host gate authoritative; only ``deny`` survives.
        """
        data = self._seed(
            monkeypatch,
            tmp_path,
            {
                "permissions": {
                    "allow": ["Bash(*)", "Edit(*)"],
                    "ask": ["Read(*)"],
                },
            },
        )
        assert "allow" not in data["permissions"]
        assert "ask" not in data["permissions"]
        # deny survives and carries the full KiroClaw pattern set.
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)
        # The de-Amazoned pattern set is non-empty (the literal count is
        # intentionally not hardcoded — it tracks _CC_DENY_PATTERNS).
        assert len(data["permissions"]["deny"]) == len(_CC_DENY_PATTERNS) > 0

    def test_seeded_settings_file_is_mode_0o600(self, monkeypatch, tmp_path):
        """The seeded file carries awsCredentialExport (a cred-refresh command),
        so it must be written 0o600 — never the 0o644 default that would leave
        it group/world-readable."""
        import os
        import stat

        from kiro_claw import cc_agent

        user_root = tmp_path / "user-claude"
        user_root.mkdir(parents=True)
        (user_root / "settings.json").write_text(
            json.dumps({"awsCredentialExport": "/bin/claude default-credential-export"})
        )
        monkeypatch.setattr(cc_agent, "_USER_CC_ROOT", user_root)
        iso_root = tmp_path / "cc-config"
        cc_agent.seed_isolated_cc_config(root=iso_root)
        seeded = iso_root / "settings.json"
        assert oct(stat.S_IMODE(os.stat(seeded).st_mode)) == "0o600"

    def test_data_loss_guard_skips_when_root_is_user_claude(self, monkeypatch, tmp_path):
        """If the isolation root resolves to the operator's real ~/.claude, the
        seed must SKIP entirely rather than strip+overwrite the genuine
        settings.json (which would destroy enabledPlugins etc.)."""
        from kiro_claw import cc_agent

        user_root = tmp_path / "user-claude"
        user_root.mkdir(parents=True)
        original = {
            "awsCredentialExport": "/bin/claude default-credential-export",
            "enabledPlugins": {"AIPowerUserCapabilities-research@aim": True},
            "theme": "dark",
        }
        (user_root / "settings.json").write_text(json.dumps(original))
        monkeypatch.setattr(cc_agent, "_USER_CC_ROOT", user_root)

        # root == the user ~/.claude → data-loss guard must early-return.
        cc_agent.seed_isolated_cc_config(root=user_root)

        after = json.loads((user_root / "settings.json").read_text())
        # File is UNCHANGED: plugins still present, nothing stripped or layered.
        assert after == original
        assert after["enabledPlugins"] == {"AIPowerUserCapabilities-research@aim": True}

    def test_seed_when_user_settings_absent(self, monkeypatch, tmp_path):
        data = self._seed(monkeypatch, tmp_path, None)
        # No creds to invent, but deny + models + default model still present.
        assert "awsCredentialExport" not in data
        assert data["availableModels"] == list(_CC_AVAILABLE_MODELS)
        assert data["model"] == _CC_DEFAULT_MODEL
        assert data["permissions"]["deny"] == list(_CC_DENY_PATTERNS)

    def test_idempotent_no_early_return_copies_creds(self, monkeypatch, tmp_path):
        # Simulate boot order: a pre-existing isolated settings.json (deny+models,
        # NO creds) written before any spawn seeds creds. Seeding must still copy
        # awsCredentialExport onto the existing file (never early-return).
        from kiro_claw import cc_agent

        user_root = tmp_path / "user-claude"
        user_root.mkdir(parents=True)
        (user_root / "settings.json").write_text(
            json.dumps({"awsCredentialExport": "/bin/claude default-credential-export"})
        )
        monkeypatch.setattr(cc_agent, "_USER_CC_ROOT", user_root)

        iso_root = tmp_path / "cc-config"
        iso_root.mkdir()
        # Pre-existing file as boot repair would write it — no creds.
        install_cc_global_deny_settings(iso_root / "settings.json")
        pre = json.loads((iso_root / "settings.json").read_text())
        assert "awsCredentialExport" not in pre

        cc_agent.seed_isolated_cc_config(root=iso_root)
        post = json.loads((iso_root / "settings.json").read_text())
        assert post["awsCredentialExport"] == "/bin/claude default-credential-export"


class TestRevertUserModelSettings:
    def test_revert_removes_kiroclaw_written_model_keys(self, tmp_path: Path):
        from kiro_claw.cc_agent import (
            _CC_AVAILABLE_MODELS,
            _CC_DEFAULT_MODEL,
            revert_user_model_settings,
        )

        target = tmp_path / "settings.json"
        target.write_text(
            json.dumps(
                {
                    "model": _CC_DEFAULT_MODEL,
                    "availableModels": list(_CC_AVAILABLE_MODELS),
                    "permissions": {"deny": ["Bash(x)"]},
                    "env": {"AWS_REGION": "us-west-2"},
                }
            )
        )
        changed = revert_user_model_settings(target_path=target, dry_run=False)
        data = json.loads(target.read_text())
        assert changed is True
        assert "availableModels" not in data  # KiroClaw-written -> removed
        assert "model" not in data  # equals default -> removed
        assert data["permissions"]["deny"]  # deny kept (security)
        assert data["env"]["AWS_REGION"] == "us-west-2"  # unrelated kept

    def test_revert_leaves_user_customized_values(self, tmp_path: Path):
        from kiro_claw.cc_agent import revert_user_model_settings

        target = tmp_path / "settings.json"
        target.write_text(
            json.dumps(
                {
                    "model": "my-custom-model",
                    "availableModels": ["my", "list"],
                }
            )
        )
        changed = revert_user_model_settings(target_path=target, dry_run=False)
        data = json.loads(target.read_text())
        assert changed is False
        assert data["model"] == "my-custom-model"
        assert data["availableModels"] == ["my", "list"]

    def test_revert_dry_run_does_not_write(self, tmp_path: Path):
        from kiro_claw.cc_agent import _CC_DEFAULT_MODEL, revert_user_model_settings

        target = tmp_path / "settings.json"
        original = json.dumps({"model": _CC_DEFAULT_MODEL})
        target.write_text(original)
        changed = revert_user_model_settings(target_path=target, dry_run=True)
        assert changed is True  # would change
        assert target.read_text() == original  # but did not write
