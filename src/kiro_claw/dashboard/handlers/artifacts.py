"""Artifacts HTTP handlers — REST endpoints over :class:`ArtifactStore`.

Endpoints
---------
- ``GET    /api/artifacts``                    list (filter by ?tag, ?kind, ?q)
- ``POST   /api/artifacts``                    create (JSON body)
- ``GET    /api/artifacts/{slug}``             read current version
- ``PATCH  /api/artifacts/{slug}``             update (content/name/description/tags)
- ``DELETE /api/artifacts/{slug}``             delete
- ``GET    /api/artifacts/{slug}/versions``    list version numbers
- ``GET    /api/artifacts/{slug}/versions/{n}``  read a specific version
- ``GET    /api/artifacts/{slug}/events``      lifecycle event log

Authorization
~~~~~~~~~~~~~
Standard dashboard auth (token middleware). Restricted sessions cannot mutate
artifacts; reads are allowed so the agent can iterate from a hook callback.

The HTTP layer is the single source of truth for SEL audit events on artifact
mutations — MCP tools and the CLI both go through here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from kiro_claw import sel as _sel_mod
from kiro_claw.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_claw.dashboard.handlers._shared import _is_restricted_session
from kiro_claw.security import redact_credentials, redact_exfiltration_urls


def sel():
    """Late-resolved sel() — calls the module function so test patching of
    ``kiro_claw.sel.sel`` (the canonical patch target) continues to work."""
    return _sel_mod.sel()


logger = logging.getLogger(__name__)


# Maximum size of an artifact create/update request body (bytes). The store
# itself enforces a stricter content cap; this layer caps the JSON envelope.
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Read a JSON body, capped at ``_MAX_BODY_BYTES``."""
    raw = await request.read()
    if len(raw) > _MAX_BODY_BYTES:
        raise ArtifactValidationError(f"request body exceeds {_MAX_BODY_BYTES} bytes")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ArtifactValidationError("request body must be a JSON object")
    return body


def _session_key(request: web.Request) -> str:
    return request.headers.get("X-Session-Key") or ""


def _audit(
    *,
    tool: str,
    request: web.Request,
    outcome: str,
    extra: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    try:
        sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome=outcome,
            error=error or "",
            metadata=extra or {},
        )
    except Exception:  # pragma: no cover — audit must never break a request
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


def _serialize(art: Any, *, include_content: bool = False) -> dict[str, Any]:
    """Serialize an Artifact for response.

    All LLM-originated string fields (``name``, ``description``, ``tags``,
    and — when ``include_content=True`` — ``content``) pass through
    ``redact_exfiltration_urls()`` + ``redact_credentials()`` per
    AUTOSDE.yaml's ``security-controls`` rule. Artifact metadata is set
    by the agent via ``artifact_save`` / ``artifact_update``, so any
    field originating in LLM output must not reach the dashboard surface
    unredacted.
    """
    out = art.to_dict(include_content=include_content)
    for key in ("name", "description"):
        val = out.get(key)
        if isinstance(val, str) and val:
            cleaned, _ = redact_exfiltration_urls(val)
            cleaned, _ = redact_credentials(cleaned)
            out[key] = cleaned
    if isinstance(out.get("tags"), list):
        out["tags"] = [_redact_text(t) if isinstance(t, str) else t for t in out["tags"]]
    if include_content and out.get("content"):
        cleaned = out["content"]
        cleaned, _ = redact_exfiltration_urls(cleaned)
        cleaned, _ = redact_credentials(cleaned)
        out["content"] = cleaned
    return out


def _redact_text(text: str) -> str:
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


# ── List / Create ─────────────────────────────────────────────────────────────


async def api_artifacts_list(request: web.Request) -> web.Response:
    tag = request.query.get("tag") or None
    kind = request.query.get("kind") or None
    q = request.query.get("q") or None
    source = request.query.get("source") or None
    source_path = request.query.get("source_path") or None
    try:
        items = get_default_store().list(
            tag=tag,
            kind=kind,
            name_contains=q,
            source=source,
            source_path=source_path,
        )
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact list failed: %s", exc)
        return _err(str(exc), status=500)
    return _json_response({"artifacts": [_serialize(a) for a in items]})


async def api_artifacts_create(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create artifacts", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # ── Auto-dedup by source_path (Mesh-1654 Phase 6) ─────────────────────
    # When the caller passes a source_path that matches an existing artifact,
    # silently bump the existing one to a new version rather than creating a
    # parallel duplicate. This makes the "Add to artifacts" action on file
    # paths idempotent — clicking it twice on the same file just produces v2,
    # not two separate artifacts. Returns 200 OK on bump (vs 201 Created on
    # genuine new save) so the caller can distinguish if it cares.
    source_path = body.get("source_path") or ""
    if isinstance(source_path, str) and source_path:
        store = get_default_store()
        try:
            existing = store.find_by_source_path(source_path)
        except (ArtifactError, OSError) as exc:
            # find_by_source_path scans meta.json files; on a corrupt store
            # we fall through to the regular create path rather than
            # blocking the save.
            logger.warning("source_path lookup failed: %s", exc)
            existing = None
        if existing is not None:
            # Same auth-based actor inference as api_artifact_update — if the
            # caller is MCP (X-Internal-Secret header), the lifecycle event
            # gets tagged 'iterated' (agent), not 'edited' (user). Without
            # this, MCP-driven re-saves would silently misattribute on the
            # activity timeline.
            is_mcp = request.headers.get("X-Internal-Secret") is not None
            actor = "agent" if is_mcp else "user"
            try:
                art = store.update(
                    existing.slug,
                    content=body.get("content"),
                    actor=actor,
                    snapshot=True,
                )
            except ArtifactValidationError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc))
            except ArtifactError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="error",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc), status=500)
            _audit(
                tool="artifact_save",
                request=request,
                outcome="success",
                extra={
                    "slug": art.slug,
                    "kind": art.kind,
                    "version": art.version,
                    "deduped": True,
                },
            )
            # 200 OK signals "bumped existing"; the create path below returns 201.
            return _json_response(_serialize(art, include_content=True), status=200)
    try:
        art = get_default_store().create(
            name=body.get("name", ""),
            content=body.get("content", ""),
            slug=body.get("slug"),
            kind=body.get("kind", "widget"),
            source=body.get("source", "chat"),
            description=body.get("description", ""),
            tags=body.get("tags") or [],
            source_path=body.get("source_path", ""),
        )
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactAlreadyExistsError as exc:
        # Explicit slug collision — semantically a 409 Conflict (the resource
        # already exists). Distinct from base ArtifactError fallback below
        # which catches store-level refusals (sensitive-path, write failure)
        # and returns 500.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc), status=409)
    except ArtifactError as exc:
        # Base-class fallback — store._write_text() can raise ArtifactError
        # ("refusing to write sensitive path: ...") after the duplicate-slug
        # check passes. Returning 409 there would be wrong; this is a server
        # error, not a conflict. Mirrors the pattern in api_artifact_update
        # and api_artifact_delete.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": body.get("slug", "")},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_save",
        request=request,
        outcome="success",
        extra={"slug": art.slug, "kind": art.kind, "version": art.version},
    )
    return _json_response(_serialize(art, include_content=True), status=201)


# ── Item: read / update / delete ──────────────────────────────────────────────


async def api_artifact_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_update(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot update artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    try:
        # Determine actor authoritatively from how the request was authed,
        # NOT from the body. MCP-originated calls carry X-Internal-Secret
        # (validated by upstream middleware before we see them); browser
        # dashboard calls don't. Tagging by auth method is both more
        # accurate (the agent's MCP layer doesn't have to remember to set
        # actor='agent') and more secure (a body field could be spoofed).
        is_mcp = request.headers.get("X-Internal-Secret") is not None
        actor = "agent" if is_mcp else "user"
        # Session correlation: MCP calls carry X-Session-Key with a real
        # chat-slot key; the dashboard's browser client sets it to the
        # literal "dashboard:ui" for every request (see api/client.ts) which
        # is NOT a slot the user can navigate to. Drop it so the activity
        # timeline doesn't render a broken "from session dashboard:ui" link.
        session_id_hdr = request.headers.get("X-Session-Key")
        if session_id_hdr == "dashboard:ui":
            session_id_hdr = None
        # Snapshot semantics (Mesh-1654 round 5): saves don't bump version
        # by default — that's the user's "save while editing" path. Agent
        # updates via MCP always snapshot (each iteration is a meaningful
        # state change worth versioning, like a git commit). The dashboard
        # can also explicitly request a snapshot via ``snapshot: true`` in
        # the body (the "Snapshot" button next to Save).
        raw_snapshot = body.get("snapshot")
        if raw_snapshot is None:
            snapshot = is_mcp  # MCP defaults to True; dashboard defaults to False.
        else:
            snapshot = bool(raw_snapshot)
        # event_type / from_version overrides — used by the revert flow to
        # mark its update as ``reverted`` (with the source version pinned)
        # rather than the default ``edited``. Validation lives in
        # store.update() — invalid values raise ArtifactValidationError →
        # 400 below. Reverts always snapshot regardless of the snapshot
        # flag because the entire point is to record the rollback.
        raw_event_type = body.get("event_type")
        event_type = raw_event_type if isinstance(raw_event_type, str) and raw_event_type else None
        if event_type == "reverted":
            snapshot = True
        raw_from_version = body.get("from_version")
        try:
            from_version = int(raw_from_version) if raw_from_version is not None else None
        except (TypeError, ValueError):
            from_version = None
        art = get_default_store().update(
            slug,
            content=body.get("content"),
            description=body.get("description"),
            tags=body.get("tags"),
            name=body.get("name"),
            actor=actor,
            session_id=session_id_hdr,
            event_type=event_type,
            from_version=from_version,
            snapshot=snapshot,
        )
        # store.update() only loads content into the returned Artifact when
        # the caller passed new content (because that path is on the write
        # branch of the store). For metadata-only updates the returned
        # Artifact has content=None, which then serializes as "content": null
        # in the response — inconsistent with api_artifact_detail which
        # always returns the actual content. Refetch in that case so the MCP
        # tool / dashboard caller always sees a populated content field.
        if art.content is None:
            art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Catches the base class fallback — store._write_text() raises
        # ArtifactError("refusing to write sensitive path: ...") which is
        # neither ArtifactNotFoundError nor ArtifactValidationError. Without this
        # branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_update",
        request=request,
        outcome="success",
        extra={"slug": art.slug, "version": art.version},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_delete(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot delete artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        get_default_store().delete(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Base-class fallback — defends against any ArtifactError subclass
        # not specifically handled above (e.g. future store-level errors).
        # Without this branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_delete",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response({"ok": True})


# ── Versions ─────────────────────────────────────────────────────────────────


async def api_artifact_versions(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        versions = get_default_store().list_versions(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": slug, "versions": versions})


async def api_artifact_version_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    version_str = request.match_info.get("version", "")
    try:
        version = int(version_str)
    except ValueError:
        return _err(f"invalid version: {version_str}")
    try:
        art = get_default_store().get(slug, version=version)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True))


# ── Lifecycle events ─────────────────────────────────────────────────────────


async def api_artifact_events(request: web.Request) -> web.Response:
    """Return the lifecycle event log for an artifact.

    Triggers the lazy backfill in ``store.get`` for legacy artifacts that
    pre-date the events field, so the activity timeline is never empty for
    a real artifact (the fallback synthesizes ``created`` / ``edited`` from
    ``created_at`` / ``updated_at``).
    """
    slug = request.match_info.get("slug", "")
    try:
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": art.slug, "events": list(art.events)})


async def api_artifact_record_event(request: web.Request) -> web.Response:
    """Record an impression-style lifecycle event without modifying content.

    Currently only ``referenced`` events go through this endpoint —
    ``WidgetFrame`` posts here when each chat impression mounts so the
    activity timeline can show "this artifact was referenced N times
    across M sessions". Other event types (``created``, ``edited``,
    ``iterated``, ``reverted``) are emitted internally by the store as a
    side effect of the corresponding mutation; only ``referenced`` is a
    pure annotation that doesn't change content/version, which is why it
    needs a dedicated endpoint.

    Auth: same X-Internal-Secret + X-Session-Key model as the rest of
    the artifacts API. Browser-originated requests get ``by='user'``;
    MCP-originated requests get ``by='agent'``. Session ID is taken
    from the X-Session-Key header (with the literal ``dashboard:ui``
    dropped — same rule as other handlers).

    Appending events mutates ``meta.json``, so this is gated behind the
    same deny-by-default ``_is_restricted_session`` check as the other
    mutation endpoints — a restricted session must not be able to flood
    an artifact's event log.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reference",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot record artifact events", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    event_type = body.get("type")
    # Restrict to ``referenced`` for now — the other event types must
    # come from the mutation paths so version-bump bookkeeping and
    # snapshot creation stay coupled to actual content changes.
    # Callers passing anything else are likely confused; reject loudly.
    if event_type != "referenced":
        return _err(
            "this endpoint only accepts type='referenced'; "
            "use POST /api/artifacts (create), PATCH /api/artifacts/{slug} "
            "(update / iterate / revert) for content-mutating events"
        )
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    session_id_hdr = request.headers.get("X-Session-Key")
    if session_id_hdr == "dashboard:ui":
        session_id_hdr = None
    raw_metadata = body.get("metadata") or {}
    if not isinstance(raw_metadata, dict):
        return _err("metadata must be an object")
    message_ts = raw_metadata.get("message_ts")
    widget_index = raw_metadata.get("widget_index")
    # Light type coercion at the boundary — store-side _append_event
    # also defends, but failing fast with a clear 400 is friendlier
    # than a silent metadata drop.
    if message_ts is not None and not isinstance(message_ts, str):
        return _err("metadata.message_ts must be a string")
    if widget_index is not None and not isinstance(widget_index, int):
        return _err("metadata.widget_index must be an integer")
    try:
        art, appended = get_default_store().record_impression(
            slug,
            by=actor,
            session_id=session_id_hdr,
            message_ts=message_ts,
            widget_index=widget_index,
        )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    except (ArtifactError, OSError) as exc:
        logger.warning("record_impression failed for %s: %s", slug, exc)
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_reference",
        request=request,
        outcome="ok",
        extra={"slug": art.slug, "suppressed": not appended},
    )
    # When the impression was suppressed (the session already has a CUD
    # event on this artifact) no `referenced` event was appended, so
    # `art.events[-1]` would be an unrelated prior event. Signal the
    # suppression explicitly rather than echoing a misleading payload.
    if not appended:
        return _json_response({"slug": art.slug, "event": None, "suppressed": True})
    # Return only the latest event entry — the full event log can be
    # fetched via the GET endpoint. Keeps this response small for the
    # high-frequency impression-logging case.
    latest = art.events[-1] if art.events else None
    return _json_response({"slug": art.slug, "event": latest})
