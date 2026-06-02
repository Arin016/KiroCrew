"""Cron job and Lessons CRUD API handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import available_timezones

from aiohttp import web

from kiro_claw.dashboard.cron_inject import (
    hydrate_slot_from_history,
    inject_cron_result_to_dashboard,
)
from kiro_claw.dashboard.state import DashboardState
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.validation import CHANNEL_ID_RE, CHANNEL_MAX_LEN

from ._shared import (
    _blocks_reads_session,
    _get_active_workspace,
    _get_lessons,
    _get_memory,
    _is_restricted_session,
    _session_has_persisted_history,
)

logger = logging.getLogger(__name__)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_claw.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


# ── Cron / Lessons ──


async def api_crons_create(request: web.Request) -> web.Response:
    """POST /api/crons — create a cron job."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "").strip()
    message = body.get("message", "").strip()
    schedule = body.get("schedule", "").strip()
    if not name or not message:
        return web.json_response({"error": "name and message required"}, status=400)
    every = body.get("every")
    cron_expr = body.get("cron")
    if not every and not cron_expr and schedule:
        # Treat schedule string as cron expr if 5-field, else as interval
        cron_expr = schedule if len(schedule.split()) == 5 else None
    channel = body.get("channel", "").strip() or None
    if channel and (len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel)):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    approval_mode = body.get("approval_mode", "").strip()
    if approval_mode and approval_mode not in {"", "auto"}:
        return web.json_response({"error": "invalid approval_mode"}, status=400)
    silent = body.get("silent", False)
    timezone_val = (body.get("timezone") or "").strip()
    if timezone_val and timezone_val not in available_timezones():
        safe_tz, _ = redact_credentials(redact_exfiltration_urls(timezone_val)[0])
        return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
    if every:
        try:
            every = int(every)
        except (ValueError, TypeError):
            return web.json_response({"error": "'every' must be an integer"}, status=400)
        job = state.crons.add_job(name, message, every_secs=every, channel=channel)
    elif cron_expr:
        job = state.crons.add_job(name, message, cron_expr=cron_expr, channel=channel)
    else:
        return web.json_response({"error": "schedule, every, or cron required"}, status=400)
    agent_id = (body.get("agent") or "").strip()
    strict_schedule = body.get("strict_schedule", False)
    if agent_id or approval_mode or silent or timezone_val or strict_schedule:
        if agent_id:
            job.agent_id = agent_id
        if approval_mode:
            job.approval_mode = approval_mode
        if silent:
            job.silent = True
        if timezone_val:
            job.timezone = timezone_val
        if strict_schedule:
            job.strict_schedule = True
        state.crons._save()
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_delete(request: web.Request) -> web.Response:
    """DELETE /api/crons/{id} — remove a cron job."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    ok = state.crons.remove_job(job_id)
    if ok:
        await state.crons.get_history().delete_job_history(job_id)
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


async def api_cron_update(request: web.Request) -> web.Response:
    """PATCH /api/crons/{id} — update a cron job (partial)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    kwargs: dict[str, Any] = {}
    for key in ("name", "message", "channel", "approval_mode", "silent", "strict_schedule"):
        if key in body:
            kwargs[key] = body[key]
    # UI sends "agent"; internal kwarg is "agent_id". Accept "agent_id" for scripted callers.
    # Normalize whitespace and coerce null so update and create persist the same value.
    if "agent" in body:
        kwargs["agent_id"] = (body["agent"] or "").strip()
    elif "agent_id" in body:
        kwargs["agent_id"] = (body["agent_id"] or "").strip()
    # Validate channel if being updated
    if "channel" in kwargs:
        ch = (kwargs["channel"] or "").strip() or None
        kwargs["channel"] = ch
        if ch and (len(ch) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(ch)):
            return web.json_response({"error": "invalid channel ID format"}, status=400)
    # Schedule: accept cron_expr or every (seconds)
    if "cron" in body:
        kwargs["cron_expr"] = body["cron"]
    if "every" in body:
        kwargs["every_secs"] = body["every"]
    if "timezone" in body:
        tz_val = (body["timezone"] or "").strip()
        if tz_val and tz_val not in available_timezones():
            safe_tz, _ = redact_credentials(redact_exfiltration_urls(tz_val)[0])
            return web.json_response({"error": f"invalid timezone: {safe_tz!r}"}, status=400)
        kwargs["timezone"] = tz_val
    if not kwargs:
        return web.json_response({"error": "no fields to update"}, status=400)
    try:
        job = state.crons.update_job(job_id, **kwargs)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    state.push_refresh("crons")
    return web.json_response({"ok": True, "id": job.id})


async def api_cron_run(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/run — trigger immediate execution."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)
    task = asyncio.create_task(state.crons.run_job(job_id))  # type: ignore[arg-type]
    state.crons._running_tasks[job_id] = task  # type: ignore[assignment]

    def _on_done(t: asyncio.Task, _jid: str = job_id) -> None:  # type: ignore[type-arg]
        if state.crons._running_tasks.get(_jid) is t:
            state.crons._running_tasks.pop(_jid, None)

    task.add_done_callback(_on_done)
    state.push_refresh("crons")
    safe_name = redact_credentials(redact_exfiltration_urls(job.name)[0])[0]
    return web.json_response({"ok": True, "name": safe_name})


async def api_cron_to_chat(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/to-chat — open last result in a chat session."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    slot_name = f"cron-{job_id}"
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == job_id), None)
    if job:
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, f"cron:{job.id}")
            if state.conversation_log else []
        )
        inject_cron_result_to_dashboard(state, job, job.last_result or "", history=history)
    else:
        # Job deleted (one-shot with delete_after_run). Create slot from history or notification.
        session_key = f"cron:{job_id}"
        history = (
            await asyncio.to_thread(state.conversation_log.read_messages, session_key)
            if state.conversation_log else []
        )
        if history:
            slot = state.get_or_create_slot(name=slot_name, agent="")
            if not slot.linked_session_key:
                slot.linked_session_key = session_key
                hydrate_slot_from_history(slot, history)
        else:
            # No session log — fall back to notification body.
            notif = next(
                (n for n in state._notification_log if n.get("job_id") == job_id),
                None,
            )
            if not notif:
                return web.json_response({"error": "job not found"}, status=404)
            slot = state.get_or_create_slot(name=slot_name, agent="")
            body = notif.get("body", "")
            if body:
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)
                if not any(message.get("content") == body for message in slot.messages):
                    slot.append("assistant", body, "msg msg-a")
        state.push_slots_update()
    return web.json_response({"ok": True, "slot": slot_name})


async def api_cron_enable(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/enable — toggle enable/disable."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = body.get("enabled", True)
    ok = state.crons.enable_job(job_id, enabled=enabled)
    if ok:
        state.push_refresh("crons")
    return web.json_response({"ok": ok})


async def api_cron_ack(request: web.Request) -> web.Response:
    """POST /api/crons/{id}/ack — acknowledge a cron notification."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    summary = body.get("summary", "acknowledged")
    notification_ts = body.get("ts", "")
    ok = state.crons.ack_job(job_id, summary)
    if notification_ts:
        state.ack_notification(notification_ts)
    return web.json_response({"ok": ok})


async def api_cron_history(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history — paginated execution history (no trace)."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    runs, total = await state.crons.get_history().get_job_history(job_id, limit=limit, offset=offset)
    for run in runs:
        for key in ("summary", "error"):
            if run.get(key):
                run[key] = redact_credentials(redact_exfiltration_urls(run[key])[0])[0]
    return web.json_response({"runs": runs, "total": total})


async def api_cron_history_detail(request: web.Request) -> web.Response:
    """GET /api/crons/{id}/history/{run_id} — full run detail with trace."""
    state: DashboardState = request.app["state"]
    job_id = request.match_info["job_id"]
    run_id = request.match_info["run_id"]
    detail = await state.crons.get_history().get_run_detail(job_id, run_id)
    if not detail:
        return web.json_response({"error": "run not found"}, status=404)
    for key in ("summary", "trace", "error"):
        if detail.get(key):
            detail[key] = redact_credentials(redact_exfiltration_urls(detail[key])[0])[0]
    return web.json_response(detail)


async def api_cron_history_all(request: web.Request) -> web.Response:
    """GET /api/crons/history — unified history across all jobs, enriched with job_name."""
    state: DashboardState = request.app["state"]
    job_id = request.query.get("job_id")
    try:
        limit = int(request.query.get("limit", "20"))
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0
    runs, total = await state.crons.get_history().get_all_history(
        job_id=job_id, limit=limit, offset=offset
    )
    # Enrich with job_name
    jobs_by_id = {j.id: j for j in state.crons.list_jobs(include_disabled=True)}
    for run in runs:
        jid = run.get("job_id", "")
        job = jobs_by_id.get(jid)
        run["job_name"] = job.name if job else jid
        for key in ("job_name", "summary", "trace", "error"):
            if run.get(key):
                run[key] = redact_credentials(redact_exfiltration_urls(run[key])[0])[0]
    return web.json_response({"runs": runs, "total": total})


async def api_lessons_create(request: web.Request) -> web.Response:
    """POST /api/lessons — add a lesson (vector store or JSONL fallback)."""
    from kiro_claw.learn import Lesson  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Block lesson writes from restricted (incognito/temporary/guest) sessions.
    sk = request.headers.get("X-Session-Key", "")
    if not sk:
        _sel().log_api_access(
            caller="anonymous", operation="learn_add", outcome="denied",
            source="dashboard", resources="missing_session_key",
        )
        return web.json_response({"error": "missing X-Session-Key"}, status=400)
    if sk != "dashboard:ui":
        slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
        in_slots = slot_name in state._slots
        in_restricted = sk in state._restricted_keys
        is_slack_ns = sk.startswith("slack:")
        # Only consult the on-disk JSONL when the cheaper in-memory
        # checks all fail. ``_session_has_persisted_history()`` performs
        # synchronous filesystem I/O (up to two ``Path.exists()`` calls),
        # so evaluating it eagerly on every ``learn_add`` request would
        # block the event loop on the common (live-slot) path. Deferring
        # it keeps the fallback semantics identical while making the
        # happy path allocation-free.
        if not (in_slots or in_restricted or is_slack_ns):
            if not _session_has_persisted_history(slot_name):
                # Slot may have been evicted from memory (idle sweep,
                # gateway restart) while the MCP subprocess keeps its
                # original KIROCLAW_SESSION_KEY env var. Ephemeral
                # (incognito/temporary) sessions never write JSONL, so
                # the absence of a session JSONL here means the key
                # genuinely does not belong to any established session.
                _sel().log_api_access(
                    caller=sk, operation="learn_add", outcome="denied",
                    source="dashboard", resources="unknown_session",
                )
                return web.json_response({"error": "unknown session"}, status=400)
            # JSONL-fallback is the sole reason the call is permitted.
            # Audit it as an allow decision so session-recovery
            # authorization is traceable alongside the deny path above.
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="jsonl_fallback_recovery",
            )
        elif in_slots:
            # Live in-memory slot — the common happy path. Audit so that
            # every ``learn_add`` permission decision on this branch is
            # traceable (security-controls rule).
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="live_slot",
            )
        elif in_restricted:
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="restricted_key",
            )
        else:  # is_slack_ns
            _sel().log_api_access(
                caller=sk, operation="learn_add", outcome="allowed",
                source="dashboard", resources="slack_namespace",
            )
    else:
        # Browser UI's static key — implicitly trusted, but the allow
        # decision itself is still an authorization outcome and must be
        # audited (security-controls rule: every permission decision
        # emits a SEL event).
        _sel().log_api_access(
            caller=sk, operation="learn_add", outcome="allowed",
            source="dashboard", resources="dashboard_ui",
        )
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        logger.warning("Blocked learn_add from restricted session %s", sk)
        _sel().log_api_access(
            caller=sk,
            operation="learn_add",
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Memory writes are not allowed in this session mode.",
        )
        return web.json_response(
            {"error": "Memory writes are not allowed in this session mode."},
            status=403,
        )
    rule = body.get("rule", "").strip()
    if not rule:
        return web.json_response({"error": "rule is required"}, status=400)
    category = body.get("category", "knowledge")
    scope = body.get("scope", "global")
    # Write to vector store if available, else JSONL
    vs = _get_memory(state).vector_store
    if vs:
        vs.write_lesson(rule, category)
    else:
        lesson = Lesson(rule=rule, category=category, ts=datetime.now(timezone.utc).isoformat())
        if scope == "workspace":
            ws = body.get("workspace")
            _get_lessons(state, ws).save(lesson)
        else:
            state.lessons.save(lesson)
    state.push_refresh("lessons")
    return web.json_response({"ok": True})


async def api_lessons_delete(request: web.Request) -> web.Response:
    """DELETE /api/lessons — remove lessons by substring."""
    state: DashboardState = request.app["state"]
    # Block lesson deletes from temporary sessions only.
    # Incognito allows learn_remove (active user action).
    if _blocks_reads_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="lessons.delete", outcome="denied",
            source="dashboard", resources=sk,
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    rule_sub = body.get("rule", "").strip()
    if not rule_sub:
        return web.json_response({"error": "rule substring required"}, status=400)
    scope = body.get("scope", "global")
    # Delete from vector store if active, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = vs.get_lessons() if vs else None
    if vs_lessons:
        ok = vs.delete_lesson(rule_sub)
    else:
        if scope == "workspace":
            ws = body.get("workspace")
            ok = _get_lessons(state, ws).remove(rule_sub)
        else:
            ok = state.lessons.remove(rule_sub)
    if ok:
        state.push_refresh("lessons")
    return web.json_response({"ok": ok})


async def api_crons(request: web.Request) -> web.Response:
    from kiro_claw.cron import compute_next_run_ts, format_schedule, get_local_tz  # noqa: F811

    state: DashboardState = request.app["state"]
    jobs = state.crons.list_jobs(include_disabled=True)
    now = time.time()
    tz_name, _ = get_local_tz()
    data = [
        {
            "id": j.id,
            "name": redact_credentials(redact_exfiltration_urls(j.name)[0])[0],
            "message": redact_credentials(redact_exfiltration_urls(j.message)[0])[0],
            "enabled": j.enabled,
            "schedule": redact_credentials(redact_exfiltration_urls(format_schedule(j.schedule, tz_name=j.timezone or tz_name))[0])[0],
            "cron_expr": j.schedule.cron_expr if j.schedule.kind == "cron" else None,
            "every_secs": j.schedule.every_secs if j.schedule.kind == "every" else None,
            "created_ts": j.created_ts or None,
            "last_status": j.last_status,
            "agent": redact_credentials(redact_exfiltration_urls(j.agent_id or "")[0])[0] or None,
            "channel": redact_credentials(redact_exfiltration_urls(j.channel or "")[0])[0] or None,
            "approval_mode": redact_credentials(redact_exfiltration_urls(j.approval_mode or "")[0])[
                0
            ]
            or None,
            "silent": j.silent,
            "strict_schedule": j.strict_schedule,
            "last_run_ts": j.last_run_ts,
            "has_result": bool(j.last_result),
            "has_slot": state.has_slot(f"cron-{j.id}"),
            "next_run_ts": compute_next_run_ts(j, now=now),
            "timezone": redact_credentials(redact_exfiltration_urls(j.timezone or "")[0])[0] or None,
            "skip_dates": (
                [redact_credentials(redact_exfiltration_urls(d)[0])[0] for d in j.skip_dates]
                if j.skip_dates
                else None
            ),
            "script": redact_credentials(redact_exfiltration_urls(j.script or "")[0])[0] or None,
            "command": redact_credentials(redact_exfiltration_urls(j.command or "")[0])[0] or None,
            "last_result": redact_credentials(redact_exfiltration_urls(j.last_result or "")[0])[0] or None,
            "last_error": redact_credentials(redact_exfiltration_urls(j.last_error or "")[0])[0] or None,
            "is_running": state.crons.is_running(j.id),
            "running_since": state.crons.running_since(j.id),
        }
        for j in jobs
    ]
    return web.json_response(
        {
            "jobs": data,
            "server_tz": redact_credentials(redact_exfiltration_urls(tz_name or "")[0])[0] or None,
        }
    )


async def api_lessons(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    # Block lesson reads only for temporary sessions (blocks_reads=True).
    # Incognito sessions can read lessons (memory context is already injected).
    if _blocks_reads_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="lessons.list", outcome="denied",
            source="dashboard", resources=sk,
        )
        return web.json_response({"lessons": []})
    workspace = request.query.get("workspace")
    # Read from vector store if it has lessons, else JSONL
    vs = _get_memory(state).vector_store
    vs_lessons = vs.get_lessons() if vs else None
    if vs_lessons:
        data = []
        for e in vs_lessons[-50:]:
            try:
                rule = json.loads(e["value_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            data.append({"rule": rule, "category": "knowledge", "ts": e.get("updated_at", "")})
    else:
        # Merge global + workspace-scoped lessons
        global_lessons = state.lessons.load_all()
        ws = workspace or _get_active_workspace(state)
        if ws != "default":
            ws_lessons = _get_lessons(state, ws).load_all()
            seen = {le.rule.lower().strip() for le in global_lessons}
            for le in ws_lessons:
                if le.rule.lower().strip() not in seen:
                    global_lessons.append(le)
        data = [
            {"rule": le.rule, "category": le.category, "ts": le.ts} for le in global_lessons[-50:]
        ]
    return web.json_response({"lessons": data})
