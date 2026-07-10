"""Layer 2b -- Telegram ``Renderer`` + interactive approval decider.

``TelegramRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Telegram's Bot API:

* ``on_turn_start`` -- typing indicator + a "🤔 …" placeholder message.
* ``on_text_chunk`` -- throttled ``editMessageText`` streaming (typewriter),
  with any trailing ``[OPTIONS:]`` markup held back from the visible stream.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer.
* ``on_prompt_choice`` -- inline Approve/Deny buttons as a SEPARATE message
  (so streaming edits don't clobber them); byte-safe ``callback_data``.
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap and attaching the ``[OPTIONS:]`` inline keyboard to the last chunk.

``TelegramApprovalDecider`` is the interactive ladder's awaiter: ``__call__``
registers a Future keyed by ``session:request_id`` and awaits a button press,
denying by default on timeout; the callback handler resolves it via
``resolve_global``.

Dependency direction is ``telegram -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_claw.messaging.renderer import Renderer
from kiro_claw.messaging.transport import TransportCapabilities

if TYPE_CHECKING:
    from kiro_claw.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

# Min seconds between intermediate edit calls: paces the typewriter effect and
# avoids the Bot API's ~30 edits/min/chat rate limit.
_STREAM_THROTTLE_S = 1.0

# Placeholder shown immediately while the agent is still generating.
_THINKING = "🤔 …"

# Interactive approval wait; deny-by-default when it elapses with no press.
_APPROVAL_TIMEOUT_S = 300.0

# Trailing "[OPTIONS: a | b | c]" -- extracted for inline-keyboard rendering.
_OPTIONS_RE = re.compile(r"\[OPTIONS:\s*(.*?)\]\s*\Z", re.DOTALL)


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into (body, options). Handles the streamed partial too."""
    m = _OPTIONS_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        options = [o.strip() for o in m.group(1).split("|") if o.strip()]
        return body, options
    # Hold back an incomplete "[OPTIONS…" fragment mid-stream.
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip(), []
    return text, []


def build_inline_keyboard(options: list[str]) -> dict | None:
    """Build an InlineKeyboardMarkup from ``[OPTIONS:]`` labels.

    ``callback_data`` is the index only (``opt:<i>``) -- Telegram caps it at
    64 BYTES, so a multi-byte (CJK/emoji) label there could overflow and make
    the whole send fail. The label is recovered from the button text at
    callback time. Two buttons per row (mobile friendly).
    """
    if not options:
        return None
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for i, opt in enumerate(options):
        row.append({"text": opt[:64], "callback_data": f"opt:{i}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into <=``limit`` chunks, preferring paragraph boundaries."""
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 4:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


class TelegramApprovalDecider:
    """Awaits an inline-button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    async def __call__(self, event: Any) -> bool:
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        TelegramApprovalDecider._REGISTRY[k] = fut
        try:
            return bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
        except asyncio.TimeoutError:
            return False  # deny-by-default on timeout
        finally:
            TelegramApprovalDecider._REGISTRY.pop(k, None)

    @classmethod
    def resolve_global(cls, key: str, approved: bool) -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting."""
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


class TelegramRenderer(Renderer):
    """Streams a turn to Telegram via ``editMessageText`` + inline keyboards."""

    channel_type = "telegram"

    def __init__(
        self,
        client: "TelegramClient",
        chat_id: int,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._chat_id = chat_id
        self._session_key = session_key
        self._msg_id: int | None = None
        self._buf: list[str] = []
        self._last_send = 0.0
        self._tool = ""
        self._last_tool = ""
        self._finalized = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._msg_id is not None:  # idempotent (dispatch + driver both call)
            return
        await self._client.send_typing(self._chat_id)
        self._msg_id = await self._client.send_message(self._chat_id, _THINKING)
        self._last_send = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> drop the transient tool footer
        await self._push(force=False)

    async def on_thinking(self, text: str) -> None:
        # Telegram does not surface reasoning inline (parity with prior behavior).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        self._tool = title or tool_kind or "tool"
        self._last_tool = self._tool
        await self._push(force=True)

    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons. callback_data stays well
        # under Telegram's 64-byte cap (a:<request_id>:<1|0>); the callback
        # handler resolves the decider Future by reconstructing the key.
        rid = str(request_id)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"a:{rid}:1"},
                    {"text": "🚫 Deny", "callback_data": f"a:{rid}:0"},
                ]
            ]
        }
        tool = self._last_tool or "this tool"
        await self._client.send_message(
            self._chat_id, f"🔐 Approve `{tool}`?", reply_markup=keyboard
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        try:
            await self._client.send_message(self._chat_id, "🗜️ Compacting context…")
        except Exception:
            logger.debug("Telegram: compaction notice send failed", exc_info=True)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._tool = ""
        ok = stop_reason != "error"
        body = self._text()
        full = body or ("…" if ok else "⚠️ Error — please try again")
        opts = self._options()
        keyboard = build_inline_keyboard(opts) if opts else None
        limit = self.capabilities.max_message_chars or 4000

        if len(full) <= limit:
            if self._msg_id:
                await self._client.edit_message(
                    self._chat_id, self._msg_id, full, reply_markup=keyboard
                )
            else:
                await self._client.send_message(
                    self._chat_id, full, reply_markup=keyboard
                )
            return

        # Long answer: first chunk reuses the streaming message; keyboard on
        # the last chunk only.
        chunks = _split_text(full, limit)
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            kb = keyboard if i == last else None
            if i == 0 and self._msg_id:
                await self._client.edit_message(self._chat_id, self._msg_id, chunk, reply_markup=kb)
            else:
                await self._client.send_message(self._chat_id, chunk, reply_markup=kb)

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def _text(self) -> str:
        raw = "".join(self._buf).strip()
        body, _ = _extract_options(raw)
        return body

    def _options(self) -> list[str]:
        raw = "".join(self._buf).strip()
        _, opts = _extract_options(raw)
        return opts

    def _compose(self) -> str:
        body = self._text()
        if self._tool:
            footer = f"🔧 {self._tool}…"
            return f"{body}\n\n{footer}" if body else footer
        return body

    async def _push(self, *, force: bool) -> None:
        if self._msg_id is None:
            return
        now = time.monotonic()
        if not force and now - self._last_send < _STREAM_THROTTLE_S:
            return
        content = self._compose()
        if not content:
            return
        limit = self.capabilities.max_message_chars or 4000
        await self._client.edit_message(self._chat_id, self._msg_id, content[:limit])
        self._last_send = now
