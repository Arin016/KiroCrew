"""Tests for the canonical model registry reader."""

from __future__ import annotations

from kiro_claw import model_registry as mr


class TestModelRegistry:
    def test_to_provider_id_canonical_key(self):
        assert (
            mr.to_provider_id("opus-4.8-1m", "claude_code")
            == "global.anthropic.claude-opus-4-8[1m]"
        )

    def test_to_provider_id_identity_passthrough_for_provider_id(self):
        # An already-resolved provider id passes through unchanged (back-compat).
        pid = "global.anthropic.claude-opus-4-8[1m]"
        assert mr.to_provider_id(pid, "claude_code") == pid

    def test_to_provider_id_unknown_passes_through_unchanged(self):
        # An unrecognized value (real-but-unregistered Bedrock id, regional
        # profile, or future model) is passed through UNCHANGED — we never
        # silently rewrite an operator's explicit id to the flagship default.
        assert (
            mr.to_provider_id("us.anthropic.claude-opus-4-8[1m]", "claude_code")
            == "us.anthropic.claude-opus-4-8[1m]"
        )
        assert mr.to_provider_id("nonexistent-model", "claude_code") == "nonexistent-model"

    def test_corrupt_registry_default_translates_to_valid_provider_id(self, monkeypatch):
        # If model_registry.json is corrupt/missing, _REGISTRY is empty and the
        # indices resolve nothing — but the default()->to_provider_id chain must
        # STILL yield a valid Bedrock id, not the bare canonical key (which the
        # adapter/Bedrock would reject with -32603/400). This is the end-to-end
        # "a corrupt registry can't brick the provider" guarantee.
        monkeypatch.setattr(mr, "_REGISTRY", {}, raising=True)
        monkeypatch.setattr(mr, "_CANONICAL_INDEX", {}, raising=True)
        monkeypatch.setattr(mr, "_DEFAULTS", {}, raising=True)
        canonical = mr.default("claude_code")
        assert canonical == mr._FALLBACK_CANONICAL  # the bare key
        # The fallback key must translate to the paired VALID provider id.
        assert mr.to_provider_id(canonical, "claude_code") == mr._FALLBACK_PROVIDER_ID
        assert mr.to_provider_id(canonical, "claude_code") == (
            "global.anthropic.claude-opus-4-8[1m]"
        )

    def test_from_provider_id_empty_returns_empty_not_auto(self):
        # Empty means "no model", NOT the 'auto' canonical key.
        assert mr.from_provider_id("", "claude_code") == ""

    def test_window_unlisted_1m_id_heuristic(self):
        # Parity with the frontend: an unlisted [1m]/-1m id still gets 1M.
        assert mr.window("global.anthropic.claude-opus-9-9[1m]") == 1_000_000
        assert mr.window("claude-future-1m") == 1_000_000
        assert mr.window("something-else") == 200_000

    def test_supports_effort_from_registry(self):
        assert mr.supports_effort("opus-4.8-1m") is True
        # auto entry has no supports_effort -> None (caller falls back).
        assert mr.supports_effort("auto") is None
        # unknown -> None
        assert mr.supports_effort("nonexistent") is None

    def test_kiro_dotted_aliases_resolve(self):
        # AIM-managed agents ship kiro dotted ids; they must map deterministically
        # (NOT fall back to the flagship), preserving e.g. meshclaw-lite on sonnet.
        assert (
            mr.to_provider_id("claude-sonnet-4.6", "claude_code")
            == "global.anthropic.claude-sonnet-4-6[1m]"
        )
        assert (
            mr.to_provider_id("claude-opus-4.7", "claude_code")
            == "global.anthropic.claude-opus-4-7[1m]"
        )
        # Opus 4.6 has no Bedrock profile; alias collapses to the current flagship.
        assert (
            mr.to_provider_id("claude-opus-4.6", "claude_code")
            == "global.anthropic.claude-opus-4-8[1m]"
        )
        # bare 'opus'/'sonnet' aliases
        assert mr.to_provider_id("opus", "claude_code") == "global.anthropic.claude-opus-4-8[1m]"
        assert (
            mr.to_provider_id("sonnet", "claude_code") == "global.anthropic.claude-sonnet-4-6[1m]"
        )

    def test_legacy_dotted_ids_do_not_regress_to_flagship(self):
        # Models the OLD _CC_MODEL_ALIASES mapped to cheaper classes must NOT
        # silently resolve to the flagship Opus 4.8 1M (a cost regression).
        flagship = "global.anthropic.claude-opus-4-8[1m]"
        sonnet = "global.anthropic.claude-sonnet-4-6[1m]"
        # Sonnet/Haiku-class ids route to Sonnet (cheapest available), not Opus.
        for sid in (
            "claude-sonnet-4.5",
            "claude-sonnet-4.5-1m",
            "claude-sonnet-4",
            "claude-haiku-4.5",
        ):
            assert mr.to_provider_id(sid, "claude_code") == sonnet, sid
        # Opus 4.5 routes to the 200K Opus, not the 1M flagship.
        assert (
            mr.to_provider_id("claude-opus-4.5", "claude_code")
            == "global.anthropic.claude-opus-4-8"
        )
        # The -1m form of 4.6 no longer downgrades to 4.7; it maps to the flagship.
        assert mr.to_provider_id("claude-opus-4.6-1m", "claude_code") == flagship

    def test_auto_passes_through_empty(self):
        assert mr.to_provider_id("auto", "claude_code") == ""

    def test_window_by_canonical(self):
        assert mr.window("opus-4.8-1m") == 1_000_000
        assert mr.window("opus-4.8") == 200_000

    def test_window_by_provider_id(self):
        assert mr.window("global.anthropic.claude-opus-4-8[1m]") == 1_000_000

    def test_available_models_returns_provider_ids(self):
        ids = mr.available_models("claude_code")
        assert "global.anthropic.claude-opus-4-8[1m]" in ids
        assert "global.anthropic.claude-sonnet-4-6[1m]" in ids
        # 'auto' maps to "" and is excluded from the allowlist.
        assert "" not in ids

    def test_default_canonical(self):
        assert mr.default("claude_code") == "opus-4.8-1m"

    def test_from_provider_id_reverse_lookup(self):
        assert (
            mr.from_provider_id("global.anthropic.claude-opus-4-8[1m]", "claude_code")
            == "opus-4.8-1m"
        )

    def test_display_list_shape(self):
        rows = mr.display_list("claude_code")
        assert {"model_name", "display_name", "description"} <= set(rows[0])
        # default first
        assert rows[0]["model_name"] == "opus-4.8-1m"


class TestClaudeCodeConstantsBackedByRegistry:
    def test_default_model_matches_registry(self):
        from kiro_claw.providers.claude_code import _CC_DEFAULT_MODEL

        assert _CC_DEFAULT_MODEL == mr.to_provider_id(mr.default("claude_code"), "claude_code")

    def test_cc_agent_constants_do_not_drift_from_registry(self):
        # cc_agent._CC_AVAILABLE_MODELS / _CC_DEFAULT_MODEL seed the isolated dir
        # and are the secondary fallback in _write_claude_local_settings. They
        # are an independent copy of the registry data; guard against silent
        # desync on a future registry edit.
        from kiro_claw.cc_agent import _CC_AVAILABLE_MODELS, _CC_DEFAULT_MODEL

        assert list(_CC_AVAILABLE_MODELS) == mr.available_models("claude_code")
        assert _CC_DEFAULT_MODEL == mr.to_provider_id(mr.default("claude_code"), "claude_code")
