"""Extension-point interfaces for the Composed Platform Providers contract.

Each Protocol here is one extension point — one place where behavior differs
between the public edition and the Amazon companion.  The public core ships a
``Default*`` implementation of every one (see ``defaults.py``); the companion
supplies an Amazon implementation for the subset it overrides.

These are ``Protocol`` types (structural) rather than ABCs so an adapter need
not import-inherit — it only has to match the shape.  ``PolicyAuthority`` is the
one exception: it is a concrete class in ``security_authority.py`` because its
deny decision must be ``@final`` to enforce the ADD-only floor.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from kiro_claw.config.loader import KiroClawConfig


# ── boot-layer extension points ──


class ProviderRegistry(Protocol):
    """The LLM-provider factory + ACP-backend registration seam.

    The public edition ships Kiro-CLI-ACP only.  The companion uses
    ``register_acp_backends`` to re-register Claude Code through the dormant
    ``ACP_BACKEND_CLAUDE`` seam without the core changing.
    """

    def create_factory(self, cfg: "KiroClawConfig") -> Callable[..., Any]:
        """Return the provider factory (today: cfg.create_provider_factory()).

        NOT YET WIRED: the core still calls ``cfg.create_provider_factory()``
        directly at every factory site; this seam is staged for a future
        migration. Overriding it has no effect yet.
        """
        ...

    def register_acp_backends(self) -> None:
        """Register any extra ACP backends (no-op in the public edition).

        Consumed at boot by ``bootstrap_context`` after the context installs.
        """
        ...


class AgentRuntime(Protocol):
    """The agent runtime: managed MCP servers + first-run setup.

    NOT YET WIRED: neither method is consumed by the core yet — managed servers
    are assembled in ``agent.py`` and first-run setup is called directly via
    ``agent.run_first_run_setup()``. The companion contributes internal MCP
    servers through ``McpToolingProvider.extra_mcp_servers`` instead (which IS
    wired). Staged for a later migration; overriding it has no effect yet.
    """

    def managed_mcp_servers(self) -> Dict[str, dict]:
        ...

    def run_first_run_setup(self) -> None:
        ...


class SandboxPolicy(Protocol):
    """The sandbox *data*: which dirs/files to hide/expose.

    The ``wrap_argv`` mechanism stays in ``sandbox.py``; only the directory and
    file lists are the extension point.  Public default = the open-source
    ``~/.aws``/``~/.ssh``/etc. lists; companion adds ``.midway``/``.ada``/etc.
    """

    def strict_dirs(self) -> List[str]:
        ...

    def cc_dirs(self) -> List[str]:
        ...


class CredentialPolicy(Protocol):
    """Redaction passes + the credential/exfil regex bundle.

    Public default = the AKIA/ASIA credential patterns and exfil URL patterns in
    ``security.py``.  The companion adds internal token/cookie regexes.
    """

    def redact(self, text: str) -> str:
        ...


class SlackEnterpriseGate(Protocol):
    """Slack enterprise/workspace allowlist + per-message origin gate.

    Public default = open (opt-in allowlist via ``slack.allowed_enterprise_ids``).
    The companion supplies the fail-closed Amazon workspace allowlist.

    Signatures mirror ``slack/enterprise.py``: ``validate_enterprise`` is called
    once at startup with the bot token; ``check_message_origin`` is the per-
    message in-memory check.
    """

    def validate_enterprise(
        self, bot_token: str, *, extra_ids: "set[str] | None" = None
    ) -> bool:
        ...

    def check_message_origin(self, event_team_id: str) -> bool:
        ...


class IdentityProvider(Protocol):
    """SSO/identity resolution.

    Public default = local token, no SSO (the ``midway.py`` no-op stubs).  The
    companion resolves through Midway / MCS / Kerberos.

    ``status_line`` is async to match the existing ``get_midway_status_line``
    coroutine the dashboard awaits.
    """

    def status(self) -> Dict[str, object]:
        ...

    async def status_line(self, prefix: str = "*Midway:*") -> str:
        ...

    def whoami(self) -> Optional[str]:
        ...

    def issuer(self) -> Optional[str]:
        ...


class EmbeddingSource(Protocol):
    """Where the embedding model comes from + request signing.

    Public default = the public Ollama registry (``qwen3-embedding:0.6b``),
    unsigned local requests.  The companion supplies an internal model source
    and a SigV4 request signer.
    """

    def registry_model(self) -> str:
        ...

    def endpoint_url(self) -> Optional[str]:
        ...

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]:
        ...


class McpToolingProvider(Protocol):
    """Extra MCP servers + skill catalog the edition contributes.

    Public default = none beyond the managed servers.  The companion injects
    builder-mcp and the internal AIM skill paths.
    """

    def extra_mcp_servers(self) -> Dict[str, dict]:
        ...

    def extra_skills(self) -> List[Path]:
        """NOT YET WIRED: the core does not yet load edition-contributed skill
        paths; only ``extra_mcp_servers`` is consumed. Staged for later.
        """
        ...


# ── install / structural extension points ──


class AppRegistryPolicy(Protocol):
    """Trusted git hosts + clone-sandbox-mode decision for the app registry.

    Public default = the open-source public-forge host set.  The companion adds
    internal git hosts as trusted.
    """

    def public_git_hosts(self) -> "frozenset[str]":
        ...

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str:
        ...


class AppsLoader(Protocol):
    """Discovery of bundled (builtin) apps + manifest sources.

    Public default = the open-source ``apps/builtins/`` set (``auto_research``,
    ``file_explorer``).  The companion bundles the internal feature apps.
    """

    def bundled_app_names(self) -> List[str]:
        ...

    def manifest_sources(self) -> List[Path]:
        ...


class PackageManager(Protocol):
    """Install strategy for external tools (ollama, etc.).

    Public default = brew/curl/pip fallbacks.  The companion uses the internal
    toolbox / capability installer.

    NOT YET WIRED: no core call site routes installs through this seam yet
    (callers still use their inline brew/curl/pip logic). Staged for later;
    overriding it has no effect yet.
    """

    def install_plan(self, tool: str) -> List[str]:
        ...

    def which(self, tool: str) -> Optional[str]:
        ...


# ── runtime-service / frontend extension points ──


class TunnelProvider(Protocol):
    """Public-URL tunnel lifecycle.

    Public default = disabled/no-op (the ``tunnel/manager.py`` stub).  The
    companion supplies the internal tunnel supervisor (the Tunnels primitive
    itself is owned by PartyRock and out of scope).
    """

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    def public_url(self) -> str:
        ...

    def enabled(self) -> bool:
        ...


class TelemetryProvider(Protocol):
    """Backend telemetry sink + the frontend RUM config blob.

    Public default = no-op; ``frontend_rum_config`` returns ``None`` so the
    SPA's RUM shim stays disabled.  The companion records events and returns the
    Cognito/RUM config the internal frontend host consumes.
    """

    def record_event(self, event_type: str, data: dict) -> None:
        ...

    def frontend_rum_config(self) -> Optional[dict]:
        ...


# ── feature apps ──


class FeatureApp(Protocol):
    """One bundled App-Kit app the active profile ships.

    Public default set is empty (or the OSS builtins).  The companion bundles
    mimir / code_reviewer / team_manager / secretary / taskkeeper / quip.

    NOT YET WIRED: the core does not yet read ``PlatformContext.feature_apps``;
    edition apps are registered via ``AppsLoader.manifest_sources`` /
    ``bundled_app_names`` instead. Staged for a later registration path;
    populating it has no effect yet.
    """

    @property
    def name(self) -> str:
        ...

    def manifest_path(self) -> Path:
        ...

    def register(self, ctx: Any) -> None:
        ...
