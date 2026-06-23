"""The PlatformContext composition object and the active-context accessor.

KiroClaw uses the Composed Platform Providers (CPP) model to share one core
between the public open-source edition and the Amazon-internal companion.  The
public core defines a set of *extension points* — interfaces in
``kiro_claw.platform.interfaces`` — and ships a ``Default*`` adapter for each
that reproduces today's open-source behavior.  An internal companion package
supplies Amazon adapters for the same interfaces.

The :class:`PlatformContext` is the frozen object, built once at boot, that
holds the chosen adapter for every extension point.  Core code reads only from
the context (directly when it has it, or via :func:`current_context` for
module-level functions), so the public core never names an Amazon class.

See ``docs/system-specs/modules/platform-context.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Tuple, TypeVar

if TYPE_CHECKING:  # avoid import cycles — config.loader imports heavy modules
    from kiro_claw.config.loader import KiroClawConfig
    from kiro_claw.platform.governance import GovernanceCeiling
    from kiro_claw.platform.interfaces import (
        AgentRuntime,
        AppRegistryPolicy,
        AppsLoader,
        CredentialPolicy,
        EmbeddingSource,
        FeatureApp,
        IdentityProvider,
        McpToolingProvider,
        PackageManager,
        ProviderRegistry,
        SandboxPolicy,
        SlackEnterpriseGate,
        TelemetryProvider,
        TunnelProvider,
    )
    from kiro_claw.platform.security_authority import PolicyAuthority

# Bumped on any field add/rename or interface-semantics change.  A companion
# built against a different CONTRACT_VERSION refuses to compose (see
# bootstrap._assert_contract).
#
# v2: added the ``governance`` carrier (the enterprise security ceiling — see
# ``kiro_claw.platform.governance``).  A companion built against v1 must rebuild.
CONTRACT_VERSION = 2

# Valid profiles.  ``standalone`` is the public default; ``amazon`` loads the
# internal companion.  ``enterprise`` is reserved for a future third edition.
PROFILE_STANDALONE = "standalone"
PROFILE_AMAZON = "amazon"


class PlatformCompositionError(RuntimeError):
    """Raised when the platform context cannot be composed safely.

    This is a *fail-closed* signal: a non-standalone profile that cannot find
    its companion, a contract-version mismatch, or a companion that would
    weaken the security floor all abort boot rather than silently downgrade.
    """


@dataclass(frozen=True)
class PlatformContext:
    """Immutable bundle of the chosen adapter for every extension point.

    Built once at boot by :func:`kiro_claw.platform.bootstrap.bootstrap_context`
    and never mutated.  The public edition composes a context whose every
    interface field is a ``Default*`` adapter; the Amazon companion replaces a
    subset via ``dataclasses.replace`` in its composition root.
    """

    # ── carriers (not interfaces) ──
    contract_version: int
    profile: str
    cfg: "KiroClawConfig"

    # ── boot-layer extension points ──
    providers: "ProviderRegistry"
    agent_runtime: "AgentRuntime"
    sandbox: "SandboxPolicy"
    credentials: "CredentialPolicy"
    security: "PolicyAuthority"
    slack_gate: "SlackEnterpriseGate"
    identity: "IdentityProvider"
    embeddings: "EmbeddingSource"
    mcp_tooling: "McpToolingProvider"

    # ── install / structural extension points ──
    registry: "AppRegistryPolicy"
    apps_loader: "AppsLoader"
    package_manager: "PackageManager"

    # ── runtime-service / frontend extension points ──
    tunnel: "TunnelProvider"
    telemetry: "TelemetryProvider"

    # ── bundled feature apps ──
    feature_apps: "Tuple[FeatureApp, ...]"

    # ── governance carrier (Level 1 enterprise security ceiling) ──
    # Frozen at boot from the trust-root policy path; ``None`` on a standalone
    # host with no policy present (editable secure-defaults).  Read at every
    # enforcement chokepoint via ``current_context().governance``.  Defaulted so
    # the single constructor and the companion's ``dataclasses.replace`` paths
    # need no change beyond opting in.
    governance: "Optional[GovernanceCeiling]" = None

    @property
    def is_amazon(self) -> bool:
        return self.profile == PROFILE_AMAZON


# ── Active-context accessor ──
# Module-level functions that cannot easily take a ``ctx`` argument (e.g.
# security.is_denied, sandbox arg builders) read the process-global context set
# once at boot.  Tests that compose a non-default context must set_context()
# and reset around the test (see the reset_platform_context fixture).
_ACTIVE: Optional[PlatformContext] = None


def set_context(ctx: PlatformContext) -> None:
    """Install the process-global active context (called once at boot)."""
    global _ACTIVE
    _ACTIVE = ctx


def current_context() -> PlatformContext:
    """Return the active context, lazily building the standalone default.

    A lazy default keeps import-time and test call sites working even when boot
    has not run (e.g. a unit test that imports ``security`` directly).  Normally
    boot installs the real context via :func:`set_context` at process start, so
    the lazy path runs at most once before that.

    Cost / ordering note: while ``_ACTIVE`` is None the lazy path loads config +
    resolves the profile (a ``~/.midway`` stat).  On the STANDALONE happy path it
    runs once and memoizes into ``_ACTIVE``, so subsequent hot-path callers
    (``hooks.on_tool_call``, ``redact_via_context``) pay only an attribute read.
    A NON-standalone profile re-raises every call (it never caches a fail-open
    state).  Callers that drive a process/worker without ``boot_platform`` should
    install a context first to avoid the unbooted resolution; the lazy default is
    a fallback, not the intended boot path.

    Fail-closed guard: the lazy default is only safe when the host actually
    resolves to the standalone profile.  If the profile resolves to a
    non-standalone edition (e.g. an Amazon host with the opt-in ``~/.midway``
    probe or ``KIROCLAW_PROFILE=amazon``) but no context was installed — meaning
    boot failed/was-skipped and a caller would otherwise get open-source defaults
    with no security overlay or credential redaction — refuse to compose and
    raise :class:`PlatformCompositionError`.  Defense-in-depth so a future
    swallowing caller cannot reintroduce the silent fail-open.
    """
    global _ACTIVE
    if _ACTIVE is None:
        # deferred (not a cycle): keep config import off the module-load path so
        # importing kiro_claw.platform stays cheap; only the lazy-default path needs it.
        from kiro_claw.config.loader import KiroClawConfig
        from kiro_claw.platform.bootstrap import (  # circular import: bootstrap imports context
            build_default_context,
        )
        from kiro_claw.platform.discovery import (  # circular import: discovery imports context
            plugin_entry_points,
        )
        from kiro_claw.platform.profile import (
            resolve_profile,  # circular import: profile imports context
        )

        cfg = KiroClawConfig.load()
        profile = resolve_profile(cfg, entry_points=plugin_entry_points())
        if profile != PROFILE_STANDALONE:
            raise PlatformCompositionError(
                f"current_context() reached with no installed context but "
                f"profile resolved to {profile!r}; refusing to compose "
                "open-source defaults (fail-closed). Boot did not run or failed "
                "to compose the companion."
            )
        _ACTIVE = build_default_context(cfg, profile=PROFILE_STANDALONE)
    return _ACTIVE


def reset_context() -> None:
    """Clear the active context (test helper)."""
    global _ACTIVE
    _ACTIVE = None


_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


def safe_context_call(
    fn: "Callable[[], _T]",
    *,
    fallback: _T,
    log_message: "str | None" = None,
) -> _T:
    """Run a context-reading thunk fail-closed, degrading to *fallback*.

    The CPP fail-closed invariant: a :class:`PlatformCompositionError` (a
    non-standalone host that could not compose its companion) MUST abort rather
    than silently degrade to open-source defaults — so it is always re-raised.
    Any *other* exception (a transient adapter failure) degrades to *fallback*
    so a best-effort lookup never breaks the caller.

    Centralizing the idiom here means a call site cannot accidentally swallow
    ``PlatformCompositionError`` by writing a bare ``except Exception`` (the bug
    that previously recurred in several hand-written shims).

    ``log_message`` is logged at debug on the degrade path; pass ``None`` for
    callers that must not log (e.g. a stdio MCP server whose stray writes would
    corrupt the JSON-RPC stream).
    """
    try:
        return fn()
    except PlatformCompositionError:
        raise
    except Exception:
        if log_message is not None:
            _logger.debug(log_message, exc_info=True)
        return fallback


def redact_via_context(text: str) -> str:
    """Redact credentials/exfil from *text* through the active PlatformContext.

    The single, canonical credential-redaction shim every egress site should
    import — instead of hand-writing the ``try current_context().credentials
    .redact / except PlatformCompositionError: raise / except Exception:
    fallback`` idiom (the bug that previously recurred in several copies).

    Routes through ``current_context().credentials.redact`` so a loaded Amazon
    companion's extra credential/cookie regexes apply.  The Default
    ``CredentialPolicy.redact`` delegates to ``security.redact``, so a standalone
    process gets byte-for-byte today's redaction.  Recursion-safe: the Default
    delegates to the bare ``security.redact``, which never calls back into the
    context — only *callers* route through this shim.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) is re-raised, never swallowed, so such a
    host does NOT silently downgrade redaction to the OSS baseline.  Any other
    (transient) adapter failure degrades to the bare ``security.redact`` so the
    security pass never silently disappears.

    No logging on the degrade path: this shim runs inside stdio MCP servers
    (``mcp_core`` / ``mcp_cron``) whose stray writes would corrupt the JSON-RPC
    stream.
    """
    # Deferred import: keep ``security`` (which pulls the redaction regex stack)
    # off the platform module-load path; only the fallback path needs it, and
    # the happy path never imports it.
    try:
        return current_context().credentials.redact(text)
    except PlatformCompositionError:
        raise
    except Exception:
        from kiro_claw.security import redact as _security_redact

        return _security_redact(text)
