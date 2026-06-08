"""Dashboard aiohttp application factory and startup."""

from __future__ import annotations

import asyncio
import errno
import importlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_claw.apps.builtins import BUILTIN_NAMES
from kiro_claw.apps.hooks_integration import (
    init_hooks_system,
    on_gateway_shutdown,
    on_gateway_startup,
)
from kiro_claw.browser.setup import (
    get_extension_token,
    has_playwright_extension,
    patch_mcp_extension,
    patch_mcp_headless,
)
from kiro_claw.config import config_dir
from kiro_claw.dashboard import (
    chat,
    handlers,
    handlers_channel,
    handlers_project,
    stt_stream,
    ws,
)
from kiro_claw.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_events,
    api_artifact_record_event,
    api_artifact_update,
    api_artifact_version_detail,
    api_artifact_versions,
    api_artifacts_create,
    api_artifacts_list,
)
from kiro_claw.dashboard.handlers.knowledge import setup_knowledge_routes
from kiro_claw.dashboard.handlers.tunnel import api_tunnel_status
from kiro_claw.dashboard.origin import bind_address_for, build_allowed_origins, check_origin
from kiro_claw.dashboard.state import _DEFAULT_PORT, DashboardState
from kiro_claw.dashboard.token_auth import token_auth_middleware
from kiro_claw.hooks import ScriptHookStore, set_global_hook_store
from kiro_claw.safety_override import safety_override
from kiro_claw.sel import sel
from kiro_claw.suggestions import api_suggestions
from kiro_claw.tunnel.setup import setup_tunnel

if TYPE_CHECKING:
    from kiro_claw.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"


def _migrate_playwright_to_proxy() -> None:
    """Migrate existing mcp.json from direct npm-playwright-mcp to proxy."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        entry = servers.get("npm:@playwright/mcp")
        if not isinstance(entry, dict):
            return
        cmd = entry.get("command", "")
        if "mcp-playwright-proxy" in cmd or "kiroclaw" in cmd:
            return
        if has_playwright_extension():
            token = get_extension_token() or ""
            if token:
                patch_mcp_extension(token)
            else:
                patch_mcp_headless()
        else:
            patch_mcp_headless()
        logger.info("Migrated Playwright MCP to proxy in mcp.json")
    except (json.JSONDecodeError, OSError):
        pass


def _precompute_telemetry(state: "DashboardState") -> None:
    """Pre-compute telemetry data (blocking I/O — call before server starts)."""
    from kiro_claw.dashboard.handlers_system import _get_owner_hash, _get_static_system_info
    _log = logging.getLogger(__name__)
    try:
        _get_owner_hash(state)
    except Exception:
        _log.warning("Failed to pre-compute owner hash", exc_info=True)
    try:
        _get_static_system_info()
    except Exception:
        _log.warning("Failed to pre-compute system info", exc_info=True)


def _register_mcp_routes(app: web.Application) -> None:
    """Register API routes used by MCP tools (spawn, lessons, crons, etc.)."""
    app.router.add_post("/api/spawn", handlers.api_spawn)
    app.router.add_get("/api/spawn", handlers.api_spawn_list)
    app.router.add_get("/api/spawn/{agent_id}", handlers.api_spawn_status)
    app.router.add_delete("/api/spawn/{agent_id}", handlers.api_spawn_delete)
    app.router.add_delete("/api/spawn", handlers.api_spawn_clear)
    app.router.add_get("/api/lessons", handlers.api_lessons)
    app.router.add_post("/api/lessons", handlers.api_lessons_create)
    app.router.add_delete("/api/lessons", handlers.api_lessons_delete)
    app.router.add_get("/api/crons", handlers.api_crons)
    app.router.add_post("/api/crons", handlers.api_crons_create)
    app.router.add_get("/api/crons/history", handlers.api_cron_history_all)
    app.router.add_delete("/api/crons/{job_id}", handlers.api_cron_delete)
    app.router.add_patch("/api/crons/{job_id}", handlers.api_cron_update)
    app.router.add_post("/api/crons/{job_id}/enable", handlers.api_cron_enable)
    app.router.add_post("/api/crons/{job_id}/run", handlers.api_cron_run)
    app.router.add_post("/api/crons/{job_id}/to-chat", handlers.api_cron_to_chat)
    app.router.add_post("/api/crons/{job_id}/ack", handlers.api_cron_ack)
    app.router.add_get("/api/crons/{job_id}/history", handlers.api_cron_history)
    app.router.add_get("/api/crons/{job_id}/history/{run_id}", handlers.api_cron_history_detail)
    app.router.add_get("/api/taskrunner", handlers.api_taskrunner_status)
    app.router.add_post("/api/taskrunner", handlers.api_taskrunner_start)
    app.router.add_post("/api/taskrunner/cancel", handlers.api_taskrunner_cancel)
    app.router.add_post("/api/send-message", handlers.api_send_message)
    app.router.add_post("/api/delete-message", handlers.api_delete_message)
    app.router.add_post("/api/browser-event", handlers.api_browser_event)
    app.router.add_post("/api/browser-auth-retry", handlers.api_browser_auth_retry)
    app.router.add_get("/api/browser/config", handlers.api_browser_config_get)
    app.router.add_put("/api/browser/config", handlers.api_browser_config_save)
    app.router.add_post("/api/session-keepalive", handlers.api_session_keepalive)
    app.router.add_get("/api/session-tool-policy", handlers.api_session_tool_policy)
    app.router.add_post("/api/slack-profile", handlers.api_slack_profile)
    app.router.add_get("/api/notifications", handlers.api_notifications)
    app.router.add_post("/api/notifications/clear", handlers.api_notifications_clear)

    # Auto-nudge (feature-flagged — returns 503 when KIROCLAW_AUTONUDGE unset)
    from kiro_claw.dashboard.handlers.autonudge import (
        api_autonudge_delete,
        api_autonudge_get,
        api_autonudge_list,
        api_autonudge_start,
        api_autonudge_update,
    )

    app.router.add_get("/api/autonudge", api_autonudge_list)
    app.router.add_post("/api/autonudge", api_autonudge_start)
    app.router.add_get("/api/autonudge/slot/{slot_key}", api_autonudge_get)
    app.router.add_patch("/api/autonudge/{loop_id}", api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", api_autonudge_delete)

    # Artifacts — persistent, versioned LLM-generated UI
    app.router.add_get("/api/artifacts", api_artifacts_list)
    app.router.add_post("/api/artifacts", api_artifacts_create)
    app.router.add_get("/api/artifacts/{slug}", api_artifact_detail)
    app.router.add_patch("/api/artifacts/{slug}", api_artifact_update)
    app.router.add_delete("/api/artifacts/{slug}", api_artifact_delete)
    app.router.add_get("/api/artifacts/{slug}/versions", api_artifact_versions)
    app.router.add_get(
        "/api/artifacts/{slug}/versions/{version}", api_artifact_version_detail
    )
    app.router.add_get("/api/artifacts/{slug}/events", api_artifact_events)
    app.router.add_post("/api/artifacts/{slug}/events", api_artifact_record_event)


async def _start_site(site: web.TCPSite, port: int) -> None:
    """Start *site*, translating EADDRINUSE into an actionable message."""
    try:
        await site.start()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            hint = (
                f"Port {port} already in use — is another KiroClaw gateway running?\n"
                f"Stop it with: kiroclaw stop  or  sudo systemctl stop kiroclaw"
            )
            logger.error(hint)
            raise SystemExit(1) from exc
        raise


def _write_secret_file(secret_path: Path, secret: str) -> None:
    """Write *secret* to *secret_path* with mode 0o600.

    On failure the (possibly truncated) file is removed and the original
    ``OSError`` is re-raised.  Caller is responsible for any further
    cleanup (e.g. tearing down the app runner).
    """
    try:
        fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # enforce perms even if file already exists
        with os.fdopen(fd, "w") as f:
            f.write(secret)
    except OSError:
        try:
            secret_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _apply_startup_yolo(state: DashboardState, cfg: Any) -> None:
    """Enable safety override at startup if ``agent.yolo=true`` in config.

    Activates with 24h TTL (no longer permanent). Re-auth required after expiry.
    """
    if not cfg.agent.yolo:
        return
    try:
        result = safety_override().activate("config")
    except Exception:
        logger.error("Failed to activate safety override from config", exc_info=True)
        return
    if not result.active:
        logger.error("Safety override activation refused (SEL audit failure?)")
        return
    logger.info(
        "Safety override enabled at startup (agent.yolo=true, expires in %ds)",
        result.ttl,
    )


async def start_dashboard(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    context_builder: ContextBuilder | None = None,
    conversation_log: ConversationLog | None = None,
    consolidator: HistoryConsolidator | None = None,
    task_runner: TaskRunner | None = None,
    slack_connected: bool = False,
    local_only: bool = True,
    configured_host: str = "",
    dashboard_url: str = "",
    slack_client: Any = None,
    owner_id: str = "",
) -> tuple[web.AppRunner, DashboardState]:
    """Start the dashboard web server.  Returns ``(runner, state)``."""
    # Auto-create consolidator if conversation_log available but no consolidator
    if consolidator is None and conversation_log is not None:
        try:
            from kiro_claw import history as _hist_mod
            from kiro_claw.memory import MemoryStore

            memory = context_builder.memory if context_builder else MemoryStore()
            if not context_builder:
                memory.init()
            consolidator = _hist_mod.HistoryConsolidator(
                log=conversation_log,
                memory=memory,
                sessions=sessions,
                lesson_store=lessons,
            )
            logger.info("Auto-created HistoryConsolidator for dashboard")
        except Exception:
            logger.debug("Could not create consolidator", exc_info=True)

    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        context_builder=context_builder,
        conversation_log=conversation_log,
        consolidator=consolidator,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
    )

    # Initialize script hook store
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers 50 MB upload + multipart overhead
    app["state"] = state
    state.load_folders()
    state.load_tags()
    app["port"] = port

    _precompute_telemetry(state)

    # MCP tool routes (shared with start_api_server)
    _register_mcp_routes(app)

    # Install persistent log ring buffer (captures logs even when Logs page is closed)
    ring_handler = handlers.install_log_ring_handler()
    if ring_handler:
        ring_handler.set_state(state)

    # Page routes
    app.router.add_get("/", handlers.index)
    app.router.add_get("/logo.png", handlers.logo)
    app.router.add_get("/{name:manifest\\.json|sw\\.js|icon-\\d+\\.png|pcm-worklet\\.js}", handlers.pwa_file)

    # WebSocket (multiplexed real-time events)
    app.router.add_get("/api/ws", ws.api_ws)

    # Status / system
    app.router.add_get("/api/status", handlers.api_status)
    app.router.add_get("/api/system", handlers.api_system)
    app.router.add_get("/api/stream", handlers.api_stream)
    app.router.add_get("/api/midway-ttl", handlers.api_midway_ttl)
    app.router.add_get("/api/dashboard/branding", handlers.api_branding)
    app.router.add_get("/api/admin/compliance/yolo-status", handlers.api_compliance_yolo_status)

    # Suggestions (pre-computed contextual prompts)
    app.router.add_get("/api/suggestions", api_suggestions)

    # Memory
    app.router.add_get("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_put("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_get("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_put("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_get("/api/memory/history", handlers.api_memory_history)
    app.router.add_put("/api/memory/history", handlers.api_memory_history)
    app.router.add_get("/api/memory/settings", handlers.api_memory_settings)
    app.router.add_put("/api/memory/settings", handlers.api_memory_settings)

    # STT (Speech-to-Text)
    app.router.add_get("/api/config/stt", handlers.api_stt_config)
    app.router.add_put("/api/config/stt", handlers.api_stt_config)
    app.router.add_post("/api/stt/install", handlers.api_stt_install)
    app.router.add_post("/api/stt/transcribe", handlers.api_stt_transcribe)
    app.router.add_get("/api/ws/stt", stt_stream.api_ws_stt)

    # Vector Memory (Semantic)
    app.router.add_get("/api/memory/semantic", handlers.api_memory_semantic)
    app.router.add_put("/api/memory/semantic", handlers.api_memory_semantic_write)
    app.router.add_delete("/api/memory/semantic/{key:.+}", handlers.api_memory_semantic_delete)
    app.router.add_get("/api/memory/events", handlers.api_memory_events)
    app.router.add_get("/api/memory/embedding-status", handlers.api_memory_embedding_status)
    app.router.add_post("/api/memory/enable-embeddings", handlers.api_memory_enable_embeddings)
    app.router.add_post("/api/memory/disable-embeddings", handlers.api_memory_disable_embeddings)
    app.router.add_get("/api/memory/episodic/search", handlers.api_memory_episodic_search)
    app.router.add_get("/api/memory/episodic", handlers.api_memory_episodic_list)
    app.router.add_delete("/api/memory/episodic/{id}", handlers.api_memory_episodic_delete)
    app.router.add_get("/api/memory/stats", handlers.api_memory_stats)
    app.router.add_post("/api/memory/migrate", handlers.api_memory_migrate)
    app.router.add_post("/api/memory/import", handlers.api_memory_import)
    app.router.add_get("/api/memory/context-preview", handlers.api_memory_context_preview)
    app.router.add_post("/api/memory/consolidate", handlers.api_memory_consolidate)
    app.router.add_get("/api/session/archive", handlers.api_session_archive_list)
    app.router.add_get("/api/session/archive/{name}", handlers.api_session_archive_read)
    app.router.add_get("/api/memory/observability", handlers.api_memory_observability)
    app.router.add_get("/api/memory/graph", handlers.api_memory_graph)
    app.router.add_post("/api/memory/promote", handlers.api_memory_promote)

    # Crons, lessons, spawn, taskrunner, send-message, notifications
    # are registered via _register_mcp_routes() above.

    # Script Hooks
    app.router.add_get("/api/hooks", handlers.api_hooks)
    app.router.add_get("/api/kiro-hooks", handlers.api_kiro_hooks)
    app.router.add_post("/api/hooks", handlers.api_hooks_create)
    app.router.add_put("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_delete("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_post("/api/hooks/{hook_id}/toggle", handlers.api_hook_toggle)
    app.router.add_post("/api/hooks/{hook_id}/test", handlers.api_hook_test)

    # Prompts (Agent SOPs)
    app.router.add_get("/api/prompts", handlers.api_prompts)
    app.router.add_get("/api/prompts/{name:.+}", handlers.api_prompt_detail)

    # Skills (CRUD + directory browser).  The browser routes use a ``/-/``
    # separator (GitLab-style) before tree/file so they can't collide with a
    # nested skill whose own last path segment is literally ``tree`` or
    # ``file`` (e.g. ``utils/tree`` → ``GET /api/skills/utils/tree`` is the
    # detail endpoint, not the browser).  They're still registered before the
    # catch-all {name:.+} so aiohttp reaches them first.
    app.router.add_get("/api/skills", handlers.api_skills)
    app.router.add_post("/api/skills", handlers.api_skills_create)
    app.router.add_get("/api/skills/{name:.+}/-/tree", handlers.api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", handlers.api_skill_file)
    app.router.add_get("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_put("/api/skills/{name:.+}", handlers.api_skill_detail)
    app.router.add_delete("/api/skills/{name:.+}", handlers.api_skill_detail)

    # Custom Themes (CRUD)
    app.router.add_get("/api/themes", handlers.api_themes)
    app.router.add_post("/api/themes", handlers.api_themes_create)
    app.router.add_get("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_put("/api/themes/{slug}", handlers.api_theme_detail)
    app.router.add_delete("/api/themes/{slug}", handlers.api_theme_detail)

    # Agent config
    app.router.add_get("/api/agent/config", handlers.api_agent_config)
    app.router.add_put("/api/agent/config", handlers.api_agent_config)
    app.router.add_get("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_put("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_get("/api/config/schema", handlers.api_config_schema)
    app.router.add_get("/api/config/kiroclaw", handlers.api_kiroclaw_config)
    app.router.add_put("/api/config/kiroclaw", handlers.api_kiroclaw_config)
    app.router.add_patch("/api/config/kiroclaw", handlers.api_kiroclaw_config_patch)
    app.router.add_get("/api/dashboard/config", handlers.api_dashboard_config)
    app.router.add_put("/api/dashboard/config", handlers.api_dashboard_config)

    # MCP servers
    app.router.add_get("/api/mcp", handlers.api_mcp_servers)
    app.router.add_get("/api/mcp/active", handlers.api_mcp_active)
    app.router.add_post("/api/mcp/probe", handlers.api_mcp_probe)
    app.router.add_get("/api/mcp/probe", handlers.api_mcp_probe_cached)
    app.router.add_post("/api/mcp/sync", handlers.api_mcp_sync)
    app.router.add_post("/api/mcp/apply", handlers.api_mcp_apply)
    app.router.add_post("/api/mcp/toggle", handlers.api_mcp_toggle)
    app.router.add_post("/api/mcp/toggle-tool", handlers.api_mcp_toggle_tool)
    app.router.add_post("/api/mcp/toggle-all", handlers.api_mcp_toggle_all)
    app.router.add_post("/api/mcp/remove", handlers.api_mcp_remove)
    # REST-style MCP server registration (App Kit)
    app.router.add_put("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    app.router.add_delete("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    # AIM integration
    app.router.add_get("/api/aim/mcp", handlers.api_aim_mcp_list)
    app.router.add_post("/api/aim/mcp/install", handlers.api_aim_mcp_install)
    app.router.add_post("/api/aim/mcp/uninstall", handlers.api_aim_mcp_uninstall)
    app.router.add_get("/api/aim/skills", handlers.api_aim_skills_list)
    app.router.add_post("/api/aim/skills/install", handlers.api_aim_skills_install)
    app.router.add_post("/api/aim/skills/uninstall", handlers.api_aim_skills_uninstall)

    # Chat
    app.router.add_post("/api/chat", chat.api_chat)
    app.router.add_get("/api/chat/slots", chat.api_chat_slots)
    app.router.add_post("/api/chat/slots", chat.api_chat_slot_create)
    app.router.add_post("/api/chat/slots/cleanup", chat.api_chat_slots_cleanup)
    app.router.add_get("/api/chat/slots/{slot}", chat.api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/stop", chat.api_chat_slot_stop)
    app.router.add_post("/api/chat/slots/{slot}/interrupt", chat.api_chat_slot_interrupt)
    app.router.add_delete("/api/chat/slots/{slot}/queue/{queue_id}", chat.api_chat_slot_queue_cancel)
    app.router.add_delete("/api/chat/slots/{slot}", chat.api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/agent", chat.api_chat_slot_agent)

    # Optimizer
    app.router.add_post("/api/optimizer/optimize", handlers.handle_optimize)
    app.router.add_post("/api/chat/slots/{slot}/model", chat.api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", chat.api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/workspace", chat.api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/project", chat.api_chat_slot_project)
    app.router.add_get("/api/recent-projects", chat.api_recent_projects)
    app.router.add_patch("/api/chat/slots/{slot}/color", chat.api_chat_slot_color)
    # Context injection (App Kit — silent background context)
    app.router.add_post("/api/chat/slots/{slot}/context", chat.api_chat_slot_context)
    app.router.add_post("/api/chat/slots/{slot}/fork", chat.api_chat_slot_fork)
    app.router.add_post("/api/chat/slots/{slot}/side/open", handlers.api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", handlers.api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", handlers.api_side_close)
    # Workspaces
    app.router.add_get("/api/workspaces", handlers.api_workspaces)
    app.router.add_post("/api/workspaces", handlers.api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", handlers.api_workspaces_update)
    app.router.add_delete("/api/workspaces/{name}", handlers.api_workspaces_delete)
    # Agents
    app.router.add_get("/api/agents/installed", handlers.api_agents_installed)
    app.router.add_get("/api/models", handlers.api_models)
    app.router.add_get("/api/effort-levels", handlers.api_effort_levels)
    app.router.add_get("/api/slash-commands", handlers.api_slash_commands)
    app.router.add_get("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_patch("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_delete("/api/agents/detail/{name}", handlers.api_agent_detail)
    # KiroClaw Agent CRUD
    app.router.add_get("/api/agents", handlers.api_kiroclaw_agents)
    app.router.add_post("/api/agents", handlers.api_kiroclaw_agents_create)
    app.router.add_post("/api/agents/sync", handlers.api_kiroclaw_agents_sync)
    app.router.add_put("/api/agents/{name}", handlers.api_kiroclaw_agent_update)
    app.router.add_delete("/api/agents/{name}", handlers.api_kiroclaw_agent_delete)
    # AIM agents
    app.router.add_get("/api/aim/agents", handlers.api_aim_agents_list)
    app.router.add_post("/api/aim/agents/install", handlers.api_aim_agents_install)
    app.router.add_post("/api/aim/agents/uninstall", handlers.api_aim_agents_uninstall)
    app.router.add_post("/api/aim/update", handlers.api_aim_update)
    # Claude Code migration (kiro → CC)
    app.router.add_get("/api/cc/mirror/preview", handlers.api_cc_mirror_preview)
    app.router.add_post("/api/cc/mirror/run", handlers.api_cc_mirror_run)
    app.router.add_get("/api/cc/aim/missing", handlers.api_cc_aim_missing)
    app.router.add_post("/api/cc/aim/sync", handlers.api_cc_aim_sync)
    # Agent metadata (Phase 1)
    app.router.add_get("/api/agent-metadata/{name}", handlers.api_agent_metadata_get)
    app.router.add_put("/api/agent-metadata/{name}", handlers.api_agent_metadata_put)
    app.router.add_delete("/api/agent-metadata/{name}", handlers.api_agent_metadata_delete)
    # Session workspace (Orchestrated Chat)
    app.router.add_get("/api/sessions/{id}/agents", handlers.api_session_agents_list)
    app.router.add_get("/api/sessions/{id}/agents/{agent_id}", handlers.api_session_agent_result)
    app.router.add_get(
        "/api/sessions/{id}/agents/{agent_id}/stream", handlers.api_session_agent_stream
    )
    app.router.add_get("/api/aim/mcp/registry", handlers.api_aim_mcp_registry)
    app.router.add_post("/api/chat/slots/{slot}/resume", chat.api_chat_slot_resume)
    app.router.add_post("/api/chat/slots/{slot}/approve", chat.api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/plan-action", chat.api_chat_plan_action)
    app.router.add_post("/api/chat/mode", chat.api_chat_mode)
    app.router.add_post("/api/chat/nav/resolve-links", chat.api_chat_nav_resolve_links)
    app.router.add_post("/api/chat/slots/{slot}/generate-title", chat.api_chat_slot_generate_title)
    app.router.add_patch("/api/chat/slots/{slot}/title", chat.api_chat_slot_rename)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", chat.api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", chat.api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", chat.api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/rewind", chat.api_chat_slot_rewind)
    # Folders
    app.router.add_get("/api/chat/folders", chat.api_chat_folders)
    app.router.add_post("/api/chat/folders", chat.api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", chat.api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", chat.api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", chat.api_chat_slot_folder)
    app.router.add_patch("/api/chat/slots/{slot}/pin", chat.api_chat_slot_pin)
    # Tags
    app.router.add_get("/api/chat/tags", chat.api_chat_tags)
    app.router.add_post("/api/chat/tags", chat.api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", chat.api_chat_tag_update)
    app.router.add_delete("/api/chat/tags/{id}", chat.api_chat_tag_delete)
    app.router.add_put("/api/chat/slots/{slot}/tags", chat.api_chat_slot_tags)
    app.router.add_post("/api/chat/slots/{slot}/drop", chat.api_chat_slot_drop)
    app.router.add_get("/api/chat/tag-columns", chat.api_chat_tag_columns)
    app.router.add_post("/api/chat/tag-columns", chat.api_chat_tag_column_create)
    app.router.add_put("/api/chat/tag-columns/order", chat.api_chat_tag_columns_reorder)
    app.router.add_patch("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_update)
    app.router.add_delete("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_delete)
    app.router.add_post("/api/voice/synthesize", chat.api_voice_synthesize)
    app.router.add_get("/api/voice/config", chat.api_voice_config)
    app.router.add_put("/api/voice/config", chat.api_voice_config)
    app.router.add_get("/api/voice/voices", chat.api_voice_voices)
    app.router.add_post("/api/chat/slots/{slot}/handoff", chat.api_chat_slot_handoff)
    app.router.add_get("/api/handoff-channels", chat.api_handoff_channels)
    app.router.add_post("/api/chat/slots/{slot}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)

    # Task runner (MCP routes via _register_mcp_routes; dashboard-only routes below)
    app.router.add_post("/api/taskrunner/plan", handlers.api_taskrunner_plan)
    app.router.add_post("/api/taskrunner/plan/cancel", handlers.api_taskrunner_plan_cancel)
    app.router.add_post("/api/taskrunner/from-chat", handlers.api_taskrunner_from_chat)
    app.router.add_delete("/api/taskrunner/{task_id}", handlers.api_taskrunner_delete)
    app.router.add_patch("/api/taskrunner/{task_id}/name", handlers.api_taskrunner_rename)
    app.router.add_patch("/api/taskrunner/{task_id}/tasks/{index}", handlers.api_taskrunner_update_task)
    app.router.add_post("/api/taskrunner/{task_id}/retry", handlers.api_taskrunner_retry)
    app.router.add_post("/api/taskrunner/{task_id}/pause", handlers.api_taskrunner_pause)
    app.router.add_post("/api/taskrunner/{task_id}/to-chat", handlers.api_taskrunner_to_chat)
    app.router.add_get(
        "/api/taskrunner/{task_id}/plan-context", handlers.api_taskrunner_plan_context
    )
    app.router.add_put("/api/taskrunner/{task_id}/plan", handlers.api_taskrunner_update_plan)
    app.router.add_post("/api/taskrunner/{task_id}/execute", handlers.api_taskrunner_execute_plan)
    app.router.add_post("/api/reveal", handlers.api_reveal_path)
    app.router.add_get("/api/file-read", handlers.api_file_read)
    app.router.add_get("/api/file-raw", handlers.api_file_raw)
    app.router.add_get("/api/file-watch", handlers.api_file_watch)
    app.router.add_post("/api/file-write", handlers.api_file_write)
    app.router.add_get("/api/file-search", handlers.api_file_search)
    app.router.add_get("/api/browse-dirs", handlers.api_browse_dirs)
    app.router.add_get("/api/browse-files", handlers.api_browse_files)
    app.router.add_post("/api/upload", handlers.api_upload)
    app.router.add_post("/api/upload/file", handlers.api_upload_file)
    app.router.add_post("/api/slack/upload-file", handlers.api_slack_upload_file)
    app.router.add_post("/api/slack/pins", handlers.api_slack_pins)
    app.router.add_post("/api/slack/reactions", handlers.api_slack_reactions)
    app.router.add_post("/api/chat/slots/{name}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)
    app.router.add_post("/api/outbox/notify", handlers.api_outbox_notify)
    app.router.add_get("/api/outbox", handlers.api_outbox_list)
    app.router.add_get("/api/outbox/{filename}", handlers.api_outbox_download)
    app.router.add_post("/api/screenshot", handlers.api_screenshot)

    # Portability (export/import config+memory as zip)
    app.router.add_get("/api/portability/export", handlers.api_portability_export)
    app.router.add_post("/api/portability/import", handlers.api_portability_import)
    app.router.add_post("/api/portability/preview", handlers.api_portability_preview)

    app.router.add_get("/api/mwinit", handlers.api_mwinit_ws)
    # Terminal (CLI panel)
    app.router.add_get("/api/ws/terminal/{session_id}", handlers.api_terminal_ws)
    app.router.add_post("/api/terminal/sessions", handlers.api_terminal_create)
    app.router.add_get("/api/terminal/sessions", handlers.api_terminal_list)
    app.router.add_delete("/api/terminal/sessions/{session_id}", handlers.api_terminal_delete)
    app.router.add_get("/api/taskrunner/refine", handlers.api_taskrunner_refine_status)
    app.router.add_post("/api/taskrunner/refine", handlers.api_taskrunner_refine)
    app.router.add_post("/api/taskrunner/refine/cancel", handlers.api_taskrunner_refine_cancel)
    app.router.add_post("/api/taskrunner/refine/answer", handlers.api_taskrunner_refine_answer)

    # Projects
    app.router.add_get("/api/projects", handlers_project.api_projects_list)
    app.router.add_get("/api/projects/{id}", handlers_project.api_project_get)
    app.router.add_post("/api/projects", handlers_project.api_project_create)
    app.router.add_put("/api/projects/{id}", handlers_project.api_project_update)
    app.router.add_delete("/api/projects/{id}", handlers_project.api_project_delete)
    app.router.add_get("/api/activities", handlers_project.api_activities_list)
    app.router.add_post("/api/comments", handlers_project.api_comment_add)
    app.router.add_get("/api/comments", handlers_project.api_comments_list)
    app.router.add_delete("/api/comments/{id}", handlers_project.api_comment_delete)

    # Channels
    app.router.add_get("/api/channels/presets", handlers_channel.api_channel_presets)
    app.router.add_get("/api/channels", handlers_channel.api_channels_list)
    app.router.add_post("/api/channels", handlers_channel.api_channel_create)
    app.router.add_get("/api/channels/{id}", handlers_channel.api_channel_get)
    app.router.add_delete("/api/channels/{id}", handlers_channel.api_channel_close)
    app.router.add_post("/api/channels/{id}/clear-context", handlers_channel.api_channel_clear_context)
    app.router.add_post("/api/channels/{id}/messages", handlers_channel.api_channel_post)
    app.router.add_post("/api/channels/{id}/agents", handlers_channel.api_channel_add_agent)
    app.router.add_patch(
        "/api/channels/{id}/agents/{aid}", handlers_channel.api_channel_update_agent
    )
    app.router.add_delete(
        "/api/channels/{id}/agents/{aid}", handlers_channel.api_channel_dismiss_agent
    )
    app.router.add_post(
        "/api/channels/{id}/agents/{aid}/wake", handlers_channel.api_channel_wake_agent
    )
    app.router.add_post(
        "/api/channels/{id}/agents/{aid}/approve", handlers_channel.api_channel_approve_agent
    )

    # Misc (notifications GET/clear and send-message via _register_mcp_routes)
    app.router.add_get("/api/notifications", handlers.api_notifications)
    app.router.add_delete("/api/notifications", handlers.api_notification_delete)
    app.router.add_post("/api/notifications/ack", handlers.api_notification_ack)
    app.router.add_post("/api/notifications/unack", handlers.api_notification_unack)
    app.router.add_post("/api/notifications/ack-all", handlers.api_notifications_ack_all)
    app.router.add_get("/api/update/check", handlers.api_update_check)
    app.router.add_get("/api/changelog", handlers.api_changelog)
    app.router.add_post("/api/update", handlers.api_update_apply)
    app.router.add_post("/api/update/auto", handlers.api_update_auto)
    app.router.add_post("/api/update/cancel", handlers.api_update_cancel)
    # Only expose the simulation endpoint in dev/debug environments
    _truthy = {"1", "true", "yes", "on"}
    if (
        os.environ.get("KIROCLAW_HOME", "").endswith("-dev")
        or os.environ.get("KIROCLAW_DEV_MODE", "").lower() in _truthy
    ):
        app.router.add_post("/api/update/simulate", handlers.api_update_simulate)
    app.router.add_get("/api/sessions", handlers.api_sessions)
    app.router.add_delete("/api/sessions", handlers.api_sessions_clear)
    app.router.add_get("/api/sessions/context", handlers.api_sessions_context)
    app.router.add_get("/api/sessions/health", handlers.api_sessions_health)
    app.router.add_get("/api/sessions/usage", handlers.api_sessions_usage)
    app.router.add_get("/api/usage/kiro", handlers.api_kiro_usage)
    app.router.add_get("/api/usage", handlers.api_usage)
    app.router.add_post("/api/sessions/restart", handlers.api_sessions_restart)
    # NOTE: /search must be registered before /{key} to avoid the path param catching "search"
    app.router.add_get("/api/sessions/search", handlers.api_sessions_search)
    app.router.add_get("/api/sessions/{key}", handlers.api_session_detail)
    app.router.add_delete("/api/sessions/{key}", handlers.api_session_delete)
    app.router.add_get("/api/logs", handlers.api_logs)
    app.router.add_get("/api/logs/level", handlers.api_log_level_get)
    app.router.add_post("/api/logs/level", handlers.api_log_level)
    app.router.add_get("/api/sel/events", handlers.api_sel_events)
    app.router.add_get("/api/sel/verify", handlers.api_sel_verify)
    app.router.add_get("/api/security/stats", handlers.api_security_stats)
    app.router.add_get("/api/approvals", handlers.api_approvals)
    app.router.add_post("/api/approvals/{id}/{action}", handlers.api_approval_resolve)

    # Local token bootstrap (file-based secret auth in handler, bypasses middleware)
    app.router.add_get("/api/token/local", handlers.api_token_local)

    # Tunnel status
    app.router.add_get("/api/tunnel/status", api_tunnel_status)

    # Session revocation (called by `kiroclaw logout` CLI)
    app.router.add_post("/api/logout", handlers.api_logout)

    # Webhook hooks (external triggers)
    app.router.add_post("/api/hooks/agent", handlers.api_hooks_agent)

    # App Platform
    from kiro_claw.apps.routes import register_app_routes
    register_app_routes(app)

    # Built-in app routes — register at startup (handlers check enabled state)
    for _builtin_name in BUILTIN_NAMES:
        try:
            _mod = importlib.import_module(f"kiro_claw.apps.builtins.{_builtin_name}")
            if hasattr(_mod, "register_routes"):
                _mod.register_routes(app)
        except ModuleNotFoundError as exc:
            if exc.name != f"kiro_claw.apps.builtins.{_builtin_name}":
                raise

    # App token exchange (App Kit §5.1 — must be before auth middleware bypass)
    app.router.add_post("/api/apps/{name}/token", handlers.api_app_token)

    # Register built-in apps (idempotent — surfaces baked-in features in App Store)
    from kiro_claw.apps.manager import register_builtin_apps
    register_builtin_apps()

    # Knowledge Library
    setup_knowledge_routes(app)

    # Start backends for enabled apps
    from kiro_claw.apps.backend import start_enabled_app_backends
    started_apps = start_enabled_app_backends()
    if started_apps:
        logger.info("Started %d app backend(s): %s", len(started_apps), ", ".join(started_apps))

    # Initialize App SDK Gateway Hooks system
    init_hooks_system(
        app,
        cron_service=state.crons,
        broadcast_fn=state.broadcast if hasattr(state, "broadcast") else None,
    )

    async def _hooks_startup(app_: web.Application) -> None:
        await on_gateway_startup(
            cron_service=state.crons,
            broadcast_fn=state.broadcast if hasattr(state, "broadcast") else None,
        )

    app.on_startup.append(_hooks_startup)

    async def _ensure_playwright(app_: web.Application) -> None:
        """Migrate any existing Playwright MCP config to the proxy (background task).

        The OSS build ships no bundled Playwright MCP installer, so there is no
        unconditional install step — we only migrate pre-existing mcp.json entries.
        """
        async def _bg_migrate() -> None:
            try:
                await asyncio.to_thread(_migrate_playwright_to_proxy)
            except Exception as exc:
                logger.debug("Playwright proxy migration skipped: %s", exc)

        task = asyncio.create_task(_bg_migrate())
        app_.setdefault("_bg_tasks", set()).add(task)
        task.add_done_callback(lambda t: app_.get("_bg_tasks", set()).discard(t))

    app.on_startup.append(_ensure_playwright)

    async def _hooks_shutdown(app_: web.Application) -> None:
        await on_gateway_shutdown()

    app.on_cleanup.append(_hooks_shutdown)

    # Static files — prefer React dist/ build, fall back to legacy static/
    if _DIST_DIR.is_dir():
        app.router.add_static(
            "/assets",
            _DIST_DIR / "assets" if (_DIST_DIR / "assets").is_dir() else _DIST_DIR,
            show_index=False,
            append_version=True,
        )
        if (_DIST_DIR / "sprites").is_dir():
            app.router.add_static("/sprites", _DIST_DIR / "sprites", show_index=False)
        # Vendor shims for the app import map (react, react-dom, react/jsx-runtime)
        if (_DIST_DIR / "vendor").is_dir():
            app.router.add_static(
                "/vendor",
                _DIST_DIR / "vendor",
                show_index=False,
                append_version=False,  # stable URLs, no cache-busting
            )
        logger.info("Serving React build from %s", _DIST_DIR)
    if _STATIC_DIR.is_dir():
        app.router.add_static(
            "/static",
            _STATIC_DIR,
            show_index=False,
            append_version=True,
        )
    else:
        logger.warning("Static dir not found: %s", _STATIC_DIR)

    # ── Middleware ────────────────────────────────────────────────────────────

    # No-cache: prevents Chrome from caching stale assets
    @web.middleware  # type: ignore[misc]
    async def no_cache_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        if hasattr(resp, "headers"):
            resp.headers.setdefault(
                "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
            )
            resp.headers.setdefault("Pragma", "no-cache")
            resp.headers.setdefault("Expires", "0")
            # CSP: defense-in-depth against XSS. Restricts script sources to
            # CSP: defense-in-depth layer. Primary XSS protection is rehypeSanitize
            # (strips script/iframe/form/foreignObject at HAST level before rendering).
            # CSP must allow 'unsafe-inline' because widget iframes (blob: sandbox)
            # inherit parent CSP per W3C spec — inline scripts in widgets need it.
            # Widget isolation is enforced by sandbox="allow-scripts" (no parent DOM
            # access) + widget-level CSP meta (connect-src 'none').
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' "
                "https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; "
                "font-src 'self' data:; "
                "connect-src 'self' ws://localhost:* ws://127.0.0.1:*; "
                "media-src 'self' blob:; "
                "frame-src 'self' blob:; "
                "object-src 'none'; base-uri 'self'",
            )
        return resp  # type: ignore[return-value]

    # SPA fallback: serve index.html for client-side React Router paths
    @web.middleware  # type: ignore[misc]
    async def spa_fallback(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        try:
            return await handler(request)  # type: ignore[operator]
        except web.HTTPNotFound:
            if request.method == "GET" and not request.path.startswith(
                ("/api/", "/assets/", "/static/", "/sprites/", "/vendor/")
            ):
                return await handlers.index(request)
            raise

    # CSRF: block state-mutating requests from cross-origin pages
    _safe_methods = {"GET", "HEAD", "OPTIONS"}

    # SEL: log mutating API operations
    _sel_log_methods = {"POST", "PUT", "DELETE", "PATCH"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_log_methods and request.path.startswith("/api/"):
            from kiro_claw.sel import sel

            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller="dashboard_user",
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller="dashboard_user",
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    app["allowed_origins"] = build_allowed_origins(port, local_only, configured_host)

    @web.middleware  # type: ignore[misc]
    async def csrf_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method not in _safe_methods:
            if not check_origin(request, require=True, fallback_header="Referer"):
                raise web.HTTPForbidden(
                    text="CSRF check failed: request origin not allowed.",
                    content_type="text/plain",
                )
        return await handler(request)  # type: ignore[operator]

    # Generate per-session secret for local app / IPC authentication.
    # NOTE: file write deferred until after port bind succeeds to avoid
    # poisoning the secret file when a second instance fails to start.
    _secret_path = config_dir() / ".local_secret"
    _secret_path.parent.mkdir(parents=True, exist_ok=True)
    _internal_secret = os.urandom(16).hex()
    app["local_secret"] = _internal_secret

    # Explicit middleware ordering — self-documenting and immune to future insertions
    app.middlewares[:] = [
        no_cache_middleware,
        csrf_middleware,
        token_auth_middleware(
            internal_paths=frozenset(
                {
                    "/api/send-message",
                    "/api/delete-message",
                    "/api/browser-event",
                    "/api/session-keepalive",
                    "/api/session-tool-policy",
                    "/api/hooks/agent",
                    "/api/outbox/notify",
                    "/api/slack/upload-file",
                    "/api/slack/pins",
                    "/api/slack/reactions",
                    "/api/mcp/servers",
                }
            ),
            mixed_internal_paths=frozenset(
                {
                    # Called by MCP (loopback + secret) AND browser polling
                    # (DCV/SSH-forwarded cookie auth).  See token_auth.py.
                    "/api/spawn",
                    "/api/chat",
                    "/api/lessons",
                    "/api/crons",  # CLI cron trigger; prefix covers all sub-routes (consistent with spawn/taskrunner)
                    "/api/taskrunner",
                    "/api/artifacts",
                }
            ),
            internal_secret=_internal_secret,
            port=port,
            local_only=local_only,
        ),
        sel_audit_middleware,
        spa_fallback,
    ]

    # Verify security invariant: if dashboard_url expands the CSRF origin
    # set for a remote URL, token auth middleware MUST be active.
    if dashboard_url:
        _has_token_auth = any(
            getattr(mw, "_is_token_auth", False) for mw in app.middlewares
        )
        if _has_token_auth:
            app["allowed_origins"] = build_allowed_origins(
                port, local_only, configured_host, dashboard_url
            )
            logger.info(
                "dashboard_url=%s: added to CSRF allowed origins (token auth verified)",
                dashboard_url,
            )
        else:
            logger.error(
                "dashboard_url=%s requires token auth — refusing to start without it. "
                "Enable Slack or remove dashboard.url from config.",
                dashboard_url,
            )
            raise RuntimeError("dashboard_url requires token auth middleware")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind_address_for(local_only), port)
    await _start_site(site, port)

    # Port bind succeeded — now safe to write the secret file
    try:
        _write_secret_file(_secret_path, _internal_secret)
    except OSError:
        await runner.cleanup()
        raise

    # Fire background MCP probe at startup (non-blocking)
    asyncio.create_task(handlers._bg_mcp_probe())

    # Start terminal orphan reaper (kills PTYs with no WS for >5 min)
    _reaper = asyncio.create_task(handlers.reap_orphaned_terminals(app))
    _reaper.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_reaper = _reaper  # prevent GC

    # Start periodic flush loop for crash protection (saves dirty slots every 5s)
    state.start_flush_loop()

    # Restore sessions — always restore foldered/pinned sessions; optionally restore recent ones.
    # NOTE: Even with restore_sessions=false, foldered and pinned sessions are restored
    # so the Explorer tree stays populated.  Users can unpin or remove from folder to dismiss.
    from kiro_claw.config.loader import KiroClawConfig

    cfg = KiroClawConfig.load()
    _apply_startup_yolo(state, cfg)

    # Wire safety override expiry notifications
    async def _notify_slack_override_expired() -> None:
        """Post override expiry notice to Slack owner DM."""
        try:
            slack_client = state.slack_client
            owner_id = state.owner_id
            if slack_client and owner_id:
                dm_channel = await slack_client.open_dm(owner_id)
                await slack_client.post_message(
                    dm_channel,
                    "\U0001f512 Safety override expired. Tools now require approval. Reply `/kiroclaw yolo` to re-authorize.",
                )
        except Exception:
            logger.debug("Slack expiry notification skipped", exc_info=True)

    def _on_override_expired(source: str) -> None:
        """Notify all interfaces when safety override expires."""
        state.broadcast_ws("yolo_expired", {"source": source})
        state.push_slots_update()
        if state.sessions is not None:
            for slot in state._slots.values():
                if not slot._trust and not slot._trust_reads:
                    state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
        # Slack cleanup — isolated so failures don't block dashboard operations
        try:
            from kiro_claw.slack.handler import (
                _trusted_sessions,  # circular import: server.py is imported by slack/gateway.py which imports handler.py
            )
            _trusted_sessions.clear()
        except Exception:
            logger.debug("Could not clear trusted sessions", exc_info=True)
        # Slack notification (prevent GC with background_tasks set)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_notify_slack_override_expired())
            state._background_tasks.add(task)
            task.add_done_callback(state._background_tasks.discard)
        except RuntimeError:
            logger.debug("No running event loop — Slack expiry notification skipped")

    safety_override().on_expired = _on_override_expired

    restored = chat.restore_recent_sessions(
        state,
        cfg.dashboard.restore_window_minutes if cfg.dashboard.restore_sessions else 0,
        folders_only=not cfg.dashboard.restore_sessions,
    )
    if restored:
        logger.info("Restored %d session(s)", restored)

    # Relaunch agents in non-archived channels
    from kiro_claw.channel import ChannelManager, run_channel_agent
    from kiro_claw.dashboard.handlers_channel import _spawn_agent_task

    mgr = ChannelManager(
        broadcast_fn=state.broadcast_ws,
        max_channels=cfg.agent.max_channels,
        max_agents=cfg.agent.max_channel_agents,
    )
    state.channel_manager = mgr
    for ch in mgr._channels.values():
        for agent in ch.members.values():
            agent.state = "pending"
            _spawn_agent_task(
                agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo)
            )

    # ── AEA Tunnel ───────────────────────────────────────────────────────────
    logger.debug("Tunnel config: enabled=%s", cfg.tunnel.enabled)
    if cfg.tunnel.enabled:
        tunnel_mgr = await setup_tunnel(
            middlewares=list(app.middlewares),
            allowed_origins=app["allowed_origins"],
            tunnel_name_mode=cfg.tunnel.name_mode,
            tunnel_name_override=cfg.tunnel.name_override,
            port=port,
            log_api_access=sel().log_api_access,
        )
        if tunnel_mgr:
            state.tunnel_manager = tunnel_mgr

    return runner, state


async def start_api_server(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    slack_client: Any = None,
    owner_id: str = "",
) -> tuple[web.AppRunner, DashboardState]:
    """Start a minimal API-only server for MCP tool transport (no UI)."""
    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
    )
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers 50 MB upload + multipart overhead
    app["state"] = state
    state.load_folders()
    state.load_tags()
    app["port"] = port

    _precompute_telemetry(state)

    # SEL audit middleware — log mutating MCP tool calls
    _sel_methods = {"GET", "POST", "PUT", "DELETE"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_methods and request.path.startswith("/api/"):
            from kiro_claw.sel import sel

            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller="mcp_tool",
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller="mcp_tool",
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    app.middlewares.append(sel_audit_middleware)

    _register_mcp_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await _start_site(site, port)
    logger.info("API-only server listening on 127.0.0.1:%d", port)

    return runner, state
