"""Default adapters — the public open-source behavior for every extension point.

Each ``Default*`` adapter delegates to the existing module-level symbol it
replaces (``agent._MANAGED_MCP_SERVERS``, ``sandbox._STRICT_DIRS``,
``security.redact``, ``midway.*``, ``embeddings._OLLAMA_MODEL``, …) so the
standalone edition is behaviorally identical to today — the contract adds an
indirection layer, not a behavior change.

The Amazon companion subclasses or replaces these in its composition root.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from kiro_claw import midway, security

# ``agent``, ``sandbox``, ``embeddings``, ``apps.registry`` and ``slack.enterprise``
# import ``kiro_claw.platform`` at module-load time, so importing them at the top
# of this module (loaded during ``platform`` package init via ``bootstrap``)
# would create a cycle — those stay local to each method and carry a
# ``# circular import`` annotation.  ``security`` and ``midway`` do not reach
# ``platform``, so they are imported at top level.


class DefaultProviderRegistry:
    """Kiro-CLI-ACP only.  Leaves the dormant ACP_BACKEND_CLAUDE seam untouched."""

    def create_factory(self, cfg: Any) -> Callable[..., Any]:
        return cfg.create_provider_factory()

    def register_acp_backends(self) -> None:
        # The public edition registers no extra ACP backends.  The companion
        # re-registers Claude Code here via the acp/client.py:_is_claude seam.
        return None


class DefaultAgentRuntime:
    """Today's managed MCP servers + first-run setup."""

    def managed_mcp_servers(self) -> Dict[str, dict]:
        from kiro_claw import agent  # circular import: agent imports platform

        return dict(agent._MANAGED_MCP_SERVERS)

    def run_first_run_setup(self) -> None:
        # KiroClaw delta: ``agent.py`` has no ``run_first_run_setup`` symbol
        # (the KiroClaw reference delegates to ``agent.run_first_run_setup()``;
        # KiroClaw performs first-run wiring inline along its install path).
        # The public Default is therefore an explicit no-op rather than a call
        # into a missing function — the Amazon companion supplies the real
        # toolbox / Bedrock first-run setup when it overrides this adapter.
        return None


class DefaultSandboxPolicy:
    """Today's open-source sensitive-dir lists from ``sandbox.py``."""

    def strict_dirs(self) -> List[str]:
        from kiro_claw import sandbox  # circular import: sandbox imports platform

        return list(sandbox._STRICT_DIRS)

    def cc_dirs(self) -> List[str]:
        from kiro_claw import sandbox  # circular import: sandbox imports platform

        return list(sandbox._CC_DIRS)


class DefaultCredentialPolicy:
    """Today's AKIA/ASIA + exfil redaction passes from ``security.py``."""

    def redact(self, text: str) -> str:
        return security.redact(text)


class DefaultSlackEnterpriseGate:
    """Default-open gate delegating to ``slack/enterprise.py``."""

    def validate_enterprise(self, bot_token: str, *, extra_ids: "set[str] | None" = None) -> bool:
        # deferred: defaults.py loads at platform-init (bootstrap imports it);
        # importing slack.enterprise eagerly would pull the slack + config stack
        # into every boot. No import cycle here — kept local for lazy loading.
        from kiro_claw.slack import enterprise

        return enterprise.validate_enterprise(bot_token, extra_ids=extra_ids)

    def check_message_origin(self, event_team_id: str) -> bool:
        # deferred: see validate_enterprise above (lazy-load the slack stack;
        # no cycle).
        from kiro_claw.slack import enterprise

        return enterprise.check_message_origin(event_team_id)


class DefaultIdentityProvider:
    """No-SSO local identity — the ``midway.py`` no-op stubs."""

    def status(self) -> Dict[str, object]:
        return midway.midway_status()

    async def status_line(self, prefix: str = "*Midway:*") -> str:
        return await midway.get_midway_status_line(prefix)

    def whoami(self) -> Optional[str]:
        # The public edition has no SSO principal beyond what kiro-cli reports.
        return None

    def issuer(self) -> Optional[str]:
        return None


class DefaultEmbeddingSource:
    """Public Ollama registry, unsigned local requests."""

    def registry_model(self) -> str:
        from kiro_claw import embeddings  # circular import: embeddings imports platform

        return embeddings._OLLAMA_MODEL

    def endpoint_url(self) -> Optional[str]:
        # Public default uses the local Ollama daemon, not a remote endpoint.
        return None

    def sign_request(
        self, method: str, url: str, headers: dict, body: "bytes | str"
    ) -> Optional[dict]:
        # Unsigned: the local Ollama daemon needs no SigV4.
        return None


class DefaultMcpToolingProvider:
    """No extra MCP servers or skills beyond the managed set."""

    def extra_mcp_servers(self) -> Dict[str, dict]:
        return {}

    def extra_skills(self) -> List[Path]:
        return []


class DefaultAppRegistryPolicy:
    """Today's KiroClaw trusted-host set + clone-sandbox-mode decision.

    KiroClaw delta: KiroClaw's ``apps/registry.py`` allows a single *internal*
    GitFarm host (``_ALLOWED_GIT_HOST = "ssh://git.amazon.com/pkg/"``) and ships
    no public-forge set — so the KiroClaw Default encodes KiroClaw's CURRENT
    (Amazon-tinted) behavior: the internal host is trusted by default.  This is
    correct for the authoring home: in KiroClaw the Default == full internal
    behavior, and the de-amazon content sync strips it down to the public
    baseline in the kiro_claw edition (where the Amazon companion re-adds it).
    """

    def public_git_hosts(self) -> "frozenset[str]":
        from kiro_claw.apps import registry  # circular import: apps.registry imports platform

        return registry._PUBLIC_GIT_HOSTS

    def clone_sandbox_mode(self, git_url: str, trusted_hosts: "frozenset[str] | None") -> str:
        from kiro_claw.apps import registry  # circular import: apps.registry imports platform

        return registry._clone_sandbox_mode(git_url, trusted_hosts)


class DefaultAppsLoader:
    """The open-source ``apps/builtins/`` set."""

    def bundled_app_names(self) -> List[str]:
        # auto_research + file_explorer ship in the public core.
        return ["auto_research", "file_explorer"]

    def manifest_sources(self) -> List[Path]:
        return []


class DefaultPackageManager:
    """Public brew/curl/pip install strategy (delegated to cli_doctor logic)."""

    def install_plan(self, tool: str) -> List[str]:
        # The public edition has no managed installer; callers fall back to
        # their existing inline brew/curl/pip logic when the plan is empty.
        return []

    def which(self, tool: str) -> Optional[str]:
        return shutil.which(tool)


class DefaultTunnelProvider:
    """Disabled tunnel — the ``tunnel/manager.py`` stub is a no-op."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def public_url(self) -> str:
        return ""

    def enabled(self) -> bool:
        return False


class DefaultTelemetryProvider:
    """No-op telemetry; RUM stays disabled (frontend shim already no-op)."""

    def record_event(self, event_type: str, data: dict) -> None:
        return None

    def frontend_rum_config(self) -> Optional[dict]:
        return None
