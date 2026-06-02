"""Tests for the shared reasoning-effort vocabulary (effort.py) and the
ACP provider cli.json overlay helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_claw.config.loader import KiroClawConfig
from kiro_claw.effort import (
    EFFORT_LEVELS,
    EFFORT_VALUES,
    is_valid_effort,
    model_supports_effort,
    resolve_effort_for_model,
)
from kiro_claw.providers.acp import (
    _clear_cli_overlay_effort,
    _read_cli_overlay,
    _write_cli_overlay,
)


class TestEffortVocabulary:
    def test_levels_include_xhigh_ordered(self):
        assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    def test_values_add_empty_sentinel(self):
        assert EFFORT_VALUES == frozenset({"", "low", "medium", "high", "xhigh", "max"})

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_is_valid_effort_true(self, level: str):
        assert is_valid_effort(level)

    @pytest.mark.parametrize("bad", ["", "LOW", "ultra", " low", 5, None, ["max"]])
    def test_is_valid_effort_false(self, bad: object):
        assert not is_valid_effort(bad)


class TestModelSupportsEffort:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4.7",
            "claude-sonnet-4.6",
            "global.anthropic.claude-opus-4-8[1m]",
            "anthropic.claude-sonnet-4-20250514-v1:0",
        ],
    )
    def test_opus_sonnet_supported(self, model: str):
        assert model_supports_effort(model)

    @pytest.mark.parametrize(
        "model",
        [None, "", "auto", "claude-haiku-4.5", "amazon.nova-pro-v1:0", "deepseek-3.2"],
    )
    def test_unsupported(self, model: str | None):
        assert not model_supports_effort(model)


class TestResolveEffortForModel:
    def test_slot_override_wins(self):
        assert (
            resolve_effort_for_model(
                "claude-opus-4.7",
                slot_overrides={"claude-opus-4.7": "low"},
                defaults={"claude-opus-4.7": "max"},
            )
            == "low"
        )

    def test_falls_back_to_defaults(self):
        assert (
            resolve_effort_for_model(
                "claude-opus-4.7", defaults={"claude-opus-4.7": "high"}
            )
            == "high"
        )

    def test_defaults_accept_json_string(self):
        # Frontend setVariable only stores strings, so defaults may arrive
        # JSON-encoded.
        assert (
            resolve_effort_for_model(
                "claude-opus-4.7", defaults='{"claude-opus-4.7": "xhigh"}'
            )
            == "xhigh"
        )

    def test_none_when_model_incapable(self):
        assert resolve_effort_for_model("claude-haiku-4.5", slot_overrides={"claude-haiku-4.5": "max"}) is None

    def test_none_when_no_level(self):
        assert resolve_effort_for_model("claude-opus-4.7") is None

    def test_malformed_defaults_ignored(self):
        assert resolve_effort_for_model("claude-opus-4.7", defaults="not json") is None
        assert resolve_effort_for_model("claude-opus-4.7", defaults=12345) is None


class TestCliOverlay:
    def test_write_then_read_roundtrip(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "xhigh")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "xhigh"}
        # Verify on-disk shape matches kiro-cli's expected format.
        cli = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli.read_text())
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "xhigh"

    def test_write_merges_preserves_other_keys(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text(
            json.dumps({"chat.enableNotifications": True,
                        "chat.modelDefaults": {"claude-opus-4.6": {"output_config": {"effort": "high"}}}})
        )
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        data = json.loads((settings_dir / "cli.json").read_text())
        # Existing unrelated setting preserved.
        assert data["chat.enableNotifications"] is True
        # Both models present.
        assert data["chat.modelDefaults"]["claude-opus-4.6"]["output_config"]["effort"] == "high"
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "max"

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert _read_cli_overlay(tmp_path) == {}

    def test_read_malformed_returns_empty(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text("{ not json")
        assert _read_cli_overlay(tmp_path) == {}

    def test_clear_removes_only_target_model(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        _write_cli_overlay(tmp_path, "claude-opus-4.6", "high")
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.6": "high"}

    def test_clear_missing_file_noop(self, tmp_path):
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")  # must not raise
        assert _read_cli_overlay(tmp_path) == {}


class TestFactoryEffortThreading:
    """The provider factory must thread the slot's reasoning_effort_override
    into effort_per_model for BOTH ACP backends — otherwise a cold start
    (or the handler's reset-then-respawn) never applies the persisted effort."""

    def _capture_provider_kwargs(self, provider_name: str, **factory_call):
        # Both factory branches lazily `from kiro_claw.providers.acp import
        # AcpProvider` (circular-import workaround). That import runs inside
        # create_provider_factory(), so patch the source module symbol BEFORE
        # building the factory, then capture the construction kwargs.
        cfg = KiroClawConfig()
        cfg.agent.provider = provider_name
        with patch("kiro_claw.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    @pytest.mark.parametrize("provider_name", ["acp", "claude_code"])
    def test_valid_effort_on_opus_threads_per_model(self, provider_name):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="xhigh",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "xhigh"}

    @pytest.mark.parametrize("provider_name", ["acp", "claude_code"])
    def test_effort_on_incapable_model_not_threaded(self, provider_name):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-haiku-4.5",
            reasoning_effort_override="high",
        )
        assert kwargs.get("effort_per_model") == {}

    @pytest.mark.parametrize("provider_name", ["acp", "claude_code"])
    def test_invalid_effort_not_threaded(self, provider_name):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="ultra",
        )
        assert kwargs.get("effort_per_model") == {}

    def test_claude_code_drops_dead_effort_env_var(self):
        # CLAUDE_CODE_EFFORT_LEVEL is not read by claude-agent-acp; it must
        # not be emitted (effort is applied live instead).
        kwargs = self._capture_provider_kwargs(
            "claude_code",
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="max",
        )
        assert "CLAUDE_CODE_EFFORT_LEVEL" not in (kwargs.get("extra_env") or {})


class TestFactoryCcConfigDirInjection:
    """The claude_code factory must isolate the spawned subprocess: inject
    CLAUDE_CONFIG_DIR into the provider's extra_env (= cc_env) pointing at the
    KiroClaw-seeded dir, and seed that EXACT dir before spawn so the child reads
    creds/models/deny at startup."""

    def test_injects_config_dir_and_runs_seed(self, monkeypatch, tmp_path):
        from kiro_claw import cc_agent

        iso_root = tmp_path / "mc" / "cc-config"
        seeded_with: list[object] = []

        def _fake_seed(root=None):
            seeded_with.append(root)
            return root

        # Force isolation ON and pin the root + seed to a tmp dir so the factory's
        # real cc_config_root()/seed don't touch the host ~/.claude.
        monkeypatch.setenv("KIROCLAW_CC_ISOLATE", "1")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(cc_agent, "cc_config_root", lambda: iso_root)
        monkeypatch.setattr(cc_agent, "seed_isolated_cc_config", _fake_seed)

        cfg = KiroClawConfig()
        cfg.agent.provider = "claude_code"
        with patch("kiro_claw.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(session_key="dash:1", agent="kiroclaw")
            kwargs = mock_provider.call_args.kwargs

        # The provider's extra_env (cc_env) carries CLAUDE_CONFIG_DIR = the root.
        extra_env = kwargs.get("extra_env") or {}
        assert extra_env.get("CLAUDE_CONFIG_DIR") == str(iso_root)
        # The seed ran against the SAME dir the child will read (derived from the
        # post-merge cc_env value, not re-derived).
        assert seeded_with == [iso_root]

    def test_caller_extra_env_override_seeds_overridden_dir(self, monkeypatch, tmp_path):
        # A caller-supplied CLAUDE_CONFIG_DIR in extra_env must win, and the seed
        # must target THAT dir (not the default cc_config_root) so the child does
        # not read an unseeded dir.
        from kiro_claw import cc_agent

        default_root = tmp_path / "default-cc"
        override_root = tmp_path / "override-cc"
        seeded_with: list[object] = []

        monkeypatch.setenv("KIROCLAW_CC_ISOLATE", "1")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(cc_agent, "cc_config_root", lambda: default_root)
        monkeypatch.setattr(
            cc_agent, "seed_isolated_cc_config", lambda root=None: seeded_with.append(root)
        )

        cfg = KiroClawConfig()
        cfg.agent.provider = "claude_code"
        with patch("kiro_claw.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                session_key="dash:1",
                agent="kiroclaw",
                extra_env={"CLAUDE_CONFIG_DIR": str(override_root)},
            )
            kwargs = mock_provider.call_args.kwargs

        assert (kwargs.get("extra_env") or {}).get("CLAUDE_CONFIG_DIR") == str(override_root)
        # Seeded the overridden dir, not the default.
        assert seeded_with == [override_root]


class TestFactoryPerAgentCcModel:
    """Under claude_code, a non-default agent (e.g. kiroclaw-lite for cheap
    background work) must run on its own resolved CC model, not the global
    Opus 4.8 cc_model — the claude backend can't pick up a per-agent model via
    --agent/set_mode the way kiro-cli does, so the factory resolves it."""

    def _capture(self, **factory_call) -> dict:
        cfg = KiroClawConfig()
        cfg.agent.provider = "claude_code"
        with patch("kiro_claw.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called
            return dict(mock_provider.call_args.kwargs)

    def test_lite_agent_resolves_own_cc_model(self):
        with patch.object(KiroClawConfig, "_resolve_agent_cc_model", return_value="claude-sonnet-4.6"):
            kwargs = self._capture(session_key="bg", agent="kiroclaw-lite")
        assert kwargs.get("model") == "claude-sonnet-4.6"
        assert kwargs.get("model") != "global.anthropic.claude-opus-4-8[1m]"

    def test_default_agent_keeps_global_cc_model(self):
        cfg = KiroClawConfig()
        expected = cfg.agent.cc_model
        for ag in ("kiroclaw", None):
            with patch.object(KiroClawConfig, "_resolve_agent_cc_model") as resolver:
                kwargs = self._capture(session_key="dash:1", agent=ag)
                assert kwargs.get("model") == expected
                resolver.assert_not_called()  # default agent never consults per-agent resolver

    def test_model_override_wins_for_custom_agent(self):
        with patch.object(KiroClawConfig, "_resolve_agent_cc_model", return_value="claude-sonnet-4.6"):
            kwargs = self._capture(
                session_key="bg", agent="kiroclaw-lite", model_override="claude-opus-4.7"
            )
        assert kwargs.get("model") == "claude-opus-4.7"

    def test_lite_falls_back_to_global_when_no_per_agent_model(self):
        cfg = KiroClawConfig()
        expected = cfg.agent.cc_model
        with patch.object(KiroClawConfig, "_resolve_agent_cc_model", return_value=""):
            kwargs = self._capture(session_key="bg", agent="kiroclaw-lite")
        assert kwargs.get("model") == expected

    def test_empty_cc_model_resolves_to_default_not_blank(self):
        # A user whose persisted config has an empty cc_model must NOT get a
        # blank model passed to the adapter (which then falls back to its own
        # models[0] — an OLD Opus 4.1). It must resolve to _CC_DEFAULT_MODEL.
        from kiro_claw.providers.claude_code import _CC_DEFAULT_MODEL

        cfg = KiroClawConfig()
        cfg.agent.provider = "claude_code"
        cfg.agent.cc_model = ""  # explicit empty (the bug's trigger)
        with patch("kiro_claw.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(session_key="dash:1", agent="kiroclaw")
            kwargs = mock_provider.call_args.kwargs
        assert kwargs.get("model") == _CC_DEFAULT_MODEL
        assert kwargs.get("model")  # never blank


class TestResolveAgentCcModel:
    """_resolve_agent_cc_model reads a custom agent's CC model from its kiro json."""

    def test_prefers_cc_model_then_model(self, tmp_path, monkeypatch):
        # cc_model wins over model; a kiro dotted id is translated to a
        # CC-valid alias (claude-sonnet-4.6 → sonnet) so the backend accepts it.
        import kiro_claw.agent as agent_mod
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        (tmp_path / "a.json").write_text(
            json.dumps({"name": "lite-x", "model": "claude-opus-4.6", "cc_model": "claude-sonnet-4.6"})
        )
        assert KiroClawConfig._resolve_agent_cc_model("lite-x") == "sonnet"

    def test_falls_back_to_model_translated_when_no_cc_model(self, tmp_path, monkeypatch):
        # The AIM-managed kiroclaw-lite agent ships only `model: claude-opus-4.6`
        # (no cc_model). That kiro dotted id is NOT a valid claude-agent-acp
        # model — sent verbatim the backend rejects the session with -32603.
        # Translate the fallback through _CC_MODEL_ALIASES so it resolves to the
        # CC-valid `opus` alias instead.
        import kiro_claw.agent as agent_mod
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        (tmp_path / "b.json").write_text(json.dumps({"name": "lite-y", "model": "claude-opus-4.6"}))
        assert KiroClawConfig._resolve_agent_cc_model("lite-y") == "opus"

    def test_cc_valid_id_passes_through_untranslated(self, tmp_path, monkeypatch):
        # A model id that is already CC-valid (full Bedrock inference profile) is
        # not a key in the alias map and must pass through unchanged.
        import kiro_claw.agent as agent_mod
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        (tmp_path / "c.json").write_text(
            json.dumps({"name": "lite-z", "cc_model": "global.anthropic.claude-sonnet-4-6[1m]"})
        )
        assert (
            KiroClawConfig._resolve_agent_cc_model("lite-z")
            == "global.anthropic.claude-sonnet-4-6[1m]"
        )

    def test_empty_when_agent_not_found(self, tmp_path, monkeypatch):
        import kiro_claw.agent as agent_mod
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        assert KiroClawConfig._resolve_agent_cc_model("nope") == ""
