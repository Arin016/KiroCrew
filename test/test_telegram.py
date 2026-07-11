"""Unit tests for the Telegram channel on the messaging-transport abstraction.

Covers: command parsing + conversation state (commands.py), text chunking +
[OPTIONS:] extraction + inline keyboards (renderer.py), deny-by-default auth +
capabilities + inbound normalization (transport.py), streaming render +
finalization (renderer.py), the interactive approval decider, and the dispatch
turn + callback routing (transport_dispatch.py).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from kiro_claw.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_claw.messaging.renderer import DONE, TEXT_CHUNK, TOOL_CALL, OutputEvent
from kiro_claw.messaging.transport import InboundMessage
from kiro_claw.telegram.client import TELEGRAM_CHUNK_LIMIT, TelegramInbound
from kiro_claw.telegram.commands import ConversationState, parse_command
from kiro_claw.telegram.renderer import (
    TelegramApprovalDecider,
    TelegramRenderer,
    _extract_options,
    _md_to_telegram_html,
    _split_markdown,
    _split_text,
    build_inline_keyboard,
)
from kiro_claw.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport
from kiro_claw.telegram.transport_dispatch import TelegramDispatcher

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeClient:
    """Captures outbound Bot API calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[int, str, Any]] = []
        self.drafts: list[tuple[int, str]] = []
        self.markup_edits: list[tuple[int, Any]] = []
        self.answered: list[str] = []
        self._mid = 100

    async def send_typing(self, chat_id: int) -> None:
        return None

    async def send_message_draft(
        self, chat_id: int, draft_id: int, text: str, *, parse_mode: Any = None
    ) -> bool:
        self.drafts.append((draft_id, text))
        return True

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: Any = None,
        reply_markup: Any = None, retry_plain: bool = True
    ) -> int:
        self._mid += 1
        self.sent.append((text, reply_markup))
        return self._mid

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, *, parse_mode: Any = None,
        reply_markup: Any = None, retry_plain: bool = True
    ) -> bool:
        self.edits.append((message_id, text, reply_markup))
        return True

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> bool:
        self.markup_edits.append((message_id, reply_markup))
        return True

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append(callback_query_id)


class _Ev:
    def __init__(
        self, kind: str, text: str = "", stop_reason: str = "", title: str = ""
    ) -> None:
        self.kind = kind
        self.text = text
        self.stop_reason = stop_reason
        self.tool_call_id = ""
        self.title = title
        self.context_usage_pct = 0.0


class FakeProvider:
    supports_steer = True

    def __init__(self, reply: str = "Answer") -> None:
        self._reply = reply
        self.steered: list = []

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def stream_command(self, command: str) -> Any:
        yield _Ev(EVENT_COMPACTION_STATUS, text="completed", title="ok")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": "ok"}

    async def approve_tool(self, request_id: Any) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


class FakeSessions:
    def __init__(self, raise_on_get: bool = False) -> None:
        self.released: list[str] = []
        self.acquired: list[str] = []
        self.destroyed: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.raise_on_get = raise_on_get
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()

    async def get_or_create(self, key: str, *, agent: Any = None, channel_id: Any = None) -> Any:
        self.last_agent = agent
        if self.raise_on_get:
            raise RuntimeError("cold-start failed")
        return FakeProvider(), True, False

    async def set_channel(self, key: str, channel: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 10.0

    def release(self, key: str) -> None:
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._gp

    def is_busy(self, key: str) -> bool:
        return self._busy

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        self.queued.append((ts, text, kw))
        return True

    def dequeue(self, key: str) -> Any:
        return self.queued.pop(0) if self.queued else None

    def has_session(self, key: str) -> bool:
        return self._has

    async def try_acquire(self, key: str) -> bool:
        # Mirror the real atomic acquire-if-idle: refuse if a turn holds the
        # semaphore or no session exists; otherwise "acquire" and record it.
        if self._busy or not self._has:
            return False
        self.acquired.append(key)
        return True

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)


class _FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        return text, None


def _cfg(soft: int = 80, default_agent: str = "") -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(soft_threshold_pct=soft),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(
    allowed: set[int], *, raise_on_get: bool = False, default_agent: str = ""
) -> tuple[TelegramDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    d = TelegramDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=_cfg(default_agent=default_agent),
        allowed_user_ids=allowed,
        agent=None,
        conv_log=None,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess


# ── commands.py ──────────────────────────────────────────────────────────


class TestParseCommand:
    def test_new_aliases(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("/start") == "new"

    def test_compact(self) -> None:
        assert parse_command("/compact") == "compact"

    def test_help(self) -> None:
        assert parse_command("/help") == "help"

    def test_command_with_trailing_args(self) -> None:
        assert parse_command("/new please") == "new"

    def test_plain_text_is_not_a_command(self) -> None:
        assert parse_command("hello there") is None

    def test_unknown_slash_is_not_a_command(self) -> None:
        assert parse_command("/frobnicate") is None


class TestConversationState:
    def test_gen_starts_at_zero_and_bumps(self) -> None:
        s = ConversationState()
        assert s.current_gen(1) == 0
        assert s.bump_gen(1) == 1
        assert s.current_gen(1) == 1

    def test_awaiting_flag_roundtrip(self) -> None:
        s = ConversationState()
        assert s.is_awaiting(1) is False
        s.set_awaiting(1)
        assert s.is_awaiting(1) is True
        s.clear_awaiting(1)
        assert s.is_awaiting(1) is False

    def test_bump_gen_clears_awaiting(self) -> None:
        s = ConversationState()
        s.set_awaiting(1)
        s.bump_gen(1)
        assert s.is_awaiting(1) is False

    def test_maybe_rotate_first_message_no_rotate(self) -> None:
        s = ConversationState()
        assert s.maybe_rotate(1, 1000.0, idle_minutes=30) is False
        assert s.current_gen(1) == 0

    def test_maybe_rotate_idle_bumps_gen(self) -> None:
        s = ConversationState()
        s.maybe_rotate(1, 1000.0, idle_minutes=30)
        assert s.maybe_rotate(1, 1000.0 + 31 * 60, idle_minutes=30) is True
        assert s.current_gen(1) == 1

    def test_maybe_rotate_records_activity_without_rotating(self) -> None:
        s = ConversationState()
        s.maybe_rotate(1, 1000.0, idle_minutes=30)
        assert s.maybe_rotate(1, 1000.0 + 60, idle_minutes=30) is False
        assert s.current_gen(1) == 0


# ── renderer.py helpers ────────────────────────────────────────────────────


class TestSplitText:
    def test_short_text_single_chunk(self) -> None:
        assert _split_text("hello", TELEGRAM_CHUNK_LIMIT) == ["hello"]

    def test_long_text_chunks_within_limit(self) -> None:
        text = "\n\n".join("para " + "x" * 500 for _ in range(20))
        chunks = _split_text(text, TELEGRAM_CHUNK_LIMIT)
        assert len(chunks) > 1
        assert all(len(c) <= TELEGRAM_CHUNK_LIMIT for c in chunks)

    def test_no_content_lost_when_hard_split(self) -> None:
        text = "y" * (TELEGRAM_CHUNK_LIMIT * 2 + 100)  # no break points
        chunks = _split_text(text, TELEGRAM_CHUNK_LIMIT)
        assert all(len(c) <= TELEGRAM_CHUNK_LIMIT for c in chunks)
        assert "".join(chunks) == text

    def test_split_markdown_keeps_fences_balanced_and_escaped(self) -> None:
        # A fenced code block longer than the limit must split into chunks that
        # each carry balanced ``` fences, so the per-chunk HTML pass wraps the
        # code in <pre> and escapes <,>,& instead of leaking a literal ``` and
        # 400-ing the send.
        code = "\n".join(f"row <{i}> & 'v'" for i in range(200))
        full = f"code:\n\n```python\n{code}\n```\n\ndone"
        chunks = _split_markdown(full, 400)
        assert len(chunks) > 1
        assert all(ch.count("```") % 2 == 0 for ch in chunks)  # balanced fences
        htmls = [_md_to_telegram_html(ch) for ch in chunks]
        assert all("```" not in h for h in htmls)  # no literal fence leaked
        assert any("<pre>" in h and "&lt;" in h for h in htmls)  # wrapped + escaped


class TestInlineKeyboard:
    def test_none_when_no_options(self) -> None:
        assert build_inline_keyboard([]) is None

    def test_callback_data_is_index_only_and_byte_safe(self) -> None:
        # Multi-byte (CJK) labels must not blow the 64-byte callback_data cap.
        kb = build_inline_keyboard(["开始实现 Tier 0 的完整方案很长的选项文字", "B"])
        assert kb is not None
        for row in kb["inline_keyboard"]:
            for btn in row:
                assert btn["callback_data"].startswith("opt:")
                assert len(btn["callback_data"].encode("utf-8")) <= 64

    def test_two_buttons_per_row(self) -> None:
        kb = build_inline_keyboard(["a", "b", "c"])
        assert kb is not None
        assert len(kb["inline_keyboard"][0]) == 2
        assert len(kb["inline_keyboard"][1]) == 1


class TestExtractOptions:
    def test_trailing_options_extracted(self) -> None:
        body, opts = _extract_options("Answer here\n\n[OPTIONS: Yes | No | Maybe]")
        assert body == "Answer here"
        assert opts == ["Yes", "No", "Maybe"]

    def test_no_options(self) -> None:
        body, opts = _extract_options("just text")
        assert body == "just text"
        assert opts == []

    def test_partial_streaming_fragment_hidden(self) -> None:
        body, opts = _extract_options("text so far [OPTIONS: Ye")
        assert "[OPTIONS" not in body
        assert opts == []


# ── transport.py: deny-by-default auth + capabilities + inbound ─────────────


class TestTransportAuth:
    """A Telegram bot is globally reachable, so auth is deny-by-default."""

    def _msg(self, uid: str) -> InboundMessage:
        return InboundMessage(
            channel_type="telegram", user_id=uid, conversation_id=uid, text="hi"
        )

    def test_empty_allowlist_denies_everyone(self) -> None:
        t = TelegramTransport(FakeClient())  # type: ignore[arg-type]
        assert t.authorize(self._msg("8743158320")) is False

    def test_listed_user_allowed(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("8743158320")) is True

    def test_unlisted_user_denied(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("999")) is False

    def test_empty_user_id_denied(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("")) is False

    def test_capabilities(self) -> None:
        assert TELEGRAM_CAPABILITIES.streaming is True
        assert TELEGRAM_CAPABILITIES.edit is True
        assert TELEGRAM_CAPABILITIES.max_message_chars == TELEGRAM_CHUNK_LIMIT
        assert TELEGRAM_CAPABILITIES.max_buttons == 8


class TestTransportReceive:
    def _run_receive(self, allowed: list[int], inbound: TelegramInbound) -> list[InboundMessage]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        t = TelegramTransport(FakeClient(), allowed_user_ids=allowed, dispatch=_dispatch)  # type: ignore[arg-type]
        asyncio.run(t.receive(inbound))
        return dispatched

    def test_authorized_message_dispatched(self) -> None:
        inbound = TelegramInbound(chat_id=7, user_id=7, text="hello", chat_type="private")
        out = self._run_receive([7], inbound)
        assert len(out) == 1
        assert out[0].channel_type == "telegram"
        assert out[0].user_id == "7"
        assert out[0].text == "hello"

    def test_unauthorized_message_dropped(self) -> None:
        inbound = TelegramInbound(chat_id=9, user_id=9, text="hello", chat_type="private")
        assert self._run_receive([7], inbound) == []

    def test_non_text_message_dropped(self) -> None:
        inbound = TelegramInbound(chat_id=7, user_id=7, text="")
        assert self._run_receive([7], inbound) == []

    def test_non_private_chat_dropped(self) -> None:
        # A bot added to a group must not run a turn (its reply would land in
        # the group, leaking tool output to non-allowlisted members) even for
        # an allow-listed sender. Fail closed on any non-private chat.
        for ct in ("group", "supergroup", "channel", ""):
            inbound = TelegramInbound(chat_id=-100, user_id=7, text="hi", chat_type=ct)
            assert self._run_receive([7], inbound) == []


# ── renderer.py: streaming + finalization ───────────────────────────────────


class TestRenderer:
    def _drive(self, events: list[OutputEvent]) -> FakeClient:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            for ev in events:
                await r.dispatch(ev)

        asyncio.run(_go())
        return cli

    def test_streaming_strips_options_and_renders_keyboard(self) -> None:
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t", title="fs_read"),
                OutputEvent(kind=TEXT_CHUNK, text="Hello. "),
                OutputEvent(kind=TEXT_CHUNK, text="Pick.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # Block streaming: no placeholder edit-stream; the finished answer is
        # sent as one block with the [OPTIONS:] keyboard attached.
        final_text, final_kb = cli.sent[-1]
        assert final_text == "Hello. Pick."  # [OPTIONS:] stripped
        labels = [b["text"] for row in final_kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_streams_via_drafts_and_persists_once_without_edits(self) -> None:
        # The core of the fix: growing text streams as native animated drafts,
        # the finished answer is persisted with ONE sendMessage, and
        # editMessageText is never used (no reflow stutter).
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="one "),
                OutputEvent(kind=TEXT_CHUNK, text="two "),
                OutputEvent(kind=TEXT_CHUNK, text="three"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert cli.edits == []  # never edits -> no reflow stutter
        assert cli.drafts  # streamed as native animated drafts
        assert len(cli.sent) == 1  # one persisted block
        assert cli.sent[-1][0] == "one two three"

    def test_error_done_renders_error_when_no_text(self) -> None:
        cli = self._drive([OutputEvent(kind=DONE, stop_reason="error")])
        assert "Error" in cli.sent[-1][0]

    def test_close_is_idempotent_after_done(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> int:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))
            n = len(cli.sent)
            await r.close()  # should no-op
            return len(cli.sent) - n

        assert asyncio.run(_go()) == 0


# ── renderer.py: interactive approval decider ───────────────────────────────


class TestApprovalDecider:
    def test_resolve_pending(self) -> None:
        async def _go() -> bool:
            d = TelegramApprovalDecider(session_key="telegram:1:0")
            task = asyncio.ensure_future(d(SimpleNamespace(request_id="rq7")))
            await asyncio.sleep(0.02)
            TelegramApprovalDecider.resolve_global("telegram:1:0:rq7", True)
            return await task

        assert asyncio.run(_go()) is True

    def test_resolve_unknown_key_returns_false(self) -> None:
        assert TelegramApprovalDecider.resolve_global("no-such-key", True) is False


# ── transport_dispatch.py: turn + callback routing ─────────────────────────


class TestDispatcher:
    def test_full_turn_records_success_and_releases(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hello world")
            )

        asyncio.run(_go())
        assert cli.sent[-1][0] == "Answer: hello world"
        assert sess.successes == ["telegram:kiroclaw:direct:7"]
        assert sess.released == ["telegram:kiroclaw:direct:7"]

    def test_agent_resolves_to_kiroclaw_when_unset(self) -> None:
        # agent=None + empty default_agent must fall back to "kiroclaw" so the
        # session loads kiroclaw-core (spawn_run), not kiro-cli's bare default.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert sess.last_agent == "kiroclaw"

    def test_cold_start_failure_finalizes_and_skips_release(self) -> None:
        # If get_or_create raises (cold-start), the turn must still be finalized
        # (block streaming sends an error block, no silent dead turn) and the
        # semaphore must NOT be released (it was never acquired).
        d, cli, sess = _dispatcher({7}, raise_on_get=True)

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert cli.sent and "Error" in cli.sent[-1][0]  # finalized by close()
        assert sess.released == []  # never acquired -> never released
        assert sess.failures == []  # not acquired -> not recorded as a failed turn

    def test_new_command_bumps_gen_and_replies(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="/new")
            )

        asyncio.run(_go())
        assert d._conv.current_gen(7) == 1
        assert "New conversation" in cli.sent[-1][0]
        assert sess.successes == []  # no turn ran

    def test_help_command_replies(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="/help")
            )

        asyncio.run(_go())
        assert "KiroClaw" in cli.sent[-1][0]

    def test_compact_refused_while_turn_running(self) -> None:
        # /compact must NOT drive the same provider while a turn streams. The
        # guard now atomically try_acquire()s the semaphore: if a turn holds it,
        # acquisition fails and we refuse — no acquire, no concurrent stream.
        d, cli, sess = _dispatcher({7})
        sess._busy = True  # simulate an in-flight turn holding the semaphore

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="/compact")
            )

        asyncio.run(_go())
        assert any("try /compact" in s[0] for s in cli.sent)  # refused with notice
        assert not any("Compacting" in s[0] for s in cli.sent)  # never started
        assert sess.acquired == []  # semaphore never taken while busy

    def test_compact_when_idle_holds_and_releases_semaphore(self) -> None:
        # When idle, /compact atomically acquires the per-session semaphore for
        # the whole compaction (serializing against a normal turn), then always
        # releases it — so it can't interleave JSON-RPC on the shared provider.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="/compact")
            )

        asyncio.run(_go())
        assert sess.acquired == ["telegram:kiroclaw:direct:7"]  # acquired the turn semaphore
        assert sess.released == ["telegram:kiroclaw:direct:7"]  # and released it in finally
        assert any("Compact" in s[0] for s in cli.sent) or any(
            "Compact" in e[1] for e in cli.edits
        )

    def test_callback_option_echoes_choice_and_redispatches(self) -> None:
        d, cli, sess = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q1", user_id=7, chat_id=7, message_id=99, data="opt:0", label="Say Hi", chat_type="private"
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        # Tapping an option retires the keyboard on the original message WITHOUT
        # overwriting its text, echoes the picked choice as its own block, then
        # re-dispatches the choice so the answer arrives as a NEW message.
        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        assert cli.edits == []  # original answer text is never clobbered
        assert "Say Hi" in cli.sent[0][0]  # choice echoed as its own block first
        assert cli.sent[-1][0] == "Answer: Say Hi"  # answer arrives as a new message

    def test_callback_approval_resolves_decider(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(7), "rq9")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            cb = SimpleNamespace(
                callback_query_id="q2", user_id=7, chat_id=7, message_id=100, data="a:rq9:1", label="", chat_type="private"
            )
            await d.on_callback(cb)  # type: ignore[arg-type]
            return fut.done() and fut.result() is True

        assert asyncio.run(_go()) is True

    def test_callback_approval_expired_shows_expired_not_approved(self) -> None:
        # Post-timeout: no pending future for the key (decider already denied by
        # default and popped it). An "Approve" press must NOT display "Approved".
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q5", user_id=7, chat_id=7, message_id=101, data="a:gone:1", label="", chat_type="private"
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.edits, "expected a verdict edit"
        assert "expired" in cli.edits[-1][1].lower()
        assert "Approved" not in cli.edits[-1][1]

    def test_callback_unauthorized_user_ignored(self) -> None:
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q3", user_id=999, chat_id=999, message_id=1, data="opt:0", label="X", chat_type="private"
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        # Deny-by-default short-circuits BEFORE the ack: no Bot API round-trip
        # and no edit/redispatch for an unauthorized user.
        assert cli.answered == []
        assert cli.edits == []

    def test_callback_non_private_chat_ignored(self) -> None:
        # Defense-in-depth: even an allow-listed user's press is ignored if the
        # callback isn't from a private chat (mirrors the receive() guard).
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q4", user_id=7, chat_id=-100, message_id=1, data="opt:0", label="X", chat_type="group"
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.answered == []
        assert cli.edits == []


class TestClientSession:
    def test_ensure_session_creates_single_shared_instance(self, monkeypatch: Any) -> None:
        # Concurrent _api callers (polling loop + handler tasks) must share ONE
        # ClientSession — the double-checked lock in _ensure_session prevents a
        # leaked duplicate.
        import kiro_claw.telegram.client as client_mod

        created = {"n": 0}

        class _FakeSession:
            def __init__(self) -> None:
                created["n"] += 1
                self.closed = False

        monkeypatch.setattr(client_mod.aiohttp, "ClientSession", _FakeSession)
        cli = client_mod.TelegramClient(token="x")

        async def _go() -> None:
            await asyncio.gather(
                cli._ensure_session(), cli._ensure_session(), cli._ensure_session()
            )

        asyncio.run(_go())
        assert created["n"] == 1


class TestTelegramTokenRedaction:
    """#1 — a Telegram bot token echoed in output must be scrubbed."""

    def test_bot_token_is_redacted(self) -> None:
        from kiro_claw.security import redact_credentials

        token = "8412345678:AAExampleSecretTokenValue_1234567890abcd"
        cleaned, warnings = redact_credentials(f"my token is {token} ok")
        assert token not in cleaned
        assert "[REDACTED: credential]" in cleaned
        assert warnings  # at least one redaction warning recorded

    def test_benign_short_colon_pairs_not_redacted(self) -> None:
        from kiro_claw.security import redact_credentials

        # Too few digits (<6) and too-short suffix (<30) to match the token
        # shape — must not be over-redacted.
        text = "ratio 12:34, port 8080:abc, time 10:30:00"
        cleaned, _ = redact_credentials(text)
        assert cleaned == text


class TestConfigMasking:
    """#5 — sensitive config fields are masked in the API response only."""

    def test_bot_token_masked_in_response(self) -> None:
        from kiro_claw.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {
                    "telegram": {
                        "bot_token": "8412345678:AAsecretsecretsecret",
                        "enabled": True,
                        "allowed_user_ids": [7],
                    }
                }

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        assert out["telegram"]["bot_token"] == _SENSITIVE_MASK  # secret hidden
        assert out["telegram"]["enabled"] is True  # non-sensitive untouched
        assert out["telegram"]["allowed_user_ids"] == [7]

    def test_empty_token_not_masked(self) -> None:
        from kiro_claw.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {"telegram": {"bot_token": "", "enabled": False}}

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        # Unset stays empty (UI shows "not set"), never a fake mask sentinel.
        assert out["telegram"]["bot_token"] == ""
        assert _SENSITIVE_MASK not in str(out)


class TestTelegramMidTurn:
    def test_busy_steer_folds_into_running_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="and also this"
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == ["and also this"]
        assert any("Folding" in t for t, _ in cli.sent)
        assert sess.queued == []

    def test_busy_queue_mode_enqueues(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="later"
                )
            )

        asyncio.run(_go())
        assert [text for _ts, text, _ in sess.queued] == ["later"]
        assert sess._gp.steered == []
        assert any("Queued" in t for t, _ in cli.sent)

    def test_not_busy_runs_a_full_turn(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello"
                )
            )

        asyncio.run(_go())
        assert sess.successes == ["telegram:kiroclaw:direct:7"]
        assert sess._gp.steered == []

    def test_drain_processes_queued_messages_iteratively(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.queued = [("t1", "first", {}), ("t2", "second", {})]

        async def _go() -> None:
            await d._drain_queue("telegram:kiroclaw:direct:7", 7, 7)

        asyncio.run(_go())
        # Both queued messages ran as full turns (drain=False -> no recursion),
        # and the queue is empty afterward.
        assert sess.successes == [
            "telegram:kiroclaw:direct:7",
            "telegram:kiroclaw:direct:7",
        ]
        assert sess.queued == []
