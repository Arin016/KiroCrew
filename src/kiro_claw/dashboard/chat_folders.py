"""Folder management — CRUD, pin, assignment, icon generation."""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
import uuid

from aiohttp import web

from kiro_claw.dashboard.chat_persistence import _save_slot_to_history
from kiro_claw.dashboard.state import DashboardState
from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_claw.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel
from kiro_claw.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

_folder_icon_lock = asyncio.Lock()


def _is_single_emoji(s: str) -> bool:
    """True if `s` is exactly one emoji grapheme (no letters/digits/text).

    Accepts simple emoji, variation-selector / skin-tone modified emoji, ZWJ
    sequences (families, professions), and two-codepoint flag pairs. Rejects
    empty strings, plain text, and multiple emoji.
    """
    if not s or len(s) > 16:
        return False
    modifiers = {0xFE0F, 0x200D}  # variation selector-16, zero-width joiner

    def _emoji_char(c: str) -> bool:
        o = ord(c)
        return (
            unicodedata.category(c).startswith("So")  # symbol, other
            or o > 0x1F000                            # supplementary emoji planes
            or o in modifiers
            or 0x1F3FB <= o <= 0x1F3FF                 # skin-tone modifiers
            or 0x1F1E6 <= o <= 0x1F1FF                 # regional indicators (flags)
        )

    if not all(_emoji_char(c) for c in s):
        return False
    # Count grapheme clusters; must be exactly one.
    cps = [ord(c) for c in s]
    n = len(cps)
    clusters = 0
    i = 0
    while i < n:
        if 0x1F1E6 <= cps[i] <= 0x1F1FF:  # flag = pair of regional indicators
            clusters += 1
            i += 2 if (i + 1 < n and 0x1F1E6 <= cps[i + 1] <= 0x1F1FF) else 1
        else:
            clusters += 1  # base emoji, then absorb modifiers / ZWJ-joined emoji
            i += 1
            while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                i += 1
            while i < n and cps[i] == 0x200D:  # ZWJ joins the following emoji
                i += 2 if i + 1 < n else 1
                while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                    i += 1
        if clusters > 1:
            return False
    return clusters == 1


async def _generate_folder_icon(state: DashboardState, folder: dict) -> None:
    """Background task: ask LLM for a single emoji for the folder name.

    Serialized via a module-level lock so concurrent folder creations don't
    interleave streams on the shared BACKGROUND_KEY session.
    """

    prompt = (
        f"Reply with exactly ONE emoji that best represents a project folder named \"{folder['name']}\". "
        "No text, no explanation, just the single emoji character."
    )

    async def _stream(client) -> str:  # type: ignore[no-untyped-def]
        t = ""
        async for event in client.stream(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                t += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await client.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break
        return t

    text = ""
    async with _folder_icon_lock:
        client, _is_new, _resumed = await state.sessions.get_or_create(BACKGROUND_KEY)
        try:
            text = await asyncio.wait_for(_stream(client), timeout=30)
        except Exception:  # noqa: BLE001 — best-effort background task
            text = ""
        finally:
            state.sessions.release(BACKGROUND_KEY)
            # Recycle the shared BG session if it's accumulated too much
            # context. See chat_title.py for the full rationale.
            await state.sessions.recycle_background()
    icon = text.strip()
    icon, _ = redact_exfiltration_urls(icon)
    icon, _ = redact_credentials(icon)
    # Validate: must be exactly one emoji (guard against stray LLM text).
    if _is_single_emoji(icon):
        if any(f["id"] == folder["id"] for f in state._folders):
            folder["icon"] = icon
            state.save_folders()
            state.push_slots_update()


async def api_chat_folders(request: web.Request) -> web.Response:
    """GET /api/chat/folders — list all project folders."""
    state: DashboardState = request.app["state"]
    return web.json_response(state._folders)


def _validate_project_dir(raw: str) -> tuple[str, str | None]:
    """Validate and normalize project_dir. Returns (resolved_path, error_msg)."""
    if not raw:
        return "", None
    if not os.path.isabs(raw) and not raw.startswith("~"):
        return "", "project_dir must be an absolute path"
    resolved = os.path.realpath(os.path.expanduser(raw))
    if is_sensitive_path(resolved):
        sel().log_api_access(
            caller="dashboard", operation="chat.folder_project_dir",
            outcome="denied", resources=resolved, error="sensitive path",
        )
        return "", "project_dir refers to a sensitive path"
    if not os.path.isdir(resolved):
        return "", "project_dir must be an existing directory"
    return resolved, None


async def api_chat_folder_create(request: web.Request) -> web.Response:
    """POST /api/chat/folders — create a project folder."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = (body.get("name") or "").strip()[:100]
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    parent_id = str(body.get("parent_id") or "")
    if parent_id and not any(f["id"] == parent_id for f in state._folders):
        return web.json_response({"error": "parent folder not found"}, status=400)
    project_dir = str(body.get("project_dir") or "").strip()
    project_dir, err = _validate_project_dir(project_dir)
    if err:
        return web.json_response({"error": err}, status=400)
    folder = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "order": len(state._folders),
        "collapsed": False,
        "parent_id": parent_id,
        "project_dir": project_dir,
    }
    state._folders.append(folder)
    state.save_folders()
    state.push_slots_update()
    # Generate icon in background — don't block the response
    task = asyncio.ensure_future(_generate_folder_icon(state, folder))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_create",
        outcome="allowed", source="dashboard", resources=str(folder["id"]),
    )
    return web.json_response(folder, status=201)


async def api_chat_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/folders/{id} — rename or reorder a folder."""
    state: DashboardState = request.app["state"]
    fid = request.match_info["id"]
    folder = next((f for f in state._folders if f["id"] == fid), None)
    if not folder:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if "name" in body:
        new_name = str(body["name"]).strip()[:100]
        if not new_name:
            return web.json_response({"error": "name required"}, status=400)
        folder["name"] = new_name
    if "collapsed" in body:
        folder["collapsed"] = bool(body["collapsed"])
    if "order" in body:
        folder["order"] = int(body["order"])
    if "default_agent" in body:
        val = body["default_agent"]
        folder["default_agent"] = str(val).strip() if val is not None else ""
    if "project_dir" in body:
        pd, err = _validate_project_dir(str(body["project_dir"] or "").strip())
        if err:
            return web.json_response({"error": err}, status=400)
        folder["project_dir"] = pd
    if "icon" in body and body.get("regenerate_icon"):
        # Mutually exclusive: a manual icon would be saved and returned, then the
        # background regeneration would silently overwrite it. Reject the
        # ambiguous request so the conflict is explicit to the caller.
        return web.json_response(
            {"error": "Cannot set icon and regenerate_icon in the same request"},
            status=400,
        )
    if "icon" in body:
        # User-chosen emoji. None or empty string clears to the default;
        # otherwise it must be exactly one emoji (no text / multiple emoji).
        raw_icon = body["icon"]
        icon_val = str(raw_icon).strip() if raw_icon is not None else ""
        if icon_val and not _is_single_emoji(icon_val):
            return web.json_response({"error": "icon must be a single emoji"}, status=400)
        folder["icon"] = icon_val[:16]
    if body.get("regenerate_icon"):
        # "Reset to auto" — re-run the LLM emoji generator in the background.
        # _generate_folder_icon saves + pushes a slots update on success.
        task = asyncio.create_task(_generate_folder_icon(state, folder))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    state.save_folders()
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_update",
        outcome="allowed", source="dashboard", resources=fid,
    )
    return web.json_response(folder)


async def api_chat_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/folders/{id} — delete a folder, ungroup its slots."""

    state: DashboardState = request.app["state"]
    fid = request.match_info["id"]
    if not any(f["id"] == fid for f in state._folders):
        return web.json_response({"error": "not found"}, status=404)
    for f in state._folders:
        if f.get("parent_id") == fid:
            f["parent_id"] = ""
    state._folders = [f for f in state._folders if f["id"] != fid]
    for slot in state._slots.values():
        if slot.folder_id == fid:
            slot.folder_id = ""
            _save_slot_to_history(state, slot, force=True)
    state.save_folders()
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_delete",
        outcome="allowed", source="dashboard", resources=fid,
    )
    return web.json_response({"ok": True})


async def api_chat_slot_folder(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/folder — assign slot to a folder."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response({"error": "folder not found"}, status=400)
    if folder_id != slot.folder_id:
        slot._folder_changed = True  # re-inject [FOLDER] breadcrumb on next turn
    slot.folder_id = folder_id
    _save_slot_to_history(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_folder",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "folder_id": slot.folder_id})


async def api_chat_slot_pin(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/pin — toggle pinned state."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slot.pinned = bool(body.get("pinned", False))
    _save_slot_to_history(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_pin",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "pinned": slot.pinned})
