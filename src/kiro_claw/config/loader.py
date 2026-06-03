"""Configuration loader for KiroClaw.

Config location: ~/.kiroclaw/config.json (overridden by KIROCLAW_HOME)
Credentials:    ~/.kiroclaw/.env (overridden by KIROCLAW_HOME)

Supports provider selection (``claude_code``, ``acp``, or ``bedrock``), session
timeouts, hook rules, and the dashboard URL via the config file. (The dashboard
*port* is set with the ``KIROCLAW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kiro_claw import __version__, model_registry
from kiro_claw.acp.types import ACP_BACKEND_CLAUDE
from kiro_claw.effort import is_valid_effort, model_supports_effort

try:
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".kiroclaw"

# Credential keys loaded from .env / environment
CRED_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"
CRED_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
CRED_OWNER_ID = "KIROCLAW_OWNER_ID"
_CREDENTIAL_KEYS = (CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN, CRED_OWNER_ID)

DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
DEFAULT_MAX_PARALLEL_STEPS = 2

_DEFAULT_PORT = 7777

# KIROCLAW_PORT is validated at CLI entry (cli.py main()).
# By the time loader.py is imported the env var is a valid int or absent.
DASHBOARD_PORT: int = int(os.environ.get("KIROCLAW_PORT", _DEFAULT_PORT))


# Cross-platform workspace root for LLM working directories.
# Override: KIROCLAW_WORKSPACE env var or ~/.kiroclaw/workspace_dir
# macOS: /Volumes/workplace/kiroclaw-workspace (fallback ~/workplace)
# Linux: ~/workplace/kiroclaw-workspace
_WORKSPACE_DIR_NAME = "kiroclaw-workspace"


def _workspace_dir_file() -> Path:
    """Return the path to the saved workspace_dir file, respecting KIROCLAW_HOME."""
    return config_dir() / "workspace_dir"


def _default_workspace_base() -> Path:
    """Return the platform-specific default base for the workspace."""
    if sys.platform == "darwin":
        vol = Path("/Volumes/workplace")
        return vol if vol.is_dir() else Path.home() / "workplace"
    return Path.home() / "workplace"


def workspace_root() -> Path:
    """Return the top-level workspace root for LLM sessions and tasks.

    Resolution order:
    1. ``KIROCLAW_WORKSPACE`` env var (used as-is, no subdirectory appended)
    2. Saved path in ``config_dir()/workspace_dir`` (written by ``kiroclaw setup``)
    3. Platform default with ``kiroclaw-workspace`` subdirectory
    """
    override = os.environ.get("KIROCLAW_WORKSPACE")
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return root
    if _workspace_dir_file().is_file():
        try:
            saved = _workspace_dir_file().read_text(encoding="utf-8").strip()
            if saved:
                root = Path(saved)
                root.mkdir(parents=True, exist_ok=True)
                return root
        except OSError:
            pass
    base = _default_workspace_base()
    root = base / _WORKSPACE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_int(value: object, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _safe_dir_name(key: str) -> str:
    """Sanitize a session key into a safe directory name."""
    return key.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")


def _session_work_dir(session_key: str | None) -> Path:
    """Return a per-session subdirectory under workspace_root()."""
    root = workspace_root()
    if session_key:
        return root / _safe_dir_name(session_key)
    return root / "_default"


OUTBOX_DIR_NAME = "outbox"


def outbox_dir() -> Path:
    """Return the outbox directory for agent-to-user file delivery."""
    d = workspace_root() / OUTBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    override = os.environ.get("KIROCLAW_HOME")
    if override:
        p = Path(override).expanduser().resolve()
        # Refuse root or system directories as config home
        if p == Path("/") or p.parts[:2] in (("/", "usr"), ("/", "System"), ("/", "etc")):
            logger.warning("KIROCLAW_HOME=%s is a system directory, ignoring", override)
        else:
            p.mkdir(parents=True, exist_ok=True)
            return p
    d = Path.home() / CONFIG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def config_local_path() -> Path:
    """Return path to config.local.json — user overrides that survive upgrades."""
    return config_dir() / "config.local.json"


def read_local_secret() -> str:
    """Read ``<config_dir>/.local_secret`` (the gateway IPC secret), or ``""``.

    Single home for the secret-file read that callers (cron scripts, MCP tool
    bridges, CLI) need to authenticate to the gateway's internal API. Returns
    empty string if the file is absent/unreadable.
    """
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except OSError:
        return ""


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*, returning a new dict.

    - Dict values are merged recursively
    - All other types in overlay replace base values
    - Keys in overlay not in base are added
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _subtract_overlay(merged: dict, overlay: dict) -> dict:
    """Remove leaf values from *merged* that are owned by the overlay.

    For nested dicts, recurse. For leaf keys present in both overlay and
    merged with the same value, remove from the result so they only live
    in config.local.json.
    """
    result = dict(merged)
    for key, ov_value in overlay.items():
        if key not in result:
            continue
        if isinstance(ov_value, dict) and isinstance(result[key], dict):
            cleaned = _subtract_overlay(result[key], ov_value)
            if cleaned:
                result[key] = cleaned
            else:
                del result[key]
        elif result[key] == ov_value:
            del result[key]
    return result


def _raw_config() -> dict:
    """Load raw config.json as dict (cached per process)."""
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the ``dir`` field from ``WorkspaceConfig`` objects (new structured
    format) or falls back to raw string values (legacy flat format).

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kiroclaw/``).
    Unmapped workspace names fall back to ``"workspace"``.
    """
    data = _raw_config()
    ws = workspace or data.get("default_workspace", "default")
    mapping = data.get("workspaces", {})
    raw_value = mapping.get(ws, "workspace")

    # Extract the directory string from either format
    if isinstance(raw_value, dict):
        dirname = raw_value.get("dir", "workspace")
    elif isinstance(raw_value, str):
        dirname = raw_value
    else:
        dirname = "workspace"

    p = Path(dirname).expanduser()
    if p.is_absolute():
        return p
    return config_dir() / dirname


def default_project_dir(workspace: str | None = None) -> str:
    """Resolve the default project directory for a workspace.

    Returns the realpath of ``workspace_dir_for(workspace)`` if it exists and
    is not a sensitive path, otherwise returns ``""``.

    Used by chat_handlers (slot.project fallback) and session.py (pool cwd)
    to avoid duplicating the same resolution + validation logic.
    """
    from kiro_claw.security import is_sensitive_path  # circular import

    try:
        ws_dir = os.path.realpath(str(workspace_dir_for(workspace)))
        if os.path.isdir(ws_dir) and not is_sensitive_path(ws_dir):
            return ws_dir
    except Exception:
        pass
    return ""


def env_path() -> Path:
    return config_dir() / ".env"


def resolve_agent_config_path() -> Path:
    """Return defaults.json, preferring project-dir override for development.

    All modules that need the agent config path should call this instead
    of reimplementing the resolution chain.
    """
    proj = os.environ.get("KIROCLAW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "defaults.json"


DEFAULT_BEDROCK_MODEL = "anthropic.claude-sonnet-4-20250514"
DEFAULT_BEDROCK_REGION = "us-west-2"


def _meta(label: str, help: str, **kwargs: object) -> dict:
    """Helper to build field metadata dicts with safe defaults."""
    return {"label": label, "help": help, **kwargs}


_BOT_NAME_MAX = 50
_BOT_NAME_RE = _re.compile(r"[^a-zA-Z0-9 _\-.]")


def _sanitize_bot_name(raw: str) -> str:
    """Sanitize bot_name: strip markdown, braces, limit length."""
    if not isinstance(raw, str):
        return ""
    name = raw.strip()[:_BOT_NAME_MAX]
    name = name.replace("{", "").replace("}", "")
    return _BOT_NAME_RE.sub("", name)


@dataclass
class AgentConfig:
    approval_mode: str = field(
        default="auto",
        metadata=_meta("Approval Mode", "Tool approval mode.", enum=["auto", "interactive"]),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta("Streaming", "Enable streaming responses."),
    )
    model: str = field(
        default=DEFAULT_MODEL,
        metadata=_meta("Model", "LLM model identifier. 'auto' resolves from agent config."),
    )
    provider: str = field(
        default="claude_code",
        metadata=_meta("Provider", "LLM provider backend.", enum=["acp", "bedrock", "claude_code"]),
    )
    bedrock_model_id: str = field(
        default=DEFAULT_BEDROCK_MODEL,
        metadata=_meta("Bedrock Model ID", "AWS Bedrock model identifier."),
    )
    bedrock_region: str = field(
        default=DEFAULT_BEDROCK_REGION,
        metadata=_meta("Bedrock Region", "AWS region for Bedrock API calls."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Default agent name for new sessions."),
    )
    sandbox: str = field(
        default="auto",
        metadata=_meta("Sandbox", "Sandbox mode for ACP provider.", enum=["auto", "off"]),
    )
    # Claude Code specific (only used when provider="claude_code")
    cc_model: str = field(
        default="opus-4.8-1m",
        metadata=_meta(
            "CC Model",
            "Claude Code model — a canonical registry key (default 'opus-4.8-1m', "
            "Opus 4.8 with the 1M context window). Translated to a provider id at "
            "the config.loader factory. Aliases: opus, sonnet, auto (empty).",
        ),
    )
    cc_connection_mode: str = field(
        default="per_session",
        metadata=_meta(
            "CC Connection Mode",
            "Session lifecycle: per_session (resume across messages, ACP-style) or ephemeral (fresh per message).",
            enum=["per_session", "ephemeral"],
        ),
    )
    cc_max_turns: int = field(
        default=0,
        metadata=_meta("CC Max Turns", "Max turns for Claude Code sessions. 0 = unlimited."),
    )
    cc_max_budget_usd: float = field(
        default=0.0,
        metadata=_meta("CC Max Budget USD", "Max spend per session in USD. 0 = unlimited."),
    )
    yolo: bool = field(
        default=False,
        metadata=_meta("YOLO Mode", "Skip tool approval confirmations."),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom name the bot identifies as in conversations. Leave empty for default.",
        ),
    )
    conductor_skill: bool = field(
        default=False,
        metadata=_meta(
            "Conductor Skill",
            "Enable agent delegation — loads conductor skill with agent roster.",
        ),
    )
    max_subagents: int = field(
        default=3,
        metadata=_meta("Max SubAgents", "Maximum amount of subagents at one time."),
    )
    spawn_min_memory_gb: float = field(
        default=4.0,
        metadata=_meta(
            "Spawn Min Memory GB",
            "Minimum available memory (GB) required to spawn a subagent. 0 disables the check.",
        ),
    )
    subagent_max_turns: int = field(
        default=100,
        metadata=_meta("SubAgent Max Turns", "Default tool-call budget per subagent."),
    )
    subagent_timeout_secs: int = field(
        default=1800,
        metadata=_meta(
            "SubAgent Timeout (seconds)",
            "Wall-clock timeout per subagent execution. 0 uses hardcoded default (1800s).",
        ),
    )
    completion_keep: str = field(
        default="head",
        metadata=_meta(
            "Completion Keep",
            "Which end of the subagent transcript to keep in the completion event "
            "injected into the parent session. Three values: 'head' (first N chars), "
            "'tail' (last N chars), 'both' (head + middle marker + tail). The full "
            "transcript stays in result.txt until cleanup; use spawn_status MCP tool "
            "to read it.",
            enum=["head", "tail", "both"],
        ),
    )
    completion_keep_chars: int = field(
        default=3000,
        metadata=_meta(
            "Completion Keep Chars",
            "Maximum characters retained in the completion event after applying "
            "completion_keep. 0 disables truncation entirely. Default 3000.",
        ),
    )
    subagent_cwd_allowed_roots: list[str] = field(
        default_factory=lambda: ["~/workspace", "~/workplace"],
        metadata=_meta(
            "SubAgent CWD Allowed Roots",
            "Directory roots under which spawn_run's cwd parameter is permitted. "
            "Values support ~ expansion. Empty list disables cwd overrides.",
        ),
    )
    max_channels: int = field(
        default=1,
        metadata=_meta("Max Channels", "Maximum concurrent agent channels (1-5)."),
    )
    max_channel_agents: int = field(
        default=3,
        metadata=_meta("Max Channel Agents", "Maximum agents per channel (1-10)."),
    )
    log_level: str = field(
        default="WARNING",
        metadata=_meta(
            "Log Level",
            "Persistent log level for the kiro_claw logger. "
            "Applied at startup; overridden by --verbose CLI flag.",
            enum=["DEBUG", "INFO", "WARNING", "ERROR"],
        ),
    )
    enforce_denied_commands: str = field(
        default="all",
        metadata=_meta(
            "Enforce Denied Commands",
            "Scope for deniedCommands enforcement on kiro agent configs. "
            "'all' enforces on every agent; 'kiroclaw' only on the kiroclaw agent.",
            enum=["all", "kiroclaw"],
        ),
    )
    soft_stop_budget_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Soft-Stop Budget",
            "Seconds to wait for cooperative cancel before hard-killing the session.",
        ),
    )

    def __post_init__(self) -> None:
        self.max_channels = max(1, min(5, self.max_channels))
        self.max_channel_agents = max(1, min(10, self.max_channel_agents))
        # Clamp to [0.5, 60.0] to match ``KiroClawConfig.load()`` behavior
        # (dashboard PATCH and YAML loader both clamp rather than raise).
        clamped = max(0.5, min(60.0, float(self.soft_stop_budget_secs)))
        if clamped != self.soft_stop_budget_secs:
            logger.warning(
                "soft_stop_budget_secs=%s out of range [0.5, 60.0]; clamped to %s",
                self.soft_stop_budget_secs,
                clamped,
            )
            self.soft_stop_budget_secs = clamped


@dataclass
class SessionConfig:
    timeout_secs: int = field(
        default=DEFAULT_SESSION_TIMEOUT,
        metadata=_meta("Session Timeout", "Idle session timeout in seconds."),
    )
    autocompact_pct: float = field(
        default=90.0,
        metadata=_meta(
            "Auto-Compact Threshold",
            "Context usage percentage at which auto-compaction triggers (5-90).",
        ),
    )
    pool_size: int = field(
        default=0,
        metadata=_meta(
            "Warm Pool Size",
            "Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables.",
        ),
    )
    pool_agent: str = field(
        default="",
        metadata=_meta(
            "Warm Pool Agent",
            "Agent name for warm pool processes. Empty string uses agent.default_agent.",
        ),
    )
    pool_ttl_secs: int = field(
        default=1800,
        metadata=_meta(
            "Warm Pool TTL",
            "Max age in seconds for pooled processes. Stale processes are discarded at claim time. 0 disables.",
        ),
    )


@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = field(
        default=DEFAULT_MAX_PARALLEL_STEPS,
        metadata=_meta("Max Parallel Steps", "Maximum number of task steps to run in parallel."),
    )


@dataclass
class OrchestratorConfig:
    stage_timeout_seconds: int = field(
        default=1800,
        metadata=_meta(
            "Stage Timeout", "Max seconds per stage before auto-run stops. Default 30 min."
        ),
    )


@dataclass
class CronHistoryConfig:
    cron_summary_cap: int = field(
        default=200,
        metadata=_meta("Summary Cap", "Max characters for run summary field."),
    )
    cron_trace_cap_kb: int = field(
        default=50,
        metadata=_meta("Trace Cap KB", "Max kilobytes for run trace field."),
    )
    cron_max_records_per_job: int = field(
        default=100,
        metadata=_meta("Max Records Per Job", "Max history records kept per job file."),
    )
    cron_max_index_records: int = field(
        default=2000,
        metadata=_meta("Max Index Records", "Max records in the global index."),
    )


@dataclass
class MemoryConfig:
    embedding_provider: str = field(
        default="none",
        metadata=_meta(
            "Embedding Provider",
            "Vector embedding backend.",
            enum=["none", "ollama"],
        ),
    )
    embedding_url: str = field(
        default="http://localhost:11434",
        metadata=_meta("Embedding URL", "URL for the embedding service."),
    )
    allow_remote_embedding: bool = field(
        default=False,
        metadata=_meta("Allow Remote Embedding", "Allow non-localhost embedding endpoints."),
    )
    embedding_managed: bool = field(
        default=True,
        metadata=_meta(
            "Managed Embedding Server",
            "When true, KiroClaw starts/stops a local Ollama server. "
            "Set to false for external or SSH-forwarded Ollama instances.",
        ),
    )
    embedding_auth: str = field(
        default="none",
        metadata=_meta(
            "Embedding Auth",
            "Auth scheme for embedding requests. Default 'none' (local Ollama). "
            "Advanced: 'aws_sigv4' signs requests for an AWS-fronted endpoint (opt-in).",
        ),
    )
    embedding_dim: int = field(
        default=1024,
        metadata=_meta("Embedding Dimension", "Dimensionality of embedding vectors."),
    )
    embedding_model: str = field(
        default="qwen3-embedding:0.6b",
        metadata=_meta(
            "Embedding Model",
            "Ollama model name for embeddings. Must match embedding_dim (e.g. qwen3-embedding:0.6b=1024, nomic-embed-text=768).",
        ),
    )
    embedding_timeout_secs: float = field(
        default=5.0,
        metadata=_meta("Embedding Timeout", "Timeout in seconds for embedding requests."),
    )
    semantic_confidence_threshold: float = field(
        default=0.8,
        metadata=_meta(
            "Semantic Confidence Threshold",
            "Minimum similarity score for semantic search results.",
        ),
    )
    episodic_dedup_threshold: float = field(
        default=0.88,
        metadata=_meta(
            "Episodic Dedup Threshold",
            "Similarity threshold for deduplicating episodic memories.",
        ),
    )
    episodic_max_results: int = field(
        default=8,
        metadata=_meta("Episodic Max Results", "Maximum episodic memory results per query."),
    )
    episodic_max_count: int = field(
        default=10_000,
        metadata=_meta("Episodic Max Count", "Maximum total episodic memories stored."),
    )
    semantic_keys: list[str] = field(
        default_factory=list,
        metadata=_meta("Semantic Keys", "Keys to index for semantic search."),
    )
    history_idle_hours: float = field(
        default=3.0,
        metadata=_meta(
            "History Idle Hours",
            "Hours of inactivity before history consolidation.",
        ),
    )
    history_max_days: int = field(
        default=365,
        metadata=_meta("History Max Days", "Maximum days of history to retain."),
    )
    embedding_runtime: str = field(
        default="native",
        metadata=_meta(
            "Embedding Runtime",
            "How Ollama runs: 'native' (direct binary) or 'docker' (container fallback for AL2 glibc).",
            enum=["native", "docker"],
        ),
    )
    migrated: bool = field(
        default=False,
        metadata=_meta("Migrated", "Whether memory has been migrated to vector store."),
    )


@dataclass
class SlackConfig:
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "List of Slack users allowed to interact. Each entry: {slack_id, name}.",
        ),
    )
    tracking_channels: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Tracking Channels",
            "Slack channels to monitor. Each entry: {channel_id, name}.",
        ),
    )
    open_channels: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Open Channels",
            "Channel IDs where all users are authorized without allowlist.",
        ),
    )
    command: str = field(
        default="kiroclaw",
        metadata=_meta("Command", "Slack slash command trigger word."),
    )
    trusted_bot_ids: set[str] = field(
        default_factory=set,
        metadata=_meta(
            "Trusted Bot IDs",
            "Bot IDs allowed to bypass the bot filter for multi-node mesh communication.",
            tags=["slack"],
        ),
    )
    allowed_enterprise_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Enterprise IDs",
            "Slack Enterprise Grid org IDs to allow. Empty list allows all orgs (default-open).",
            tags=["slack"],
        ),
    )
    reactions: dict[str, str | None] = field(
        default_factory=dict,
        metadata=_meta(
            "Reactions",
            "Override phase reaction emojis. Valid keys: queued, thinking, coding, browsing, tool, done, error. "
            "Set a value to null to suppress that phase entirely.",
            tags=["slack"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Slack messages during processing.",
            tags=["slack"],
        ),
    )
    home_tab_sessions_per_kind: int = field(
        default=5,
        metadata=_meta(
            "Home Tab Sessions Per Kind",
            "Max sessions shown per category (main chat / autopilot) in the Slack Home Tab.",
            tags=["slack"],
        ),
    )
    use_tunnel_url: bool = field(
        default=False,
        metadata=_meta(
            "Use Tunnel URL in Slack",
            "When true, dashboard links posted to Slack (DM and channel challenge) "
            "use the tunnel URL if one is active. When false (default), "
            "Slack links always use the configured dashboard origin or host:port. "
            "Disabled by default until the tunnel mechanism is scaled for general use.",
            tags=["slack"],
        ),
    )


@dataclass
class DashboardConfig:
    url: str = field(
        default="",
        metadata=_meta(
            "Dashboard URL",
            "Public URL for the dashboard (used in Slack links).",
        ),
    )
    restore_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Restore Sessions",
            "Re-open recently active sessions on startup.",
        ),
    )
    restore_window_minutes: int = field(
        default=30,
        metadata=_meta(
            "Restore Window Minutes",
            "Time window (minutes) for session restoration (0-1440). 0 = restore all.",
        ),
    )
    bot_name: str = field(
        default="",
        metadata=_meta(
            "Bot Name",
            "Custom bot display name for the dashboard UI.",
        ),
    )
    avatar: str = field(
        default="",
        metadata=_meta(
            "Avatar",
            "Path to custom avatar image for the dashboard UI.",
        ),
    )
    merge_queued_messages: bool = field(
        default=False,
        metadata=_meta(
            "Merge Queued Messages",
            "Concatenate follow-up messages while the agent is busy instead of queueing them separately.",
        ),
    )
    mcp_probe_timeout_secs: int = field(
        default=15,
        metadata=_meta(
            "MCP Probe Timeout",
            "Seconds to wait for MCP server handshake during probe (5-120).",
        ),
    )
    widget_density: str = field(
        default="more",
        metadata=_meta(
            "Widget Density",
            "How aggressively the agent uses inline widgets. "
            "'more' encourages widgets for any visual content; "
            "'less' limits to only when markdown is clearly insufficient.",
            enum=["more", "less"],
        ),
    )
    auto_open_browser: bool = field(
        default=True,
        metadata=_meta(
            "Auto Open Browser",
            "Open the dashboard URL in the default browser on gateway startup.",
        ),
    )
    quick_send: bool = field(
        default=False,
        metadata=_meta(
            "Quick Send",
            "Click a suggested reply to send it instantly. Shift+Click to select multiple.",
        ),
    )
    terminal: dict = field(
        default_factory=lambda: {"enabled": False},
        metadata=_meta(
            "Terminal",
            "Terminal panel configuration. Set enabled=true to show CLI panel in dashboard.",
        ),
    )
    default_project: str = field(
        default="",
        metadata=_meta(
            "Default Project",
            "Directory path used as the project for new chat tabs. Empty = workspace dir.",
        ),
    )


@dataclass
class KiroClawAgentConfig:
    kiro_agent: str = field(
        default="",
        metadata=_meta("Kiro Agent", "Kiro agent name (modeId for session/set_mode)."),
    )
    workspace: str = field(
        default="default",
        metadata=_meta("Workspace", "Named workspace from the workspaces section."),
    )
    memory_store: str = field(
        default="default",
        metadata=_meta("Memory Store", "Named memory store from the memory_stores section."),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable agent description."),
    )
    source: str = field(
        default="kiroclaw",
        metadata=_meta("Source", "Agent origin: kiroclaw or builtin."),
    )


@dataclass
class WorkspaceConfig:
    dir: str = field(
        default="workspace",
        metadata=_meta("Directory", "Workspace directory path."),
    )


@dataclass
class MemoryStoreConfig:
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable purpose of this memory store."),
    )
    embedding_provider: str = field(
        default="",
        metadata=_meta(
            "Embedding Provider",
            "Override embedding backend for this store. Empty inherits from top-level memory.",
            enum=["", "none", "ollama"],
        ),
    )


@dataclass
class ExternalRegistryConfig:
    """An external app registry source (org-owned repo with app.json files)."""

    name: str = field(
        default="",
        metadata=_meta("Name", "Human-readable registry name (e.g. 'identityservices')."),
    )
    repo: str = field(
        default="",
        metadata=_meta("Repo", "GitFarm package name containing apps."),
    )
    branch: str = field(
        default="mainline",
        metadata=_meta("Branch", "Git branch to read from."),
    )


@dataclass
class SkillsConfig:
    max_triggered: int = field(
        default=3,
        metadata=_meta("Max Triggered", "Maximum number of skills to load per message (≥1)."),
    )
    # ── Hermes-style auto skill creation (Mesh-677) ──
    # All fields default to OFF so upgrades are zero-impact. Enable via
    # ``kiroclaw config set skills.auto_create_from_sessions true`` or the
    # dashboard Settings → Skills panel (future).
    auto_create_from_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Create Skills",
            "When true, analyze each session after completion and synthesize a reusable "
            "SKILL.md when a non-trivial multi-step procedure is detected. Generated "
            "skills live under skills/auto/ so they never collide with hand-authored "
            "skills. Disabled by default.",
        ),
    )
    auto_refine_on_deviation: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Refine Skills",
            "When true, update an existing auto-created skill if the agent succeeds "
            "via a different tool sequence than documented. Requires "
            "auto_create_from_sessions. Disabled by default.",
        ),
    )
    auto_min_tool_calls: int = field(
        default=5,
        metadata=_meta(
            "Auto Min Tool Calls",
            "Minimum tool calls in a session for it to qualify for skill extraction "
            "(≥2). Lower values produce more skills but reduce quality.",
        ),
    )
    auto_similarity_threshold: float = field(
        default=0.85,
        metadata=_meta(
            "Auto Similarity Threshold",
            "Skip creation when an existing skill's description has keyword overlap "
            "≥ this fraction with the synthesized description (0.0-1.0). Prevents "
            "near-duplicate skills.",
        ),
    )

    def __post_init__(self) -> None:
        if self.max_triggered < 1:
            logger.warning("max_triggered %d < 1, using 1", self.max_triggered)
            object.__setattr__(self, "max_triggered", 1)
        if self.auto_min_tool_calls < 2:
            logger.warning("auto_min_tool_calls %d < 2, using 2", self.auto_min_tool_calls)
            object.__setattr__(self, "auto_min_tool_calls", 2)
        if not 0.0 <= self.auto_similarity_threshold <= 1.0:
            logger.warning(
                "auto_similarity_threshold %.2f out of range [0.0, 1.0], using 0.85",
                self.auto_similarity_threshold,
            )
            object.__setattr__(self, "auto_similarity_threshold", 0.85)
        if self.auto_refine_on_deviation and not self.auto_create_from_sessions:
            logger.warning(
                "auto_refine_on_deviation requires auto_create_from_sessions; "
                "disabling auto_refine_on_deviation"
            )
            object.__setattr__(self, "auto_refine_on_deviation", False)


# ---------------------------------------------------------------------------
# Validation helpers — used by KiroClawConfig.load()
# ---------------------------------------------------------------------------

# JSON Schema type → Python type names for log messages
_JSON_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def _lookup_schema_node(schema: dict, dot_path: str) -> dict | None:
    """Walk the JSON Schema tree to find the node for a dot-separated path."""
    parts = dot_path.split(".")
    node = schema
    for part in parts:
        props = node.get("properties", {})
        if part in props:
            node = props[part]
        else:
            return None
    return node


def _is_sensitive_path(schema: dict, dot_path: str) -> bool:
    """Return True if the field at *dot_path* is marked sensitive."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return False
    return node.get("x-meta", {}).get("sensitive", False)


def _is_deprecated_path(schema: dict, dot_path: str) -> bool:
    """Return True if the field at *dot_path* is marked deprecated."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return False
    return node.get("x-meta", {}).get("deprecated", False)


def _get_help_text(schema: dict, dot_path: str) -> str:
    """Return the help text for the field at *dot_path*."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return ""
    return node.get("x-meta", {}).get("help", "")


def _mask_value(value: object, sensitive: bool) -> str:
    """Return a display string for a value, masking if sensitive."""
    if sensitive:
        return '"***"'
    return repr(value)


def _dot_path_from_json_path(path: list) -> str:
    """Convert a jsonschema error path (deque of keys) to a dot-separated string."""
    return ".".join(str(p) for p in path)


def _actual_type_name(value: object) -> str:
    """Return a human-readable type name for a JSON value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _apply_field_default(data: dict, dot_path: str) -> None:
    """Remove the invalid value at *dot_path* so the loader falls back to defaults.

    Only handles top-level and one-level nested paths (e.g. ``agent.provider``).
    """
    parts = dot_path.split(".")
    if len(parts) == 1:
        data.pop(parts[0], None)
    elif len(parts) == 2:
        section = data.get(parts[0])
        if isinstance(section, dict):
            section.pop(parts[1], None)


def _validate_config_data(data: dict) -> dict:
    """Validate *data* against the config JSON Schema.

    Logs warnings for any issues found and mutates *data* in-place to
    remove invalid values (so the loader falls back to field defaults).
    Always returns *data* — never raises.
    """
    if not _HAS_JSONSCHEMA:
        return data

    # Lazy import to avoid circular import at module level
    from kiro_claw.config.schema import JSON_SCHEMA, SCHEMA_REGISTRY

    # 1. Detect unrecognized top-level keys
    known_top_keys = {e.path for e in SCHEMA_REGISTRY if "." not in e.path and e.path != "*"}
    unknown = sorted(set(data.keys()) - known_top_keys)
    if unknown:
        logger.warning("Config: unrecognized top-level keys: %s", ", ".join(unknown))

    # 2. Detect deprecated fields and log warnings
    for entry in SCHEMA_REGISTRY:
        if not entry.deprecated:
            continue
        parts = entry.path.split(".")
        # Check if the deprecated key is present in data
        node = data
        found = True
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                found = False
                break
        if found:
            logger.warning(
                "Config: deprecated field '%s': %s",
                entry.path,
                entry.help,
            )

    # 3. Normalize case-insensitive enum fields before validation
    agent = data.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("log_level"), str):
        agent["log_level"] = agent["log_level"].upper()

    # 4. Run jsonschema validation
    try:
        jsonschema.validate(data, JSON_SCHEMA)
    except jsonschema.ValidationError:
        # Collect all errors (including nested ones)
        validator_cls = jsonschema.validators.validator_for(JSON_SCHEMA)
        validator = validator_cls(JSON_SCHEMA)
        for err in validator.iter_errors(data):
            dot_path = _dot_path_from_json_path(err.absolute_path)
            if not dot_path:
                # Root-level schema error — skip
                continue

            sensitive = _is_sensitive_path(JSON_SCHEMA, dot_path)
            value = err.instance
            display_val = _mask_value(value, sensitive)

            # Determine error type
            if err.validator == "enum":
                allowed = err.schema.get("enum", [])
                logger.warning(
                    "Config: enum violation at '%s': " "allowed values %s, got %s; using default",
                    dot_path,
                    allowed,
                    display_val,
                )
                _apply_field_default(data, dot_path)
            elif err.validator == "type":
                expected = err.schema.get("type", "unknown")
                actual = _actual_type_name(value)
                logger.warning(
                    "Config: type mismatch at '%s': "
                    "expected %s, got %s (value: %s); using default",
                    dot_path,
                    expected,
                    actual,
                    display_val,
                )
                _apply_field_default(data, dot_path)
            else:
                # Generic validation error
                logger.warning(
                    "Config: validation error at '%s': %s; using default",
                    dot_path,
                    err.message,
                )
                _apply_field_default(data, dot_path)

    return data


# Channel activation modes
ACTIVATION_ALWAYS = "always"  # Process every message
ACTIVATION_MENTION = "mention"  # Only respond when @mentioned
ACTIVATION_OBSERVE = "observe"  # Record messages, respond only when @mentioned (deep context)
ACTIVATION_REVIEW = "review"  # Generate response, show ephemeral draft for owner approval
ACTIVATION_OFF = "off"  # Ignore all messages completely — no history recorded
_VALID_ACTIVATIONS = frozenset(
    {ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OBSERVE, ACTIVATION_REVIEW, ACTIVATION_OFF}
)


@dataclass
class ChannelConfig:
    """Per-channel Slack configuration."""

    activation: str = field(
        default=ACTIVATION_MENTION,
        metadata=_meta(
            "Activation",
            "Channel activation mode.",
            enum=["always", "mention", "observe", "review", "off"],
        ),
    )
    agent: str = field(
        default="",
        metadata=_meta("Agent", "Agent override for this channel (empty = default)."),
    )
    thread_follow: bool = field(
        default=True,
        metadata=_meta(
            "Thread Follow",
            "Respond to all messages in threads where bot was previously @mentioned.",
        ),
    )

    @classmethod
    def from_dict(cls, data: dict) -> ChannelConfig:
        activation = data.get("activation", ACTIVATION_MENTION)
        if activation not in _VALID_ACTIVATIONS:
            activation = ACTIVATION_MENTION
        return cls(
            activation=activation,
            agent=data.get("agent", ""),
            thread_follow=data.get("thread_follow", True),
        )


_VALID_STT_PROVIDERS = ("whisper", "transcribe")
_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


def _validated_stt_provider(value: str) -> str:
    """Return *value* if recognised, else warn and default to whisper."""
    if value in _VALID_STT_PROVIDERS:
        return value
    logger.warning("Unknown STT provider '%s', falling back to whisper", value)
    return "whisper"


_VALID_COMPLETION_KEEP = ("head", "tail", "both")


def _validated_completion_keep(value: object) -> str:
    """Return *value* if it is one of head/tail/both, else raise ValueError."""
    if isinstance(value, str) and value in _VALID_COMPLETION_KEEP:
        return value
    raise ValueError(
        f"agent.completion_keep must be one of {list(_VALID_COMPLETION_KEEP)}, " f"got {value!r}"
    )


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


def _validate_tracking_channels(raw: list) -> list[dict]:
    """Validate and coerce tracking_channels entries.

    Accepted formats:
    - ``{"channel_id": "C...", "name": "..."}`` — passed through
    - ``"C..."`` (bare string) — auto-coerced to ``{"channel_id": "C..."}`` with a warning

    Rejects entries that are neither strings starting with C/D/G nor dicts with channel_id.
    """
    if not raw:
        return []
    result: list[dict] = []
    coerced = 0
    rejected = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("channel_id"):
            result.append(entry)
        elif isinstance(entry, str) and len(entry) > 1 and entry[0] in _VALID_CHANNEL_PREFIXES:
            result.append({"channel_id": entry})
            coerced += 1
        else:
            rejected += 1
    if coerced:
        logger.warning(
            "Config: slack.tracking_channels has %d bare string(s) — auto-coerced to "
            '{"channel_id": "..."} format. Prefer: [{"channel_id": "C...", "name": "..."}]',
            coerced,
        )
    if rejected:
        logger.warning(
            "Config: slack.tracking_channels has %d invalid entries (expected objects with "
            '"channel_id" field or bare channel ID strings starting with C/D/G). '
            "These entries were ignored.",
            rejected,
        )
    return result


def _migrate_workspaces(raw_workspaces: dict) -> dict[str, WorkspaceConfig]:
    """Auto-migrate workspaces from flat or structured format.

    - String values → WorkspaceConfig(dir=value)
    - Dict values with ``dir`` key → WorkspaceConfig(dir=value["dir"])
    - Non-string/non-dict values → default WorkspaceConfig()
    - Empty input → {"default": WorkspaceConfig(dir="workspace")}
    """
    result: dict[str, WorkspaceConfig] = {}
    for name, value in raw_workspaces.items():
        if isinstance(value, str):
            result[name] = WorkspaceConfig(dir=value)
        elif isinstance(value, dict):
            result[name] = WorkspaceConfig(dir=value.get("dir", "workspace"))
        else:
            result[name] = WorkspaceConfig()
    if not result:
        result["default"] = WorkspaceConfig(dir="workspace")
    return result


def resolve_memory_store_config(
    top_level_memory: dict,
    store_overrides: dict,
) -> dict:
    """Deep-merge store overrides onto top-level memory defaults.

    Merge happens at the raw dict level BEFORE dataclass construction.
    A store that only sets embedding_provider inherits all other memory
    settings from the top-level config, not from MemoryConfig defaults.
    """
    merged = dict(top_level_memory)
    for key, value in store_overrides.items():
        if key == "description":
            continue  # description is store-only metadata, not a memory setting
        if value != "" and value is not None:
            merged[key] = value
    return merged


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str


@dataclass
class SttConfig:
    """Speech-to-text configuration (opt-in, disabled by default)."""

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Enable voice memo transcription."),
    )
    provider: str = field(
        default="whisper",
        metadata=_meta("Provider", "STT provider.", enum=list(_VALID_STT_PROVIDERS)),
    )
    whisper_path: str = field(
        default="",
        metadata=_meta("Whisper Path", "Path to whisper binary (auto-detected if empty)."),
    )
    model: str = field(
        default="turbo",
        metadata=_meta("Model", "Whisper model size.", enum=["turbo"]),
    )
    device: str = field(
        default="cpu",
        metadata=_meta("Device", "Computation device.", enum=["cpu", "cuda"]),
    )
    timeout_secs: int = field(
        default=300,
        metadata=_meta("Timeout", "Transcription timeout in seconds."),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )
    language_code: str = field(
        default="en-US",
        metadata=_meta(
            "Language Code", "Language for speech recognition (e.g. en-US, fr-FR, es-ES)."
        ),
    )
    streaming: bool = field(
        default=False,
        metadata=_meta(
            "Streaming",
            "Stream partial transcripts live to the dashboard input (transcribe provider only).",
        ),
    )


DEFAULT_QUICK_REACTIONS: list[str] = [
    "thumbsup",
    "white_check_mark",
    "eyes",
    "pray",
    "tada",
    "heart",
]


@dataclass
class KeywordHook:
    """A keyword-triggered workflow hook for Secretary."""

    keyword: str
    action: str  # "spawn_session" | "notify" | "auto_reply"
    agent: str = "kiroclaw"
    template: str = "{message}"
    channels: list[str] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    autonudge: bool = True
    max_cycles: int = 24
    cooldown_seconds: int = 300
    # auto_reply only: prompt the LLM uses to draft the reply.
    # Supports placeholders: {message}, {sender}, {channel_name}, {keyword}, {context}.
    # If empty, falls back to Secretary's standard draft prompt (style_rules + user context).
    reply_prompt: str = ""


@dataclass
class SecretaryConfig:
    """Secretary — reads your Slack, drafts replies, presents for approval."""

    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable Secretary background polling."),
    )
    user_id: str = field(
        default="",
        metadata=_meta("User ID", "Authenticated Slack user ID (set during setup)."),
    )
    watched_channels: list[str] = field(
        default_factory=list,
        metadata=_meta("Watched Channels", "Slack channel IDs to monitor."),
    )
    poll_interval_seconds: int = field(
        default=60,
        metadata=_meta("Poll Interval", "Seconds between polls."),
    )
    style_rules: list[str] = field(
        default_factory=list,
        metadata=_meta("Style Rules", "Initial communication style rules for drafting."),
    )
    alert_keywords: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Alert Keywords", "Keywords that trigger immediate notification (e.g. SEV, outage)."
        ),
    )
    alert_on_name_mention: bool = field(
        default=False,
        metadata=_meta("Alert on Name Mention", "Notify when your name is mentioned without @."),
    )
    test_mode: bool = field(
        default=False,
        metadata=_meta("Test Mode", "Include own messages in inbox (for testing)."),
    )
    quick_reactions: list[str] = field(
        default_factory=lambda: list(DEFAULT_QUICK_REACTIONS),
        metadata=_meta("Quick Reactions", "Emoji names for the quick-access reaction row."),
    )
    auto_cleanup_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Auto Cleanup",
            "Automatically delete stored Slack sessions after retention period.",
        ),
    )
    dm_retention_days: int = field(
        default=90,
        metadata=_meta("DM Retention (days)", "Days to retain DM sessions before auto-deletion."),
    )
    channel_retention_days: int = field(
        default=365,
        metadata=_meta(
            "Channel Retention (days)",
            "Days to retain channel message sessions before auto-deletion.",
        ),
    )
    keyword_hooks: list[dict] = field(
        default_factory=list,
        metadata=_meta("Keyword Hooks", "Keyword-triggered workflow dispatchers."),
    )


@dataclass
class TaskKeeperConfig:
    """TaskKeeper task management with triage and To-Do sync."""

    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable TaskKeeper app."),
    )
    username: str = field(
        default="",
        metadata=_meta("Username", "Slack username for @mention search."),
    )
    email_enabled: bool = field(
        default=False,
        metadata=_meta("Email", "Enable email scanning."),
    )
    scan_interval_seconds: int = field(
        default=300,
        metadata=_meta("Scan Interval", "Seconds between automatic scans (min 60)."),
    )
    auto_scan_enabled: bool = field(
        default=False,
        metadata=_meta("Auto Scan", "Enable periodic background scanning."),
    )


@dataclass
class TunnelConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable a tunnel to expose the dashboard for remote access."),
    )
    name_mode: str = field(
        default="username",
        metadata=_meta(
            "Name Mode",
            "Tunnel naming: 'username' uses 'kiroclaw', "
            "'hash' uses 'kiroclaw-<hostHash>' for multi-host disambiguation.",
            enum=["username", "hash"],
        ),
    )
    name_override: str = field(
        default="",
        metadata=_meta(
            "Name Override",
            "Explicit tunnel name (overrides name_mode). "
            "Note: some tunnel providers prefix your username (e.g. 'foo' becomes '<user>-foo').",
        ),
    )


@dataclass
class KiroClawConfig:
    agent: AgentConfig = field(
        default_factory=AgentConfig,
        metadata=_meta("Agent", "Agent runtime configuration."),
    )
    session: SessionConfig = field(
        default_factory=SessionConfig,
        metadata=_meta("Session", "Session management settings."),
    )
    taskrunner: TaskRunnerConfig = field(
        default_factory=TaskRunnerConfig,
        metadata=_meta("Task Runner", "Task runner configuration."),
    )
    orchestrator: OrchestratorConfig = field(
        default_factory=OrchestratorConfig,
        metadata=_meta("Orchestrator", "Autopilot/orchestrator settings."),
    )
    cron_history: CronHistoryConfig = field(
        default_factory=CronHistoryConfig,
        metadata=_meta("Cron History", "Cron execution history storage limits."),
    )
    memory: MemoryConfig = field(
        default_factory=MemoryConfig,
        metadata=_meta("Memory", "Memory and embedding configuration."),
    )
    skills: SkillsConfig = field(
        default_factory=SkillsConfig,
        metadata=_meta("Skills", "Skill loading and matching configuration."),
    )
    stt: SttConfig = field(
        default_factory=SttConfig,
        metadata=_meta("STT", "Speech-to-text transcription settings."),
    )
    secretary: SecretaryConfig = field(
        default_factory=SecretaryConfig,
        metadata=_meta("Secretary", "Secretary reads Slack, drafts replies."),
    )
    taskkeeper: TaskKeeperConfig = field(
        default_factory=TaskKeeperConfig,
        metadata=_meta("TaskKeeper", "TaskKeeper task management with triage and To-Do sync."),
    )

    slack: SlackConfig = field(
        default_factory=SlackConfig,
        metadata=_meta("Slack", "Slack integration settings.", tags=["slack"]),
    )
    dashboard: DashboardConfig = field(
        default_factory=DashboardConfig,
        metadata=_meta("Dashboard", "Dashboard UI settings."),
    )
    tunnel: TunnelConfig = field(
        default_factory=TunnelConfig,
        metadata=_meta("Tunnel", "AEA tunnel settings for remote dashboard access."),
    )
    hooks: dict = field(
        default_factory=dict,
        metadata=_meta("Hooks", "Script hook definitions keyed by hook ID."),
    )
    slack_channels: dict[str, ChannelConfig] = field(
        default_factory=dict,
        metadata=_meta("Slack Channels", "Per-channel activation config."),
    )
    slack_dm_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta("Slack DM Activation", "Default activation mode for DMs."),
    )
    observe_max_messages: int = field(
        default=200,
        metadata=_meta("Observe Max Messages", "Max messages per observe-mode channel."),
    )
    observe_ttl_hours: float = field(
        default=168.0,
        metadata=_meta("Observe TTL Hours", "Hours to keep observe history."),
    )
    agents: dict[str, KiroClawAgentConfig] = field(
        default_factory=dict,
        metadata=_meta("Agents", "Named KiroClaw agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active KiroClaw agent name from the agents section."),
    )
    workspaces: dict[str, WorkspaceConfig] = field(
        default_factory=dict,
        metadata=_meta("Workspaces", "Named workspace definitions."),
    )
    default_workspace: str = field(
        default="default",
        metadata=_meta("Default Workspace", "Active workspace name."),
    )
    memory_stores: dict[str, MemoryStoreConfig] = field(
        default_factory=dict,
        metadata=_meta("Memory Stores", "Named memory store definitions."),
    )
    default_memory_store: str = field(
        default="default",
        metadata=_meta("Default Memory Store", "Fallback memory store name."),
    )
    auto_update: bool = field(
        default=True,
        metadata=_meta("Auto Update", "Enable automatic update checks."),
    )
    timezone: str = field(
        default="",
        metadata=_meta(
            "Timezone",
            "IANA timezone name (e.g. 'America/Los_Angeles'). "
            "Used to display cron schedules in local time.",
        ),
    )
    snapshot_dir: str = field(
        default="",
        metadata=_meta(
            "Snapshot Directory",
            "Directory for kiroclaw snapshot output. "
            "Defaults to ~/.kiroclaw/snapshots if empty.",
        ),
    )
    registries: list[ExternalRegistryConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Registries",
            "External app registries (org-owned repos). " "Each entry: {name, repo, branch}.",
        ),
    )

    def channel_config(self, channel_id: str) -> ChannelConfig:
        """Return the config for *channel_id*, falling back to defaults.

        DMs (channel IDs starting with ``D``) use ``slack_dm_activation``.
        Group channels use ``mention`` unless overridden in ``slack_channels``.
        """
        if channel_id in self.slack_channels:
            return self.slack_channels[channel_id]
        if channel_id.startswith("D"):
            return ChannelConfig(activation=self.slack_dm_activation)
        return ChannelConfig(activation=ACTIVATION_MENTION)

    @property
    def slack_enterprise_ids(self) -> set[str]:
        """Extra allowed enterprise IDs from ``slack.allowed_enterprise_ids``."""
        return set(self.slack.allowed_enterprise_ids)

    @classmethod
    def load(cls) -> KiroClawConfig:
        """Load config from ~/.kiroclaw/config.json, falling back to defaults.

        If ``config.local.json`` exists alongside ``config.json``, it is
        deep-merged on top. User overrides in the local file survive
        upgrades that regenerate ``config.json``.

        The overlay is applied at load time but NOT persisted back by
        ``save()`` — only the base config is written to ``config.json``.
        """
        path = config_path()
        data: dict = {}
        loaded_base = False
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
                    loaded_base = True
                else:
                    logger.warning("Config is not a JSON object, using defaults")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load config from %s: %s", path, e)

        # Deep-merge config.local.json overlay (user-owned, never touched by setup)
        local_data: dict = {}
        local_path = config_local_path()
        if local_path.is_file():
            try:
                st_mode = local_path.stat().st_mode
                if st_mode & 0o002:
                    logger.warning(
                        "config.local.json is world-writable (%o); "
                        "consider running: chmod 600 %s",
                        st_mode & 0o777,
                        local_path,
                    )
                raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(raw_local, dict):
                    local_data = raw_local
                else:
                    logger.warning("config.local.json is not a JSON object, ignoring")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load config.local.json: %s", e)

        if local_data:
            data = _deep_merge(data, local_data)

        # Return defaults only if neither file was successfully loaded. Seed
        # the default "kiroclaw" agent in-memory (matching the on-disk
        # migration below) so a never-setup home still lists the default agent
        # — but do NOT persist: a plain read (e.g. `agent list`) must not
        # create config files as a side effect.
        if not loaded_base and not local_data:
            cfg = cls()
            kiro = cfg.agent.default_agent or "kiroclaw"
            cfg.agents["default"] = KiroClawAgentConfig(
                kiro_agent=kiro,
                workspace="default",
                memory_store="default",
            )
            cfg.default_agent = "default"
            return cfg

        # Validate against JSON Schema (advisory — never fatal)
        _validate_config_data(data)

        agent_data = data.get("agent", {})
        if not isinstance(agent_data, dict):
            agent_data = {}
        session_data = data.get("session", {})
        if not isinstance(session_data, dict):
            session_data = {}
        taskrunner_data = data.get("taskrunner", {})
        cron_history_data = data.get("cron_history", {})
        if not isinstance(cron_history_data, dict):
            cron_history_data = {}
        memory_data = data.get("memory", {})
        if not isinstance(memory_data, dict):
            memory_data = {}
        slack_data = data.get("slack", {})
        if not isinstance(slack_data, dict):
            slack_data = {}
        dashboard_data = data.get("dashboard", {})
        if not isinstance(dashboard_data, dict):
            dashboard_data = {}
        stt_data = data.get("stt", {})
        if not isinstance(stt_data, dict):
            stt_data = {}
        secretary_data = data.get("secretary", {})
        if not isinstance(secretary_data, dict):
            secretary_data = {}
        taskkeeper_data = data.get("taskkeeper", {})
        if not isinstance(taskkeeper_data, dict):
            taskkeeper_data = {}
        tunnel_data = data.get("tunnel", {})
        if not isinstance(tunnel_data, dict):
            tunnel_data = {}
        skills_data = data.get("skills", {})
        if not isinstance(skills_data, dict):
            skills_data = {}

        # Parse agents section into dict[str, KiroClawAgentConfig]
        raw_agents = data.get("agents", {})
        agents: dict[str, KiroClawAgentConfig] = {}
        if isinstance(raw_agents, dict):
            for name, entry in raw_agents.items():
                if isinstance(entry, dict):
                    agents[name] = KiroClawAgentConfig(
                        kiro_agent=entry.get("kiro_agent", ""),
                        workspace=entry.get("workspace", "default"),
                        memory_store=entry.get("memory_store", "default"),
                        description=entry.get("description", ""),
                        source=entry.get("source", "kiroclaw"),
                    )

        # Migrate workspaces from flat or structured format
        raw_workspaces = data.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}
        workspaces = _migrate_workspaces(raw_workspaces)

        # Parse memory_stores; synthesize default if missing
        raw_stores = data.get("memory_stores", {})
        memory_stores: dict[str, MemoryStoreConfig] = {}
        if isinstance(raw_stores, dict) and raw_stores:
            for name, entry in raw_stores.items():
                if isinstance(entry, dict):
                    memory_stores[name] = MemoryStoreConfig(
                        description=entry.get("description", ""),
                        embedding_provider=entry.get("embedding_provider", ""),
                    )
        if not memory_stores:
            memory_stores["default"] = MemoryStoreConfig()

        # Parse top-level default_agent and default_memory_store
        default_agent_val = data.get("default_agent", "")
        if not isinstance(default_agent_val, str):
            default_agent_val = ""
        default_memory_store_val = data.get("default_memory_store", "default")
        if not isinstance(default_memory_store_val, str):
            default_memory_store_val = "default"

        cfg = cls(
            agent=AgentConfig(
                approval_mode=agent_data.get("approval_mode", "auto"),
                streaming=agent_data.get("streaming", True),
                model=agent_data.get("model", DEFAULT_MODEL),
                provider=agent_data.get("provider", "claude_code"),
                bedrock_model_id=agent_data.get("bedrock_model_id", DEFAULT_BEDROCK_MODEL),
                bedrock_region=agent_data.get("bedrock_region", DEFAULT_BEDROCK_REGION),
                default_agent=agent_data.get("default_agent", ""),
                sandbox=agent_data.get("sandbox", "auto"),
                yolo=agent_data.get("yolo", False),
                conductor_skill=agent_data.get("conductor_skill", False),
                max_subagents=agent_data.get("max_subagents", 3),
                subagent_max_turns=agent_data.get("subagent_max_turns", 100),
                subagent_timeout_secs=agent_data.get("subagent_timeout_secs", 1800),
                completion_keep=_validated_completion_keep(
                    agent_data.get("completion_keep", "head")
                ),
                completion_keep_chars=int(agent_data.get("completion_keep_chars", 3000)),
                subagent_cwd_allowed_roots=list(
                    agent_data.get(
                        "subagent_cwd_allowed_roots",
                        ["~/workspace", "~/workspaces", "~/workplace", "~/workplaces"],
                    )
                ),
                log_level=agent_data.get("log_level", "WARNING").upper(),
                enforce_denied_commands=agent_data.get("enforce_denied_commands", "all")
                .lower()
                .strip(),
                bot_name=_sanitize_bot_name(agent_data.get("bot_name", "")),
                max_channels=agent_data.get("max_channels", 1),
                max_channel_agents=agent_data.get("max_channel_agents", 3),
                soft_stop_budget_secs=max(
                    0.5, min(60.0, float(agent_data.get("soft_stop_budget_secs", 10.0)))
                ),
                cc_model=agent_data.get("cc_model", "opus-4.8-1m"),
                cc_connection_mode=agent_data.get("cc_connection_mode", "per_session"),
                cc_max_turns=int(agent_data.get("cc_max_turns", 0)),
                cc_max_budget_usd=float(agent_data.get("cc_max_budget_usd", 0.0)),
            ),
            session=SessionConfig(
                timeout_secs=session_data.get("timeout_secs", DEFAULT_SESSION_TIMEOUT),
                autocompact_pct=float(session_data.get("autocompact_pct", 90.0)),
                pool_size=int(session_data.get("pool_size", 0)),
                pool_agent=str(session_data.get("pool_agent", "")),
                pool_ttl_secs=int(session_data.get("pool_ttl_secs", 1800)),
            ),
            taskrunner=TaskRunnerConfig(
                max_parallel_steps=taskrunner_data.get(
                    "max_parallel_steps", DEFAULT_MAX_PARALLEL_STEPS
                ),
            ),
            cron_history=CronHistoryConfig(
                cron_summary_cap=int(cron_history_data.get("cron_summary_cap", 200)),
                cron_trace_cap_kb=int(cron_history_data.get("cron_trace_cap_kb", 50)),
                cron_max_records_per_job=int(
                    cron_history_data.get("cron_max_records_per_job", 100)
                ),
                cron_max_index_records=int(cron_history_data.get("cron_max_index_records", 2000)),
            ),
            memory=MemoryConfig(
                embedding_provider=memory_data.get("embedding_provider", "none"),
                embedding_url=memory_data.get("embedding_url", "http://localhost:11434"),
                allow_remote_embedding=memory_data.get("allow_remote_embedding", False),
                embedding_managed=memory_data.get("embedding_managed", True),
                embedding_auth=memory_data.get("embedding_auth", "none"),
                embedding_model=memory_data.get("embedding_model", "qwen3-embedding:0.6b"),
                embedding_dim=memory_data.get("embedding_dim", 1024),
                embedding_timeout_secs=memory_data.get("embedding_timeout_secs", 5.0),
                embedding_runtime=memory_data.get("embedding_runtime", "native"),
                semantic_confidence_threshold=memory_data.get("semantic_confidence_threshold", 0.8),
                episodic_dedup_threshold=memory_data.get("episodic_dedup_threshold", 0.88),
                episodic_max_results=memory_data.get("episodic_max_results", 8),
                episodic_max_count=memory_data.get("episodic_max_count", 10_000),
                semantic_keys=memory_data.get("semantic_keys", []),
                history_idle_hours=memory_data.get("history_idle_hours", 3.0),
                history_max_days=memory_data.get("history_max_days", 365),
                migrated=memory_data.get("migrated", False),
            ),
            slack=SlackConfig(
                allowed_users=[
                    u
                    for u in slack_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("slack_id")
                ],
                tracking_channels=_validate_tracking_channels(
                    slack_data.get("tracking_channels", [])
                ),
                open_channels=[
                    c for c in slack_data.get("open_channels", []) if isinstance(c, str)
                ],
                command=slack_data.get("command", "kiroclaw"),
                trusted_bot_ids=set(slack_data.get("trusted_bot_ids", [])),
                allowed_enterprise_ids=[
                    e
                    for e in slack_data.get("allowed_enterprise_ids", [])
                    if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
                ],
                reactions={
                    k: v
                    for k, v in slack_data.get("reactions", {}).items()
                    if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
                },
                reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
                use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
            ),
            dashboard=DashboardConfig(
                url=dashboard_data.get("url", ""),
                restore_sessions=dashboard_data.get("restore_sessions", False),
                restore_window_minutes=dashboard_data.get("restore_window_minutes", 30),
                bot_name=dashboard_data.get("bot_name", ""),
                avatar=dashboard_data.get("avatar", ""),
                merge_queued_messages=dashboard_data.get("merge_queued_messages", False),
                mcp_probe_timeout_secs=_safe_int(
                    dashboard_data.get("mcp_probe_timeout_secs", 15), 15
                ),
                auto_open_browser=dashboard_data.get("auto_open_browser", True),
                quick_send=dashboard_data.get("quick_send", False),
                widget_density=dashboard_data.get("widget_density", "more"),
                terminal=dashboard_data.get("terminal", {"enabled": False}),
                default_project=dashboard_data.get("default_project", ""),
            ),
            tunnel=TunnelConfig(
                enabled=bool(tunnel_data.get("enabled", False)),
                name_mode=str(tunnel_data.get("name_mode", "username")),
                name_override=str(tunnel_data.get("name_override", "")),
            ),
            hooks=data.get("hooks", {}),
            agents=agents,
            default_agent=default_agent_val,
            workspaces=workspaces,
            default_workspace=data.get("default_workspace", "default"),
            memory_stores=memory_stores,
            default_memory_store=default_memory_store_val,
            stt=SttConfig(
                enabled=stt_data.get("enabled", False),
                provider=_validated_stt_provider(stt_data.get("provider", "whisper")),
                whisper_path=stt_data.get("whisper_path", ""),
                # Default changed from "base" to "turbo" — turbo is faster and
                # recommended for most users (809M vs 74M, but much better latency).
                model=stt_data.get("model", "turbo"),
                device=stt_data.get("device", "cpu"),
                timeout_secs=stt_data.get("timeout_secs", 300),
                transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
                transcribe_profile=stt_data.get("transcribe_profile", ""),
                language_code=stt_data.get("language_code", "en-US"),
                streaming=stt_data.get("streaming", False),
            ),
            auto_update=data.get("auto_update", True),
            timezone=data.get("timezone", ""),
            snapshot_dir=data.get("snapshot_dir", ""),
            registries=[
                ExternalRegistryConfig(
                    name=str(r.get("name", "")),
                    repo=str(r.get("repo", "")),
                    branch=str(r.get("branch", "mainline")),
                )
                for r in (data.get("registries") or [])
                if isinstance(r, dict) and r.get("repo")
            ],
            secretary=SecretaryConfig(
                enabled=bool(secretary_data.get("enabled", False)),
                user_id=str(secretary_data.get("user_id", "")),
                watched_channels=[
                    str(c) for c in secretary_data.get("watched_channels", []) if isinstance(c, str)
                ],
                poll_interval_seconds=max(30, int(secretary_data.get("poll_interval_seconds", 60))),
                style_rules=[
                    str(r) for r in secretary_data.get("style_rules", []) if isinstance(r, str)
                ],
                alert_keywords=[
                    str(k) for k in secretary_data.get("alert_keywords", []) if isinstance(k, str)
                ],
                alert_on_name_mention=bool(secretary_data.get("alert_on_name_mention", False)),
                test_mode=bool(secretary_data.get("test_mode", False)),
                quick_reactions=[
                    str(r)
                    for r in (secretary_data.get("quick_reactions") or DEFAULT_QUICK_REACTIONS)
                    if isinstance(r, str)
                ],
                keyword_hooks=secretary_data.get("keyword_hooks") or [],
            ),
            taskkeeper=TaskKeeperConfig(
                enabled=bool(taskkeeper_data.get("enabled", False)),
                username=str(taskkeeper_data.get("username", "")),
                email_enabled=bool(taskkeeper_data.get("email_enabled", False)),
                scan_interval_seconds=max(
                    60, int(taskkeeper_data.get("scan_interval_seconds", 300))
                ),
                auto_scan_enabled=bool(taskkeeper_data.get("auto_scan_enabled", False)),
            ),
            skills=SkillsConfig(
                max_triggered=int(skills_data.get("max_triggered", 3)),
                auto_create_from_sessions=bool(skills_data.get("auto_create_from_sessions", False)),
                auto_refine_on_deviation=bool(skills_data.get("auto_refine_on_deviation", False)),
                auto_min_tool_calls=int(skills_data.get("auto_min_tool_calls", 5)),
                auto_similarity_threshold=float(skills_data.get("auto_similarity_threshold", 0.85)),
            ),
            slack_channels={
                ch_id: ChannelConfig.from_dict(ch_data)
                for ch_id, ch_data in data.get("slack", {}).get("channels", {}).items()
                if isinstance(ch_data, dict)
            },
            slack_dm_activation=_validate_activation(
                data.get("slack", {}).get("dm_activation", ACTIVATION_ALWAYS)
            ),
            observe_max_messages=max(
                1, int(data.get("slack", {}).get("observe_max_messages", 200))
            ),
            observe_ttl_hours=max(
                0.0, float(data.get("slack", {}).get("observe_ttl_hours", 168.0))
            ),
        )

        # Write-back migration: if the on-disk config has legacy format
        # (flat workspace strings, missing sections), back up the original
        # and save the migrated version.  One-shot — subsequent loads see
        # the canonical format and skip.
        try:
            needs_migration = False
            # Flat workspace strings → need migration to {"dir": ...}
            for v in raw_workspaces.values():
                if isinstance(v, str):
                    needs_migration = True
                    break

            # One-time migration: create default agent when none exists
            if not cfg.agents:
                kiro = cfg.agent.default_agent or "kiroclaw"
                cfg.agents["default"] = KiroClawAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                needs_migration = True
            if not cfg.default_agent or cfg.default_agent not in cfg.agents:
                # Prefer "default" if it exists, otherwise use first available agent
                if "default" in cfg.agents:
                    cfg.default_agent = "default"
                elif cfg.agents:
                    cfg.default_agent = next(iter(cfg.agents))
                else:
                    cfg.default_agent = "default"
                needs_migration = True

            if needs_migration:
                backup = path.with_suffix(".json.bak")
                import shutil

                shutil.copy2(path, backup)
                logger.info(
                    "Config migrated — backup saved to %s",
                    backup,
                )
                cfg.save()
        except Exception as e:
            # Migration write-back is best-effort; never block startup.
            logger.warning("Config write-back failed: %s", e)

        return cfg

    def to_dict(self) -> dict:
        """Serialize config to the JSON structure used by config.json."""
        from dataclasses import asdict

        d: dict = {
            "agent": asdict(self.agent),
            "session": asdict(self.session),
            "memory": asdict(self.memory),
            "slack": asdict(self.slack),
            "dashboard": asdict(self.dashboard),
            "hooks": self.hooks,
            "agents": {name: asdict(agent_cfg) for name, agent_cfg in self.agents.items()},
            "default_agent": self.default_agent,
            "workspaces": {name: asdict(ws_cfg) for name, ws_cfg in self.workspaces.items()},
            "default_workspace": self.default_workspace,
            "memory_stores": {name: asdict(ms_cfg) for name, ms_cfg in self.memory_stores.items()},
            "default_memory_store": self.default_memory_store,
            "stt": asdict(self.stt),
            "secretary": asdict(self.secretary),
            "taskkeeper": asdict(self.taskkeeper),
            "taskrunner": asdict(self.taskrunner),
            "orchestrator": asdict(self.orchestrator),
            "cron_history": asdict(self.cron_history),
            "skills": asdict(self.skills),
            "timezone": self.timezone,
            "auto_update": self.auto_update,
        }
        # External registries (only serialize if non-empty)
        if self.registries:
            d["registries"] = [asdict(r) for r in self.registries]
        # Preserve per-channel activation settings on round-trip
        slack_section = d.setdefault("slack", {})
        if self.slack_channels:
            slack_section["channels"] = {
                ch_id: asdict(cfg) for ch_id, cfg in self.slack_channels.items()
            }
        if self.slack_dm_activation != ACTIVATION_ALWAYS:
            slack_section["dm_activation"] = self.slack_dm_activation
        slack_section["observe_max_messages"] = self.observe_max_messages
        if self.slack.trusted_bot_ids:
            slack_section["trusted_bot_ids"] = sorted(self.slack.trusted_bot_ids)
        else:
            slack_section.pop("trusted_bot_ids", None)
        slack_section["observe_ttl_hours"] = self.observe_ttl_hours
        d["secretary"] = asdict(self.secretary)
        return d

    def save(self) -> None:
        """Write current config to ~/.kiroclaw/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.
        """

        meta = {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        }
        d = self.to_dict()

        # Strip overlay-owned values so they don't leak into config.json
        local_path = config_local_path()
        if local_path.is_file():
            try:
                raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(raw_local, dict):
                    d = _subtract_overlay(d, raw_local)
            except (json.JSONDecodeError, OSError):
                pass

        d = {"meta": meta, **d}
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults."""
        # Installed agent config (generated by kiroclaw setup)
        agent_json = Path.home() / ".kiro" / "agents" / "kiroclaw.json"
        if agent_json.is_file():
            try:
                data = json.loads(agent_json.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        # Bundled defaults.json
        bundled = Path(__file__).resolve().parent / "defaults.json"
        if bundled.is_file():
            try:
                data = json.loads(bundled.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_MODEL

    @staticmethod
    def _resolve_agent_cc_model(agent: str) -> str:
        """Resolve a custom agent's Claude Code model from its kiro agent json.

        Mirrors ``SessionManager._resolve_agent_model`` but is used by the
        claude_code provider factory: the claude backend passes neither
        ``--agent`` nor ``session/set_mode``, so (unlike kiro-cli) it cannot
        pick up a per-agent model from the agent id alone — the model must be
        resolved here and threaded into the provider.

        Prefers an explicit ``cc_model`` field, falling back to the agent's
        ``model``. Returns the RAW stored value (a canonical registry key, a kiro
        dotted id like ``claude-opus-4.6``, or an already-resolved provider id);
        the ``_claude_code`` factory translates it to a provider id exactly once
        via ``model_registry.to_provider_id`` (the translation boundary). This is
        necessary because a kiro dotted id is NOT a valid claude-agent-acp /
        Bedrock model — sent verbatim to ``set_config_option("model", …)`` the
        backend rejects the session with ``-32603`` — and the registry's alias
        map resolves it (e.g. the AIM-managed ``kiroclaw-lite`` agent's
        ``claude-sonnet-4.6`` → the Sonnet provider id). Returns ``""`` when no
        per-agent model is found so the caller can fall back to global ``cc_model``.
        """
        try:
            from kiro_claw.agent import (
                KIRO_AGENTS_DIR,  # circular import: agent imports config.loader
            )
        except Exception:
            return ""
        for af in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                ad = json.loads(af.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if ad.get("name") == agent or af.stem == agent:
                # Return the raw stored value (canonical key or provider id);
                # the factory's to_provider_id translates it at the boundary.
                return ad.get("cc_model") or ad.get("model") or ""
        return ""

    def load_credentials(self) -> dict[str, str]:
        """Load credentials from ~/.kiroclaw/.env and environment variables.

        .env format: KEY=VALUE (one per line, # comments, no quotes required).
        Environment variables override .env values.
        """
        creds: dict[str, str] = {}
        ep = env_path()
        if ep.exists():
            # Enforce restrictive permissions on credential file
            try:
                if ep.stat().st_mode & 0o077:
                    ep.chmod(0o600)
            except OSError:
                logger.warning("Cannot enforce permissions on %s", ep)
            for line in ep.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()

        for key in _CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kiroclaw/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        for k, v in creds.items():
            if v:
                os.environ.setdefault(k, v)

        return creds

    def create_provider_factory(self) -> Callable:
        """Return a factory that creates LLMProvider instances from config.

        The factory accepts an optional ``session_key`` to create a
        per-session subdirectory under ``workspace_root()``.
        """
        if self.agent.provider == "bedrock":
            from kiro_claw.providers.bedrock import BedrockProvider

            model_id = self.agent.bedrock_model_id
            region = self.agent.bedrock_region

            def _bedrock(session_key: str | None = None, **_kwargs: object) -> BedrockProvider:
                return BedrockProvider(model_id=model_id, region=region)

            return _bedrock

        if self.agent.provider == "claude_code":
            # An empty/unset cc_model must NOT be passed through as "" — the
            # claude-agent-acp adapter then falls back to its own models[0],
            # which on current builds is an OLD Opus (4.1). Resolve to the
            # KiroClaw default (canonical key) so an unconfigured user still gets
            # the intended flagship model; it is translated to a provider id at
            # the boundary below.
            from kiro_claw.providers.acp import (
                AcpProvider,  # circular: acp -> client -> session -> config.loader
            )

            cc_model = self.agent.cc_model or model_registry.default("claude_code")
            sandbox = self.agent.sandbox

            def _claude_code(
                session_key: str | None = None,
                agent: str | None = None,
                model_override: str | None = None,
                channel_id: str | None = None,
                cwd: str | None = None,
                extra_env: dict[str, str] | None = None,
                reasoning_effort_override: str | None = None,
                **_kwargs: object,
            ) -> AcpProvider:
                wdir = Path(cwd) if cwd else _session_work_dir(session_key)
                cc_env = {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                }
                # Isolate the spawned claude-agent-acp subprocess from the user's
                # global ~/.claude install (enabledPlugins → ~17x duplicated
                # builder-mcp blocks + ~80 agents + ~100 skills of base-prompt
                # bloat). CLAUDE_CONFIG_DIR points the adapter's SettingsManager
                # and the SDK at a KiroClaw-seeded dir that keeps Bedrock creds
                # (awsCredentialExport) + the 1M model allowlist but drops plugins.
                # Set BEFORE the extra_env merge so a caller-supplied override wins.
                # The same cc_config_root() value drives the resume guard and
                # cleanup (providers/cleanup.py), so transcript storage agrees.
                # circular import: cc_agent._isolated_cc_config_dir imports
                # config.loader.config_dir, so a module-level import of cc_agent
                # here would cycle (config.loader → cc_agent → config.loader).
                # Deferred to call time to break it.
                from kiro_claw import cc_agent as _cc_agent

                if _cc_agent.cc_isolation_enabled():
                    cc_env["CLAUDE_CONFIG_DIR"] = str(_cc_agent.cc_config_root())
                if extra_env:
                    cc_env.update(extra_env)
                # Effort for the claude-agent-acp backend is applied live via
                # session/set_config_option after start (see AcpProvider.start →
                # _apply_initial_effort), keyed per-model. CLAUDE_CODE_EFFORT_LEVEL
                # is NOT read by claude-agent-acp, so we thread the level as a
                # per-model override instead of an (ineffective) env var.
                #
                # Per-agent CC model: the default kiroclaw agent keeps the
                # global cc_model, but custom agents (e.g. kiroclaw-lite for
                # cheap background work — title/compaction/heartbeat) declare
                # their own model in their kiro agent json. The claude backend
                # passes neither --agent nor set_mode, so it cannot resolve a
                # per-agent model the way kiro-cli does — resolve it here.
                if model_override:
                    _cc_m = model_override
                elif agent and agent != "kiroclaw":
                    _cc_m = self._resolve_agent_cc_model(agent) or cc_model or ""
                else:
                    _cc_m = cc_model or ""
                # Translation boundary: cc_model / overrides may be a canonical
                # registry key (e.g. "opus-4.8-1m") OR an already-resolved
                # provider id. Translate to a provider id exactly here; every
                # consumer below (AcpProvider, settings.local.json) uses ids.
                _cc_m = model_registry.to_provider_id(_cc_m, "claude_code") if _cc_m else ""
                _eff_per_model: dict[str, str] = {}
                if (
                    _cc_m
                    and reasoning_effort_override
                    and is_valid_effort(reasoning_effort_override)
                    and model_supports_effort(_cc_m)
                ):
                    _eff_per_model[_cc_m] = reasoning_effort_override
                # Seed the isolated config dir before spawn so the subprocess
                # reads creds/models/deny at startup (before Bedrock cred
                # resolution). Idempotent + cheap; safe under warm-pool churn.
                # Seed the EXACT dir the child will read: extra_env may have
                # overridden CLAUDE_CONFIG_DIR above, so derive the root from the
                # final cc_env value rather than re-deriving the default — else
                # the child reads an unseeded dir.
                if _cc_agent.cc_isolation_enabled():
                    _seed_root = cc_env.get("CLAUDE_CONFIG_DIR")
                    try:
                        _cc_agent.seed_isolated_cc_config(Path(_seed_root) if _seed_root else None)
                    except Exception:
                        logger.warning("CC isolation seed failed; continuing", exc_info=True)
                return AcpProvider(
                    work_dir=wdir,
                    model=_cc_m,
                    agent=agent or "kiroclaw",
                    sandbox_mode=sandbox,
                    session_key=session_key,
                    channel_id=channel_id,
                    extra_env=cc_env,
                    acp_backend=ACP_BACKEND_CLAUDE,
                    effort_per_model=_eff_per_model,
                )

            return _claude_code

        from kiro_claw.providers.acp import (  # noqa: F811 — circular import, conditional branch
            AcpProvider,
        )

        model = self.agent.model
        if model == DEFAULT_MODEL:
            model = self._resolve_agent_model()

        sandbox = self.agent.sandbox

        def _acp(
            session_key: str | None = None,
            agent: str | None = None,
            channel_id: str | None = None,
            model_override: str | None = None,
            cwd: str | None = None,
            extra_env: dict[str, str] | None = None,
            reasoning_effort_override: str | None = None,
            **_kwargs: object,
        ) -> AcpProvider:
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Custom agents use their own model from their agent config;
            # only override model for the default kiroclaw agent.
            # If model_override is provided (from slot.model), use it.
            if model_override:
                m = model_override
            elif not agent or agent == "kiroclaw":
                m = model
            else:
                m = None
            # Thread the slot's effort into a per-model override so the kiro
            # cli.json overlay is written from it at spawn — without this, a
            # kiro cold start (or the handler's reset-then-respawn) would only
            # pick up effort already recovered from a pre-existing overlay,
            # never the freshly-set slot value. Mirrors the _claude_code path.
            _eff_per_model: dict[str, str] = {}
            if (
                m
                and reasoning_effort_override
                and is_valid_effort(reasoning_effort_override)
                and model_supports_effort(m)
            ):
                _eff_per_model[m] = reasoning_effort_override
            return AcpProvider(
                work_dir=wdir,
                model=m,
                agent=agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                effort_per_model=_eff_per_model,
            )

        return _acp


# ---------------------------------------------------------------------------
# Agent resolver and kiro agent validation
# ---------------------------------------------------------------------------


def _workspace_name_for_dir(config: KiroClawConfig, ws_dir: Path) -> str:
    """Find the workspace name whose dir matches *ws_dir*."""
    for name, ws_cfg in config.workspaces.items():
        if Path(ws_cfg.dir) == ws_dir:
            return name
    return "default"


def resolve_agent_bindings(
    config: KiroClawConfig,
    agent_name: str | None = None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution:
    1. If agent_name is given and exists in config.agents → use its bindings
    2. Otherwise use config.default_agent (guaranteed to exist by load())
    """
    import dataclasses as _dc

    # Step 1: explicit agent_name
    if agent_name and agent_name in config.agents:
        agent_cfg = config.agents[agent_name]
    elif config.default_agent and config.default_agent in config.agents:
        # Step 2: default_agent (guaranteed valid by load())
        agent_cfg = config.agents[config.default_agent]
    elif config.agents:
        # Defensive: default_agent not in agents, use first available
        first_name = next(iter(config.agents))
        logger.warning(
            "default_agent '%s' not found in agents, using '%s'",
            config.default_agent,
            first_name,
        )
        agent_cfg = config.agents[first_name]
    else:
        # No agents at all — return safe defaults
        logger.warning("No agents configured, using bare defaults")
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=config.default_memory_store,
            effective_memory_config=_dc.asdict(config.memory),
            kiro_agent=config.agent.default_agent,
        )

    # Resolve workspace
    ws_name = agent_cfg.workspace
    if ws_name in config.workspaces:
        ws_dir = Path(config.workspaces[ws_name].dir)
    else:
        logger.warning(
            "Agent workspace '%s' not found, falling back to default_workspace '%s'",
            ws_name,
            config.default_workspace,
        )
        fallback_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(fallback_ws.dir) if fallback_ws else Path("workspace")

    # Resolve memory store
    store_name = agent_cfg.memory_store
    if store_name not in config.memory_stores:
        logger.warning(
            "Agent memory_store '%s' not found, falling back to '%s'",
            store_name,
            config.default_memory_store,
        )
        store_name = config.default_memory_store

    kiro_agent = agent_cfg.kiro_agent

    # Build effective memory config via dict-level merge
    store_cfg = config.memory_stores.get(store_name)
    store_dict = _dc.asdict(store_cfg) if store_cfg else {}
    top_level_memory = _dc.asdict(config.memory)
    effective_memory = resolve_memory_store_config(top_level_memory, store_dict)

    return ResolvedBindings(
        workspace_dir=ws_dir,
        memory_store_name=store_name,
        effective_memory_config=effective_memory,
        kiro_agent=kiro_agent,
    )


def validate_kiro_agent_references(
    config: KiroClawConfig,
    installed_agents: list[str],
) -> None:
    """Cross-reference kiro_agent values against installed agents.

    Logs warnings for unresolved references. Never raises.
    """
    installed_names = set(installed_agents)
    for mc_name, mc_agent in config.agents.items():
        if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_names:
            logger.warning(
                "KiroClaw agent '%s' references kiro agent '%s' " "which is not installed",
                mc_name,
                mc_agent.kiro_agent,
            )
