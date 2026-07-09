"""Tests for the session-title behavior:

- ``_ChatSlot.display_title`` shows "New Session…" for untitled slots (brand-new
  empty sessions and the pre-LLM window), never the bare chat-N key.
- ``_fallback_title_from_messages`` truncates the first user message with an
  ellipsis when the LLM can't title the chat.
- ``_maybe_auto_title`` SKIP fallback and in-flight guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_claw.dashboard.chat_title import (
    _fallback_title_from_messages,
)
from kiro_claw.dashboard.state import NEW_SESSION_TITLE, _ChatSlot


def _fake_state():
    state = MagicMock()
    # conversation_log must be truthy for _persist_title to attempt a write.
    state.conversation_log = MagicMock()
    return state


class TestDisplayTitle:
    def test_untitled_default_shows_new_session(self):
        slot = _ChatSlot("chat-4-1783603256")  # title defaults to key
        assert slot.title == slot.key
        assert slot._titled is False
        assert slot.display_title == NEW_SESSION_TITLE

    def test_label_value(self):
        assert NEW_SESSION_TITLE == "New Session…"

    def test_titled_slot_shows_real_title(self):
        slot = _ChatSlot("chat-4-1783603256", title="Debug flaky test")
        slot._titled = True
        assert slot.display_title == "Debug flaky test"

    def test_non_key_title_shows_through_even_if_untitled(self):
        # e.g. a plan/cron/slack session that set a title without _titled.
        slot = _ChatSlot("chat-4-1783603256", title="Plan: task-7")
        assert slot._titled is False
        assert slot.display_title == "Plan: task-7"

    def test_empty_title_shows_new_session(self):
        slot = _ChatSlot("chat-4-1783603256", title="")
        # title defaults to key when empty is passed, but force-empty to simulate
        # a resume path that stored no title.
        slot.title = ""
        assert slot.display_title == NEW_SESSION_TITLE

    def test_resumed_dashboard_key_form_shows_new_session(self):
        # Resume can set title to the dashboard_-prefixed key form while the
        # slot key is stripped — still an identifier, not a name.
        slot = _ChatSlot("chat-4-1783603256")
        slot.title = "dashboard_chat-4-1783603256"
        assert slot._titled is False
        assert slot.display_title == NEW_SESSION_TITLE


class TestFallbackTitle:
    def test_short_message_returned_whole(self):
        msgs = [{"role": "user", "content": "hi there"}]
        assert _fallback_title_from_messages(msgs) == "hi there"

    def test_long_message_truncated_with_ellipsis_on_word_boundary(self):
        long = "help me write a dockerfile for a go service with a multi stage build and caching"
        out = _fallback_title_from_messages([{"role": "user", "content": long}])
        assert out.endswith("…")
        assert len(out) <= 61  # <=60 chars + ellipsis
        # trimmed on a word boundary — no dangling space before the ellipsis
        assert not out[:-1].endswith(" ")
        assert long.startswith(out[:-1])

    def test_strips_browse_marker(self):
        msgs = [{"role": "user", "content": "[BROWSE] check something"}]
        assert _fallback_title_from_messages(msgs) == "check something"

    def test_no_user_text_returns_label(self):
        assert _fallback_title_from_messages([]) == NEW_SESSION_TITLE


class TestAutoTitleInFlightGuard:
    """The on-send trigger and the end-of-turn trigger must not both hit the LLM."""

    def test_in_flight_guard_short_circuits(self):
        import asyncio

        from kiro_claw.dashboard import chat_title

        state = _fake_state()
        slot = _ChatSlot("chat-4-1783603256")
        slot.messages.append({"role": "user", "content": "debug my flaky test"})
        slot._title_in_flight = True  # simulate an attempt already running

        called = False

        async def _boom(*_a, **_k):
            nonlocal called
            called = True
            return "should not happen"

        orig = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _boom  # type: ignore[assignment]
        try:
            asyncio.run(chat_title._maybe_auto_title(state, slot))
        finally:
            chat_title._generate_title_via_kiro = orig  # type: ignore[assignment]

        assert called is False  # guard prevented the LLM call
        assert slot._titled is False


class TestSkipFallbackBranch:
    """On LLM SKIP: keep pending on the on-send attempt, fall back once the
    assistant has responded (definitive failure)."""

    def _run_with_skip(self, slot):
        import asyncio

        from kiro_claw.dashboard import chat_title

        state = _fake_state()

        async def _skip(*_a, **_k):
            return ""  # simulate SKIP/empty

        orig = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _skip  # type: ignore[assignment]
        try:
            asyncio.run(chat_title._maybe_auto_title(state, slot))
        finally:
            chat_title._generate_title_via_kiro = orig  # type: ignore[assignment]
        return state

    def test_on_send_skip_falls_back_but_stays_unlocked(self):
        # Only the user message present (on-send attempt). SKIP now shows the
        # truncated fallback immediately (fast), but leaves _titled False so the
        # end-of-turn attempt can still upgrade it to a real LLM title.
        slot = _ChatSlot("chat-9-1")
        slot.messages.append({"role": "user", "content": "something vague"})

        self._run_with_skip(slot)

        assert slot._titled is False
        assert slot.title == "something vague"  # short enough, no ellipsis

    def test_skip_after_response_falls_back_to_truncation(self):
        # Assistant has responded and the LLM still SKIP'd — definitive failure.
        slot = _ChatSlot("chat-9-2")
        slot.messages.append({"role": "user", "content": "a fairly vague opening question here"})
        slot.messages.append({"role": "assistant", "content": "some reply"})

        self._run_with_skip(slot)

        assert slot._titled is True
        assert slot.title == "a fairly vague opening question here"  # short enough, no ellipsis
