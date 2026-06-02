"""Navigation panel — LLM-powered link summary resolution."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from aiohttp import web

from kiro_claw.dashboard.state import DashboardState
from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel
from kiro_claw.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

_LINK_SUMMARY_PROMPT = (
    "You are a link labeling agent. Given a list of URLs with their surrounding context from a chat conversation, "
    "generate a short descriptive label (3-8 words) for each URL.\n\n"
    "Rules:\n"
    "- Output one label per line, in the same order as the input\n"
    "- Each line should be ONLY the label text, nothing else\n"
    "- Be concise: 'Memory V2 Design Doc' not 'A document about the Memory V2 design'\n"
    "- For CRs: include the feature name, e.g. 'Nav panel link labels CR'\n"
    "- For tickets: include the topic, e.g. 'OmniScan Cognex deployment'\n"
    "- If context is insufficient, use the URL type + ID as label, e.g. 'Doc dVbcAXW3'\n\n"
    "{items}"
)


def _safe_url_for_prompt(url: str) -> str:
    """Strip query/fragment from URL before feeding to LLM to prevent prompt injection."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _build_link_summary_prompt(links: list[dict]) -> str:
    """Build prompt for batch link summary generation."""
    items: list[str] = []
    for i, link in enumerate(links):
        url = _safe_url_for_prompt(link.get("url", "")[:500])
        ctx = link.get("context", "").strip()[:300]
        ctx_part = f"\n  Context: {ctx}" if ctx else ""
        items.append(f"{i + 1}. URL: {url}{ctx_part}")
    return _LINK_SUMMARY_PROMPT.format(items="\n".join(items))


async def _resolve_link_summaries(state: DashboardState, links: list[dict]) -> list[str]:
    """Generate summaries for a batch of links using the background session."""
    prompt = _build_link_summary_prompt(links)
    client, _is_new, _resumed = await state.sessions.get_or_create(BACKGROUND_KEY)
    text = ""
    try:
        async for event in client.stream(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await client.reject_tool(event.request_id)
                sel().log_tool_invocation(
                    session_key=BACKGROUND_KEY, tool_name="unknown", outcome="denied",
                    source="chat_nav", request_id=str(event.request_id),
                )
            elif event.kind == EVENT_COMPLETE:
                break
    finally:
        state.sessions.release(BACKGROUND_KEY)

    # Parse: one label per line
    lines = [re.sub(r'^\d{1,2}[.)]\s+', '', ln.strip()) for ln in text.strip().splitlines() if ln.strip()]
    # Redact each label
    results: list[str] = []
    for ln in lines:
        ln, redacted_url = redact_exfiltration_urls(ln)
        ln, redacted_cred = redact_credentials(ln)
        if redacted_url or redacted_cred:
            sel().log_tool_invocation(
                session_key=BACKGROUND_KEY, tool_name="llm_output_redaction",
                source="chat_nav", outcome="redacted",
                metadata={"redacted_url": bool(redacted_url), "redacted_cred": bool(redacted_cred)},
            )
        results.append(ln[:80])
    return results


async def api_chat_nav_resolve_links(request: web.Request) -> web.Response:
    """POST /api/chat/nav/resolve-links — batch resolve link summaries via LLM."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    links = body.get("links", [])
    if not isinstance(links, list) or not links:
        return web.json_response({"error": "links array required"}, status=400)
    # Cap at 20 links per request
    links = links[:20]

    try:
        summaries = await _resolve_link_summaries(state, links)
    except Exception:
        logger.warning("Link summary resolution failed", exc_info=True)
        return web.json_response({"error": "resolution failed"}, status=500)

    # Pad if LLM returned fewer lines than expected
    while len(summaries) < len(links):
        summaries.append("")

    return web.json_response({"summaries": summaries[:len(links)]})
