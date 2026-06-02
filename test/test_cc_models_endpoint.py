"""Tests for the claude_code model list assembled by /api/models.

The dropdown leads with the KiroClaw-curated set (Opus 4.8 1M/200k, Opus 4.7,
Sonnet 4.6, Haiku 4.5) so users always see clean, current defaults ahead of
whatever the adapter advertises (its set can be stale — Opus 4.1, Sonnet 4.5);
adapter extras are appended de-duped, plus the configured default.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_claw.dashboard.handlers.agents import (
    _CC_CURATED_MODELS,
    _advertised_cc_models,
    _cc_models,
    _normalize_model_key,
)


def _request_with_providers(providers: dict) -> MagicMock:
    """Fake aiohttp request whose sessions.active_providers() yields `providers`.

    Mirrors the real SessionManager API (active_providers()) so the test can't
    pass against an attribute the production object doesn't have.
    """
    sessions = SimpleNamespace(active_providers=lambda: list(providers.values()))
    state = SimpleNamespace(sessions=sessions)
    req = MagicMock()
    req.app.__getitem__.return_value = state
    return req


class _FakeProvider:
    def __init__(self, models):
        self._models = models

    def available_models(self):
        return self._models


class TestAdvertisedCcModels:
    def test_maps_modelid_name_description(self):
        prov = _FakeProvider([
            {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6", "description": "Everyday"},
        ])
        out = _advertised_cc_models(_request_with_providers({"s": prov}))
        assert out == [
            {
                "model_name": "claude-sonnet-4-6",
                "display_name": "Sonnet 4.6",
                "description": "Everyday",
            }
        ]

    def test_empty_when_no_active_sessions(self):
        assert _advertised_cc_models(_request_with_providers({})) == []

    def test_skips_provider_without_accessor(self):
        out = _advertised_cc_models(_request_with_providers({"s": object()}))
        assert out == []


class TestCcModelsMerge:
    def test_curated_set_always_present_even_without_session(self):
        # No live provider → falls back to static catalog, but the full curated
        # set still leads the list.
        out = _cc_models(_request_with_providers({}))
        names = [m["model_name"] for m in out]
        assert "global.anthropic.claude-opus-4-8[1m]" in names
        assert "global.anthropic.claude-opus-4-8" in names
        # The curated set comes first, in order, ahead of any fallback.
        curated_names = [m["model_name"] for m in _CC_CURATED_MODELS]
        assert names[: len(curated_names)] == curated_names
        assert names[0] == "global.anthropic.claude-opus-4-8[1m]"

    def test_curated_leads_then_adapter_extras_appended(self):
        # Adapter advertises a stale set (Opus 4.1, Sonnet 4.5). Curated leads;
        # adapter-only extras are appended after, never displacing the defaults.
        prov = _FakeProvider([
            {"modelId": "claude-opus-4-1", "name": "Opus 4.1", "description": ""},
            {"modelId": "claude-sonnet-4-5", "name": "Sonnet 4.5", "description": ""},
        ])
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        curated_names = [m["model_name"] for m in _CC_CURATED_MODELS]
        # Curated set leads in order.
        assert names[: len(curated_names)] == curated_names
        # Adapter extras present but appended after the curated set.
        assert "claude-opus-4-1" in names
        assert "claude-sonnet-4-5" in names
        assert names.index("claude-opus-4-1") >= len(curated_names)

    def test_no_duplicate_when_adapter_already_lists_curated_model(self):
        # The adapter advertises the SAME prefixed ids the curated set uses
        # (because they are in availableModels) — they must collapse to one row,
        # with the curated entry (friendly display_name) winning.
        prov = _FakeProvider([
            {"modelId": "global.anthropic.claude-sonnet-4-6[1m]", "name": "Sonnet 4.6", "description": ""},
            {"modelId": "global.anthropic.claude-opus-4-8[1m]", "name": "Opus 4.8", "description": ""},
        ])
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names.count("global.anthropic.claude-opus-4-8[1m]") == 1
        assert names.count("global.anthropic.claude-sonnet-4-6[1m]") == 1

    def test_spelling_variant_ids_collapse_to_one_row(self):
        # Defensive: if the adapter ever advertises a case/dot variant of a
        # curated id, normalization must still collapse it to a single row.
        # Regression for the duplicate rows seen in the picker.
        prov = _FakeProvider([
            {"modelId": "GLOBAL.ANTHROPIC.CLAUDE-OPUS-4-8[1M]", "name": "Opus 4.8 (dup)", "description": ""},
            {"modelId": "default", "name": "Default", "description": ""},
        ])
        out = _cc_models(_request_with_providers({"s": prov}))
        keys = [_normalize_model_key(m["model_name"]) for m in out]
        # The case-variant 4.8 id collapses with the curated one.
        assert keys.count(_normalize_model_key("global.anthropic.claude-opus-4-8[1m]")) == 1
        # The curated row wins (carries the friendly display_name).
        opus48 = next(
            m for m in out
            if _normalize_model_key(m["model_name"])
            == _normalize_model_key("global.anthropic.claude-opus-4-8[1m]")
        )
        assert opus48["display_name"] == "Opus 4.8 (1M context)"

    def test_default_and_auto_aliases_collapse(self):
        # Adapter's "default" and a curated/fallback "auto" both mean
        # let-the-backend-pick; only one should surface.
        prov = _FakeProvider([
            {"modelId": "default", "name": "Default", "description": ""},
            {"modelId": "auto", "name": "Auto", "description": ""},
        ])
        out = _cc_models(_request_with_providers({"s": prov}))
        alias_rows = [m for m in out if m["model_name"].lower() in ("default", "auto")]
        assert len(alias_rows) == 1

    def test_configured_default_force_included(self):
        out = _cc_models(_request_with_providers({}), configured_default="custom-model-xyz")
        names = [m["model_name"] for m in out]
        assert "custom-model-xyz" in names

    def test_configured_default_not_duplicated_if_already_present(self):
        out = _cc_models(
            _request_with_providers({}),
            configured_default="global.anthropic.claude-opus-4-8[1m]",
        )
        names = [m["model_name"] for m in out]
        assert names.count("global.anthropic.claude-opus-4-8[1m]") == 1
