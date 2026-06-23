"""Phase 2 — the governance ceiling composes onto PlatformContext at boot.

Confirms: ``build_default_context`` carries the loaded ceiling on every path
(boot + lazy default); a present env policy flows through; an unreadable policy
aborts; absent → ``governance=None``; CONTRACT_VERSION is bumped to 2.
"""

from __future__ import annotations

import json

import pytest

from kiro_claw.config.loader import KiroClawConfig
from kiro_claw.platform.bootstrap import build_default_context
from kiro_claw.platform.context import CONTRACT_VERSION, PlatformCompositionError


def test_contract_version_is_two():
    assert CONTRACT_VERSION == 2


def test_standalone_no_policy_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("KIROCLAW_SECURITY_POLICY", raising=False)
    monkeypatch.setattr("kiro_claw.platform.governance._POLICY_HOME_PATH", tmp_path / "nope.json")
    ctx = build_default_context(KiroClawConfig.load())
    assert ctx.governance is None
    assert ctx.contract_version == 2


def test_env_policy_composes_onto_context(monkeypatch, tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "commands": {"mode": "deny", "deny": ["git push*"]},
            }
        )
    )
    monkeypatch.setenv("KIROCLAW_SECURITY_POLICY", str(p))
    ctx = build_default_context(KiroClawConfig.load())
    assert ctx.governance is not None
    assert "commands" in ctx.governance.controls


def test_unreadable_policy_aborts_boot(monkeypatch, tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{ not json")
    monkeypatch.setenv("KIROCLAW_SECURITY_POLICY", str(bad))
    with pytest.raises(PlatformCompositionError):
        build_default_context(KiroClawConfig.load())


def test_looser_profile_ordinal_aborts_boot(monkeypatch, tmp_path):
    """Validation rules 3 & 7: a profile looser than the ceiling aborts boot."""
    from kiro_claw.platform import governance_profiles as gp

    # Ceiling requires sandbox >= cc; a profile that sets sandbox off is looser.
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "weak.json").write_text(
        json.dumps(
            {
                "name": "weak",
                "bind": {"type": "surface", "id": "cron"},
                "sandbox": {"min_level": "off"},
            }
        )
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps({"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "cc"}})
    )
    monkeypatch.setenv("KIROCLAW_SECURITY_POLICY", str(p))
    try:
        from kiro_claw.platform import bootstrap

        with pytest.raises(PlatformCompositionError):
            bootstrap.bootstrap_context(KiroClawConfig.load())
    finally:
        gp.reset_store()


def test_profile_within_ceiling_boots(monkeypatch, tmp_path):
    from kiro_claw.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "ok.json").write_text(
        json.dumps(
            {
                "name": "ok",
                "bind": {"type": "surface", "id": "cron"},
                "sandbox": {"min_level": "strict"},  # stricter than ceiling cc → fine
            }
        )
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    p = tmp_path / "policy.json"
    p.write_text(
        json.dumps({"version": 1, "boot": {"fail_closed": True}, "sandbox": {"min_level": "cc"}})
    )
    monkeypatch.setenv("KIROCLAW_SECURITY_POLICY", str(p))
    try:
        from kiro_claw.platform import bootstrap

        bootstrap.bootstrap_context(KiroClawConfig.load())  # no raise
    finally:
        gp.reset_store()


def test_lazy_default_carries_ceiling(monkeypatch, tmp_path):
    """The lazy current_context() path (stray subprocess) also gets the ceiling."""
    from kiro_claw.platform import context as ctx_mod

    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"version": 1, "boot": {"fail_closed": True}}))
    monkeypatch.setenv("KIROCLAW_SECURITY_POLICY", str(p))
    monkeypatch.delenv("KIROCLAW_PROFILE", raising=False)
    ctx_mod.reset_context()
    try:
        ctx = ctx_mod.current_context()
        assert ctx.governance is not None
    finally:
        ctx_mod.reset_context()
