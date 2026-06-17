"""Plugin admission control — the gate that decides whether a plugin may load.

This is the policy layer on top of the structural discovery gates
(fail-closed discovery, contract-version, ADD-only floor). It lets a managed
fleet **reject or ban** a plugin before its code is ever imported, using a
defense-in-depth model:

1. **Kill-switch (`banned`)** — a fleet can ban a plugin by name; the ban always
   wins, even in an otherwise-open policy. This is the R-08 / M-09 remote-disable
   control.
2. **Marketplace allowlist (`approved`)** — when the policy carries a non-empty
   allowlist, only listed plugins are admitted. This is the marketplace review
   gate: a plugin is admitted only after it has been reviewed and added.
3. **Verify-before-run signature (`require_signature`)** — the plugin ships a
   signed manifest; admission verifies the signature against a trust key the
   *policy* (not the plugin) carries, before `ep.load()`. This is the R-11 /
   M-12 supply-chain control.
4. **Capability ceiling** — the manifest declares the capabilities the plugin
   requests (tools, network egress, credential paths). Admission rejects a
   plugin whose declared capabilities exceed the policy ceiling.

Trust-root invariant: the admission policy is loaded from a **fleet-controlled
source** (`KIROCLAW_ADMISSION_POLICY` env path, or
`~/.kiroclaw/admission_policy.json`), never from the plugin being admitted, so a
plugin cannot approve or un-ban itself. The public edition ships **no** policy →
default-open (admit), preserving today's behavior; a managed fleet ships a policy
and the gate enforces.

The plugin manifest is read **without importing the plugin module** (from the
installed distribution's files), so a malicious plugin's code never runs before
the admission decision.

See ``docs/system-specs/modules/platform-context.md`` (Plugin admission).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import importlib.metadata as _md
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    import importlib.metadata

logger = logging.getLogger(__name__)

# Where a managed fleet drops the admission policy. Env wins so a fleet push can
# point at a managed, read-only location.
_POLICY_ENV = "KIROCLAW_ADMISSION_POLICY"
_POLICY_DEFAULT_PATH = Path.home() / ".kiroclaw" / "admission_policy.json"

# The manifest a plugin ships (read import-free from its distribution files).
_MANIFEST_FILENAME = "kiroclaw_plugin.json"

# Policy modes.
MODE_OPEN = "open"  # admit unless explicitly banned (public default)
MODE_ENFORCE = "enforce"  # admit only what passes every active check


@dataclass(frozen=True)
class PluginManifest:
    """A plugin's self-declaration, read before its code is imported."""

    name: str
    publisher: str = ""
    version: str = ""
    # Requested capabilities, e.g. {"tools": [...], "egress": [...], "paths": [...]}.
    capabilities: Dict[str, List[str]] = field(default_factory=dict)
    # Detached signature over the canonical manifest (sans this field).
    signature: str = ""

    @staticmethod
    def from_dict(d: dict) -> "PluginManifest":
        return PluginManifest(
            name=str(d.get("name", "")),
            publisher=str(d.get("publisher", "")),
            version=str(d.get("version", "")),
            capabilities={k: list(v) for k, v in (d.get("capabilities") or {}).items()},
            signature=str(d.get("signature", "")),
        )

    def signing_payload(self) -> bytes:
        """Canonical bytes the signature covers (manifest minus the signature)."""
        body = {
            "name": self.name,
            "publisher": self.publisher,
            "version": self.version,
            "capabilities": {k: sorted(v) for k, v in sorted(self.capabilities.items())},
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class AdmissionPolicy:
    """The fleet-controlled trust root. Never sourced from a plugin."""

    mode: str = MODE_OPEN
    require_signature: bool = False
    # publisher -> shared secret (POC: HMAC; real impl: publisher public key).
    trust_keys: Dict[str, str] = field(default_factory=dict)
    # Marketplace allowlist. None = no allowlist (any non-banned plugin). A
    # present (even empty) list = only these names are admitted.
    approved: Optional[List[str]] = None
    # Kill-switch. Always wins, in any mode.
    banned: List[str] = field(default_factory=list)
    # Per-capability ceiling, e.g. {"egress": ["*.amazon.com"], "paths": []}.
    # A declared capability value not covered by the ceiling rejects the plugin.
    capability_ceiling: Dict[str, List[str]] = field(default_factory=dict)

    @staticmethod
    def open_default() -> "AdmissionPolicy":
        """The public edition's policy: admit everything (no fleet to enforce)."""
        return AdmissionPolicy(mode=MODE_OPEN)

    @staticmethod
    def from_dict(d: dict) -> "AdmissionPolicy":
        approved = d.get("approved", None)
        return AdmissionPolicy(
            mode=str(d.get("mode", MODE_OPEN)),
            require_signature=bool(d.get("require_signature", False)),
            trust_keys={str(k): str(v) for k, v in (d.get("trust_keys") or {}).items()},
            approved=(list(approved) if approved is not None else None),
            banned=list(d.get("banned", [])),
            capability_ceiling={k: list(v) for k, v in (d.get("capability_ceiling") or {}).items()},
        )


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    manifest: Optional[PluginManifest] = None


def load_admission_policy() -> AdmissionPolicy:
    """Load the fleet admission policy. Default-open when none is present."""
    raw = os.environ.get(_POLICY_ENV, "").strip()
    path: Optional[Path] = None
    if raw:
        path = Path(raw)
    elif _POLICY_DEFAULT_PATH.exists():
        path = _POLICY_DEFAULT_PATH
    if path is None:
        return AdmissionPolicy.open_default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A present-but-unreadable policy is a fail-closed signal: a fleet meant
        # to enforce something. Refuse to silently fall open.
        logger.error("admission policy at %s is unreadable; failing closed", path)
        return AdmissionPolicy(mode=MODE_ENFORCE, require_signature=True, approved=[])
    return AdmissionPolicy.from_dict(data)


def _read_plugin_manifest(ep: "importlib.metadata.EntryPoint") -> Optional[PluginManifest]:
    """Read a plugin's manifest WITHOUT importing its module.

    Locates the manifest among the entry point's installed distribution files.
    Returns None when the plugin ships no manifest.
    """
    dist = getattr(ep, "dist", None)
    if dist is None:
        # Python 3.9 fallback: scan distributions for one owning this entry.
        try:
            for cand in _md.distributions():
                for e in cand.entry_points:
                    if e.group == ep.group and e.name == ep.name and e.value == ep.value:
                        dist = cand
                        break
                if dist is not None:
                    break
        except Exception:
            logger.debug("could not resolve distribution for entry point", exc_info=True)
            return None
    if dist is None:
        return None
    # 1) Try a packaged data file shipped inside the distribution.
    try:
        for f in dist.files or []:
            if f.name == _MANIFEST_FILENAME:
                text = f.read_text()  # type: ignore[attr-defined]
                if text:
                    return PluginManifest.from_dict(json.loads(text))
    except Exception:
        logger.debug("manifest file scan failed", exc_info=True)
    # 2) Fall back to a dist-info metadata file of the same name.
    try:
        text = dist.read_text(_MANIFEST_FILENAME)
        if text:
            return PluginManifest.from_dict(json.loads(text))
    except Exception:
        logger.debug("manifest dist-info read failed", exc_info=True)
    return None


def _signature_valid(manifest: PluginManifest, policy: AdmissionPolicy) -> bool:
    """Verify the manifest signature against a trust key the POLICY carries.

    POC uses HMAC-SHA256 with a per-publisher shared secret. A production
    implementation verifies an asymmetric signature against the publisher's
    public key pinned in the fleet policy — the shape (policy holds the trust
    root, plugin holds only the signature) is identical.
    """
    secret = policy.trust_keys.get(manifest.publisher)
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), manifest.signing_payload(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, manifest.signature)


def _capabilities_within_ceiling(
    manifest: PluginManifest, policy: AdmissionPolicy
) -> Optional[str]:
    """Return a rejection reason if any declared capability exceeds the ceiling."""
    for cap, requested in manifest.capabilities.items():
        ceiling = policy.capability_ceiling.get(cap)
        if ceiling is None:
            # Capability category not granted at all by the fleet.
            if requested:
                return f"capability {cap!r} not permitted by fleet policy"
            continue
        if "*" in ceiling:
            continue
        # Ceiling entries are case-SENSITIVE globs (e.g. "*.amazon.com" matches
        # "api.amazon.com"), matching the documented policy shape. Use
        # fnmatchcase (NOT fnmatch) so the admission decision is deterministic
        # across platforms — plain fnmatch runs through os.path.normcase, which
        # case-folds on macOS but not Linux, so the same policy would admit/reject
        # differently per OS. A requested value exceeds the ceiling only if NO
        # ceiling pattern matches it.
        over = [r for r in requested if not any(fnmatch.fnmatchcase(r, pat) for pat in ceiling)]
        if over:
            return f"capability {cap!r} requests {over} beyond ceiling {ceiling}"
    return None


def evaluate_admission(
    ep: "importlib.metadata.EntryPoint", policy: AdmissionPolicy
) -> AdmissionDecision:
    """Decide whether the plugin behind *ep* may load. Runs before ``ep.load()``."""
    manifest = _read_plugin_manifest(ep)
    plugin_name = manifest.name if manifest else ep.name

    # 1) Kill-switch always wins, in any mode.
    if plugin_name in policy.banned or ep.name in policy.banned:
        return AdmissionDecision(False, f"plugin {plugin_name!r} is banned (kill-switch)", manifest)

    # Whether the fleet has configured ANY active enforcement beyond the open
    # default.  A capability_ceiling counts: an operator who sets a ceiling
    # expects it enforced even in open mode (otherwise the ceiling is a silent
    # no-op).  Only when NOTHING is configured do we take the open fast path.
    enforcing = (
        policy.mode != MODE_OPEN
        or policy.approved is not None
        or policy.require_signature
        or bool(policy.capability_ceiling)
    )
    if not enforcing:
        # Truly-open policy: admit (the ban check above still applied).
        return AdmissionDecision(True, "admitted (open policy)", manifest)

    # From here the fleet is enforcing something; a manifest is required so the
    # checks below (allowlist / signature / capability ceiling) have something
    # to evaluate.
    if manifest is None:
        return AdmissionDecision(
            False, f"plugin {ep.name!r} ships no {_MANIFEST_FILENAME} manifest", None
        )

    # 2) Marketplace allowlist (skipped when no allowlist is configured).
    if policy.approved is not None and plugin_name not in policy.approved:
        return AdmissionDecision(
            False, f"plugin {plugin_name!r} is not on the approved allowlist", manifest
        )

    # 3) Verify-before-run signature (skipped unless required).
    if policy.require_signature and not _signature_valid(manifest, policy):
        return AdmissionDecision(
            False, f"plugin {plugin_name!r} signature invalid or unsigned", manifest
        )

    # 4) Capability ceiling — enforced whenever a ceiling is configured, in any
    #    mode (this is what makes a ceiling meaningful under an open policy).
    cap_reason = _capabilities_within_ceiling(manifest, policy)
    if cap_reason:
        return AdmissionDecision(False, f"plugin {plugin_name!r}: {cap_reason}", manifest)

    return AdmissionDecision(True, f"admitted ({policy.mode} policy)", manifest)
