"""Tests for plugin admission control (kiro_claw.platform.admission)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from kiro_claw.platform import discovery as discovery_mod
from kiro_claw.platform.admission import (
    MODE_ENFORCE,
    MODE_OPEN,
    AdmissionPolicy,
    PluginManifest,
    evaluate_admission,
)
from kiro_claw.platform.discovery import PluginAdmissionError, discover_companion_context


class _FakeEntryPoint:
    """Stands in for an importlib.metadata.EntryPoint without a real dist.

    A captured manifest is returned by monkeypatching ``_read_plugin_manifest``;
    ``load`` returns a builder that yields a sentinel context.
    """

    def __init__(self, name="amazon", value="m:build", loaded=None):
        self.name = name
        self.value = value
        self.group = "kiroclaw.plugins"
        self._loaded = loaded

    def load(self):
        return self._loaded


def _signed(manifest: PluginManifest, secret: str) -> PluginManifest:
    sig = hmac.new(secret.encode(), manifest.signing_payload(), hashlib.sha256).hexdigest()
    return PluginManifest(
        name=manifest.name,
        publisher=manifest.publisher,
        version=manifest.version,
        capabilities=manifest.capabilities,
        signature=sig,
    )


@pytest.fixture
def patch_manifest(monkeypatch):
    """Helper to set the manifest evaluate_admission will read for an entry point."""

    def _set(manifest):
        monkeypatch.setattr(
            "kiro_claw.platform.admission._read_plugin_manifest",
            lambda ep: manifest,
        )

    return _set


class TestOpenPolicy:
    def test_open_admits_unsigned_plugin(self, patch_manifest):
        patch_manifest(None)  # no manifest needed in open mode
        ep = _FakeEntryPoint()
        decision = evaluate_admission(ep, AdmissionPolicy.open_default())
        assert decision.allowed
        assert "open" in decision.reason

    def test_open_still_honors_ban(self, patch_manifest):
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "banned" in decision.reason


class TestKillSwitch:
    def test_ban_wins_over_everything(self, patch_manifest):
        # Even a fully-signed, allowlisted plugin is rejected if banned.
        secret = "k"
        m = _signed(
            PluginManifest(name="amazon", publisher="p13n", version="1"),
            secret,
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE,
            require_signature=True,
            trust_keys={"p13n": secret},
            approved=["amazon"],
            banned=["amazon"],
        )
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "kill-switch" in decision.reason

    def test_ban_is_case_and_whitespace_insensitive(self, patch_manifest):
        # A ban must not be evadable by a name-case or trailing-whitespace
        # mismatch between the policy and the manifest/entry-point name.
        patch_manifest(PluginManifest(name="Amazon-Evil", publisher="x", version="1"))
        ep = _FakeEntryPoint(name="Amazon-Evil")
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon-evil "])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "banned" in decision.reason


class TestManifestParsing:
    def test_string_capability_value_is_not_exploded(self):
        # A capability value given as a string (not a list) must become a
        # single-element list, NOT be exploded into per-character entries by
        # ``list(v)`` — which would corrupt both the ceiling check and the
        # signed payload.
        m = PluginManifest.from_dict(
            {"name": "p", "capabilities": {"egress": "*.amazon.com"}}
        )
        assert m.capabilities["egress"] == ["*.amazon.com"]

    def test_non_list_non_str_capability_value_drops_to_empty(self):
        m = PluginManifest.from_dict({"name": "p", "capabilities": {"egress": 42}})
        assert m.capabilities["egress"] == []

    def test_policy_string_capability_ceiling_not_exploded(self):
        p = AdmissionPolicy.from_dict(
            {"mode": "enforce", "capability_ceiling": {"egress": "*.amazon.com"}}
        )
        assert p.capability_ceiling["egress"] == ["*.amazon.com"]


class TestAllowlist:
    def test_not_on_allowlist_rejected(self, patch_manifest):
        m = PluginManifest(name="rogue", publisher="x", version="1")
        patch_manifest(m)
        ep = _FakeEntryPoint(name="rogue")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "allowlist" in decision.reason

    def test_on_allowlist_admitted(self, patch_manifest):
        m = PluginManifest(name="amazon", publisher="p13n", version="1")
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert decision.allowed


class TestSignature:
    def test_valid_signature_admitted(self, patch_manifest):
        secret = "s3cret"
        m = _signed(PluginManifest(name="amazon", publisher="p13n", version="1"), secret)
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": secret}
        )
        assert evaluate_admission(ep, policy).allowed

    def test_unsigned_rejected_when_signature_required(self, patch_manifest):
        m = PluginManifest(name="amazon", publisher="p13n", version="1")  # no sig
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": "s3cret"}
        )
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "signature" in decision.reason

    def test_tampered_capabilities_invalidate_signature(self, patch_manifest):
        secret = "s3cret"
        signed = _signed(
            PluginManifest(
                name="amazon", publisher="p13n", version="1", capabilities={"egress": ["a"]}
            ),
            secret,
        )
        # attacker swaps capabilities but keeps the old signature
        tampered = PluginManifest(
            name="amazon",
            publisher="p13n",
            version="1",
            capabilities={"egress": ["evil.example"]},
            signature=signed.signature,
        )
        patch_manifest(tampered)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(
            mode=MODE_ENFORCE, require_signature=True, trust_keys={"p13n": secret}
        )
        assert not evaluate_admission(ep, policy).allowed


class TestCapabilityCeiling:
    def test_capability_over_ceiling_rejected(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.evil.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "egress" in decision.reason

    def test_capability_within_ceiling_admitted(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.amazon.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        assert evaluate_admission(ep, policy).allowed

    def test_capability_glob_ceiling_admits_concrete_value(self, patch_manifest):
        # A concrete host must be admitted when it matches a glob ceiling entry
        # (e.g. "api.amazon.com" under "*.amazon.com") — the ceiling uses
        # fnmatch semantics, matching the documented policy shape.
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["api.amazon.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*.amazon.com"]})
        assert evaluate_admission(ep, policy).allowed

    def test_unceilinged_capability_category_rejected(self, patch_manifest):
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"paths": ["~/.ssh"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, capability_ceiling={"egress": ["*"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "paths" in decision.reason

    def test_open_mode_still_enforces_capability_ceiling(self, patch_manifest):
        # A ceiling configured under an OPEN policy (no allowlist, no signature)
        # must still be enforced — the open-mode fast path must not bypass it.
        m = PluginManifest(
            name="amazon", publisher="p13n", version="1", capabilities={"egress": ["*.evil.com"]}
        )
        patch_manifest(m)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_OPEN, capability_ceiling={"egress": ["*.amazon.com"]})
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "egress" in decision.reason

    def test_open_mode_no_ceiling_still_fast_path_admits(self, patch_manifest):
        # Truly-open policy (no ceiling, no allowlist, no signature) still admits
        # without requiring a manifest.
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        decision = evaluate_admission(ep, AdmissionPolicy(mode=MODE_OPEN))
        assert decision.allowed


class TestEnforceRequiresManifest:
    def test_enforce_rejects_plugin_without_manifest(self, patch_manifest):
        patch_manifest(None)
        ep = _FakeEntryPoint(name="amazon")
        policy = AdmissionPolicy(mode=MODE_ENFORCE, approved=["amazon"])
        decision = evaluate_admission(ep, policy)
        assert not decision.allowed
        assert "manifest" in decision.reason


class TestPolicyLoading:
    def test_no_policy_is_open_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIROCLAW_ADMISSION_POLICY", raising=False)
        monkeypatch.setattr(
            "kiro_claw.platform.admission._POLICY_DEFAULT_PATH", tmp_path / "nope.json"
        )
        from kiro_claw.platform.admission import load_admission_policy

        policy = load_admission_policy()
        assert policy.mode == MODE_OPEN
        assert policy.approved is None

    def test_unreadable_policy_fails_closed(self, monkeypatch, tmp_path):
        bad = tmp_path / "admission_policy.json"
        bad.write_text("{ not valid json")
        monkeypatch.setenv("KIROCLAW_ADMISSION_POLICY", str(bad))
        from kiro_claw.platform.admission import load_admission_policy

        policy = load_admission_policy()
        # fail-closed: enforce + signature + empty allowlist (admits nothing)
        assert policy.mode == MODE_ENFORCE
        assert policy.require_signature
        assert policy.approved == []

    def test_policy_round_trip(self, monkeypatch, tmp_path):
        p = tmp_path / "admission_policy.json"
        p.write_text(
            json.dumps(
                {
                    "mode": "enforce",
                    "require_signature": True,
                    "trust_keys": {"p13n": "s"},
                    "approved": ["amazon"],
                    "banned": ["rogue"],
                    "capability_ceiling": {"egress": ["*.amazon.com"]},
                }
            )
        )
        monkeypatch.setenv("KIROCLAW_ADMISSION_POLICY", str(p))
        from kiro_claw.platform.admission import load_admission_policy

        policy = load_admission_policy()
        assert policy.mode == MODE_ENFORCE
        assert policy.banned == ["rogue"]
        assert policy.approved == ["amazon"]


class TestDiscoveryGate:
    def test_rejected_plugin_aborts_discovery(self, monkeypatch):
        # A banned plugin must raise PluginAdmissionError BEFORE ep.load() runs.
        loaded_marker = {"called": False}

        def _should_not_run(_cfg):
            loaded_marker["called"] = True
            raise AssertionError("ep.load() ran for a rejected plugin")

        ep = _FakeEntryPoint(name="amazon", loaded=_should_not_run)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(
            "kiro_claw.platform.admission._read_plugin_manifest",
            lambda e: PluginManifest(name="amazon", publisher="p13n", version="1"),
        )
        policy = AdmissionPolicy(mode=MODE_OPEN, banned=["amazon"])
        with pytest.raises(PluginAdmissionError):
            discover_companion_context("amazon", None, policy=policy)
        assert loaded_marker["called"] is False  # verify-before-run held

    def test_admitted_plugin_loads(self, monkeypatch):
        sentinel = object()
        ep = _FakeEntryPoint(name="amazon", loaded=lambda _cfg: sentinel)
        monkeypatch.setattr(discovery_mod, "plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(
            "kiro_claw.platform.admission._read_plugin_manifest",
            lambda e: PluginManifest(name="amazon", publisher="p13n", version="1"),
        )
        policy = AdmissionPolicy(mode=MODE_OPEN)
        result = discover_companion_context("amazon", None, policy=policy)
        assert result is sentinel
