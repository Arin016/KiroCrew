"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import get_instance as _autonudge_get
from kiro_crew.autonudge import is_channel_key

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    resolve_stop_sentinel,
)
from kiro_crew.autonudge_grant import (
    AUTHORIZED_WINDOWS,
    authorize_run,
    release_run_grant,
    run_grant_scope,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.safety_override import safety_override
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def render_nudge_message(message: str, stop_sentinel_path: str | None) -> str:
    """Replace {{STOP_FILE}} template with the resolved sentinel path."""
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


def _serialize(loop: Any) -> dict:
    """The loop plus how long its per-run auto-approve window still has to run.

    Derived here rather than stored on the loop: the grant lives in
    ``SafetyOverride`` and expires on a clock, so a copy persisted alongside the
    loop would be stale the moment it was written. ``0`` means no window -- the
    UI needs "not authorized" and "authorized, 3h left" to be the same field so
    it cannot render a stale offer next to a live grant.
    """
    data = asdict(loop)
    try:
        data["auto_approve_remaining_secs"] = safety_override().scope_remaining_secs(
            run_grant_scope(loop.slot_key)
        )
    except Exception:
        logger.error("Run-grant read failed for %s", loop.slot_key, exc_info=True)
        data["auto_approve_remaining_secs"] = 0
    data["auto_approve_windows"] = list(AUTHORIZED_WINDOWS)
    return data


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?, stop_sentinel_path? }
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # idle_secs/max_cycles/max_runtime_secs come straight from the request
    # body: int() raises ValueError on "abc", TypeError on null/list, and
    # OverflowError on float("inf") (1e309 is legal JSON in aiohttp's parser),
    # any of which would surface as a 500 instead of a 400. Non-integral
    # floats are rejected rather than silently truncated (int(1.5) -> 1 would
    # store a value the caller never asked for). Coerce up front and reject
    # bad input, matching the sibling handlers_instances.api_instances_add
    # guard on the same pattern.
    try:
        for _name in ("idle_secs", "max_cycles", "max_runtime_secs"):
            _val = body.get(_name)
            if isinstance(_val, float) and not _val.is_integer():
                return web.json_response(
                    {"error": f"{_name} must be a whole number", "code": "not_a_whole_number"},
                    status=400,
                )
        idle_secs = int(body.get("idle_secs", 60))
        max_cycles = int(body.get("max_cycles", 0))
        max_runtime_secs = int(body.get("max_runtime_secs", 0))
    except (TypeError, ValueError, OverflowError):
        return web.json_response(
            {"error": "idle_secs, max_cycles and max_runtime_secs must be integers"}, status=400
        )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=(body.get("session_key") or body.get("slot_key") or ""),
        message=(body.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path=(body.get("stop_sentinel_path") or ""),
        max_runtime_secs=max_runtime_secs,
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active.

    Thin HTTP mapping over ``authorize_and_update_nudge``, which owns the
    message redaction, the integer coercion, and the audit-or-deny policy — see
    its docstring for why those live in the transport-agnostic module and not
    here.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
        max_runtime_secs=body.get("max_runtime_secs"),
        source="dashboard",
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})


async def api_autonudge_authorize(request: web.Request) -> web.Response:
    """POST /api/autonudge/{loop_id}/authorize — grant this run auto-approval.

    The authorizing act is THIS request. The window is not accepted from the
    agent that armed the loop and is not derived from telemetry: it is one of the
    offered values, chosen by an operator whose dashboard credential is on the
    request. That is the whole security argument for the feature -- an agent
    cannot reach this endpoint on its own behalf, so it cannot lengthen its own
    authority, and a grant therefore always has a human behind it.

    Owner-gated for the same reason the tool-approval round-trip is: a non-owner
    holding a dashboard session may watch a run, but may not widen what it is
    allowed to do without asking.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {"error": "auto-nudge disabled", "code": "autonudge_disabled"}, status=503
        )
    if not is_owner_dashboard_request(request):
        sel().log_api_access(
            caller="dashboard",
            operation="autonudge.run_authorize",
            outcome="denied",
            source="dashboard",
            resources="reason:not_owner",
        )
        return web.json_response(
            {"error": "owner only", "code": "not_owner"}, status=403
        )
    loop_id = request.match_info["loop_id"]
    try:
        body = await request.json()
    except Exception:
        # `code` is the machine-readable contract; a bare sentence is
        # untranslatable by construction (RFC 9457 3.1.3).
        return web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        # A non-object body (`[]`, `"x"`, `7200`) would make `.get` raise and turn
        # a malformed request into a 500.
        return web.json_response(
            {"error": "body must be a JSON object", "code": "bad_body"}, status=400
        )
    window_secs = body.get("window_secs")
    # Require the value to ALREADY be an exact integer rather than coercing it.
    # `int(7200.5)` is 7200, which is an offered window -- so coercion would hand
    # out a real grant for a value the offered-set check was supposed to reject,
    # defeating the closed-set property this endpoint exists to hold. `bool` is a
    # subclass of `int`, so it is excluded explicitly.
    if isinstance(window_secs, bool) or not isinstance(window_secs, int):
        return web.json_response(
            {
                "error": "window_secs must be one of the offered integers",
                "code": "bad_window",
                "offered": list(AUTHORIZED_WINDOWS),
            },
            status=400,
        )

    # Looked up through the public listing rather than a private dict: the
    # service exposes no by-id getter, and a `hasattr` probe for one would turn
    # a missing method into a permanent 404 instead of a failure anyone notices.
    loop = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    if loop is None:
        return web.json_response(
            {"error": "loop not found", "code": "not_found"}, status=404
        )
    if not getattr(loop, "active", False):
        # Authorizing a stopped run would grant a window nothing consumes, and
        # the release path has already run for it -- so nothing would hand this
        # one back.
        return web.json_response(
            {"error": "loop is not running", "code": "loop_inactive"}, status=409
        )
    if is_channel_key(loop.slot_key):
        # Only the dashboard approval path consults the run scope. A Slack or
        # Discord loop would take the grant and still stall on every approval, so
        # succeeding here would be a promise the system cannot keep -- worse than
        # refusing, because the operator would walk away believing it worked.
        return web.json_response(
            {
                "error": "auto-approve for a run is dashboard-only for now",
                "code": "channel_loop_unsupported",
            },
            status=409,
        )

    # activate_scoped() audits fail-closed with a synchronous SEL filesystem
    # write; on a slow disk that would stall every task on the event loop, so it
    # runs on a thread.
    granted = await asyncio.to_thread(
        authorize_run, loop.slot_key, window_secs, source="dashboard"
    )
    if granted:
        # The offload opens a window in which the run can stop, and the release
        # path may have already run before this grant existed -- which would leave
        # a window nothing hands back. Re-read the loop and compensate rather than
        # holding the service lock across the audit I/O.
        still = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
        if still is None or not getattr(still, "active", False):
            release_run_grant(loop.slot_key, reason="stopped_during_authorize")
            return web.json_response(
                {"error": "loop is not running", "code": "loop_inactive"}, status=409
            )
    if not granted:
        return web.json_response(
            {
                "error": "window not offered",
                "code": "window_not_offered",
                "offered": list(AUTHORIZED_WINDOWS),
            },
            status=400,
        )
    return web.json_response(
        {"ok": True, "loop_id": loop_id, "window_secs": window_secs}
    )


async def api_autonudge_revoke(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id}/authorize — hand the window back early.

    Revoking only ever moves the deadline EARLIER, so it is safe in a way granting
    is not: there is no input that widens authority here. That asymmetry is why it
    is allowed for a loop in any state and is idempotent — refusing a revoke would
    leave authority in place that an operator asked to drop, which is the worse
    outcome. Without it the only way to shed a mis-clicked window is to stop the
    run, i.e. destroy the work to reduce the grant.

    Still owner-gated: a non-owner cancelling someone else's authorization is a
    denial-of-service on the run, even though it cannot escalate anything.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {"error": "auto-nudge disabled", "code": "autonudge_disabled"}, status=503
        )
    if not is_owner_dashboard_request(request):
        sel().log_api_access(
            caller="dashboard",
            operation="autonudge.run_revoke",
            outcome="denied",
            source="dashboard",
            resources="reason:not_owner",
        )
        return web.json_response(
            {"error": "owner only", "code": "not_owner"}, status=403
        )
    loop_id = request.match_info["loop_id"]
    loop = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    if loop is None:
        return web.json_response(
            {"error": "loop not found", "code": "not_found"}, status=404
        )
    # deactivate_scope writes to the SEL, so keep that filesystem I/O off the loop.
    await asyncio.to_thread(release_run_grant, loop.slot_key, reason="revoked")
    return web.json_response({"ok": True, "loop_id": loop_id})
