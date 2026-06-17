"""KiroClaw platform contract — the Composed Platform Providers (CPP) seam.

Public API for booting and reading the platform context.  Core code imports
from ``kiro_claw.platform`` only; the Amazon companion imports
``build_default_context`` + the interfaces and supplies its own adapters.

See ``docs/system-specs/modules/platform-context.md``.
"""

from __future__ import annotations

from kiro_claw.platform.admission import (
    AdmissionDecision,
    AdmissionPolicy,
    PluginManifest,
    evaluate_admission,
    load_admission_policy,
)
from kiro_claw.platform.bootstrap import boot_platform, bootstrap_context, build_default_context
from kiro_claw.platform.context import (
    CONTRACT_VERSION,
    PROFILE_AMAZON,
    PROFILE_STANDALONE,
    PlatformCompositionError,
    PlatformContext,
    current_context,
    reset_context,
    safe_context_call,
    set_context,
)
from kiro_claw.platform.discovery import PLUGIN_GROUP, PluginAdmissionError
from kiro_claw.platform.profile import resolve_profile
from kiro_claw.platform.security_authority import (
    BASELINE_DENY,
    PolicyAuthority,
    SecurityOverlay,
    assert_security_floor,
)

__all__ = [
    "CONTRACT_VERSION",
    "PROFILE_AMAZON",
    "PROFILE_STANDALONE",
    "PLUGIN_GROUP",
    "PlatformContext",
    "PlatformCompositionError",
    "boot_platform",
    "bootstrap_context",
    "build_default_context",
    "current_context",
    "safe_context_call",
    "set_context",
    "reset_context",
    "resolve_profile",
    "PolicyAuthority",
    "SecurityOverlay",
    "BASELINE_DENY",
    "assert_security_floor",
    # Plugin admission control
    "AdmissionPolicy",
    "AdmissionDecision",
    "PluginManifest",
    "PluginAdmissionError",
    "evaluate_admission",
    "load_admission_policy",
]
