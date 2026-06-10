"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from hypothesis import HealthCheck, settings

from kiro_claw.safety_override import reset_singleton as _reset_safety_override
from kiro_claw.slack.client import SlackClientOps
from kiro_claw.slack.handler import _PHASE_EMOJIS, _build_phase_emojis

# ── Hypothesis profiles ─────────────────────────────────────────────────
# Default (CI): fast iteration.  Run ``HYPOTHESIS_PROFILE=thorough brazil-build test``
# for deeper coverage.
settings.register_profile("default", max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
settings.register_profile("thorough", max_examples=100)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

# Ensure .hypothesis/tmp exists (build environment may not have it)
os.makedirs(os.path.join(os.path.dirname(__file__), "..", ".hypothesis", "tmp"), exist_ok=True)

_HAS_GIT = shutil.which("git") is not None

requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking."""
    _reset_safety_override()
    yield
    _reset_safety_override()


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure an event loop exists for asyncio.Semaphore default_factory (Python 3.9)."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _disable_challenge_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin challenge-redirect OFF in tests so messages reach the agent.

    This matches the production default (challenge_redirect_enabled=false).
    Tests that specifically exercise challenge behavior override this via
    monkeypatch on the config.
    """
    import kiro_claw.slack.events as _events_mod

    monkeypatch.setattr(_events_mod, "_is_challenge_redirect_enabled", lambda: False)


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure git commits succeed in environments without a global git identity."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture(autouse=True)
def _no_load_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip system load checks in tests — avoids real asyncio.sleep delays."""
    from unittest.mock import AsyncMock

    try:
        monkeypatch.setattr("kiro_claw.task_executor._wait_for_load", AsyncMock())
    except AttributeError:
        pass  # load guard not present in this branch


@pytest.fixture(autouse=True)
def _enterprise_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    monkeypatch.setattr("kiro_claw.slack.enterprise._validated_team_id", "TTEST")
    monkeypatch.setattr("kiro_claw.slack.enterprise._validated_enterprise_id", "ETEST")
    monkeypatch.setattr("kiro_claw.slack.enterprise._allowed_team_ids", {"TTEST"})


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


class MockSlackClient(SlackClientOps):
    """In-memory mock for testing."""

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self._next_ts = 1000000
        self._fetch_message_result: str | None = None
        self._fetch_thread_replies_result: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            ("post", {"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts,
                      "unfurl_links": unfurl_links, "unfurl_media": unfurl_media})
        )
        return ts

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "blocks",
                {
                    "channel": channel,
                    "blocks": blocks,
                    "text": text,
                    "thread_ts": thread_ts,
                    "ts": ts,
                    "unfurl_links": unfurl_links,
                    "unfurl_media": unfurl_media,
                },
            )
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))

    async def delete_message(self, channel, ts):
        self.actions.append(("delete", {"channel": channel, "ts": ts}))

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("react", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("unreact", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def open_dm(self, user_id):
        self.actions.append(("open_dm", {"user_id": user_id}))
        return f"D{user_id}"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "user_id": user_id, "text": text, "blocks": blocks, "thread_ts": thread_ts}))

    async def views_publish(self, user_id, view):
        self.actions.append(("views_publish", {"user_id": user_id, "view": view}))

    async def views_open(self, trigger_id, view):
        self.actions.append(("views_open", {"trigger_id": trigger_id, "view": view}))

    async def views_update(self, view_id, view):
        self.actions.append(("views_update", {"view_id": view_id, "view": view}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        self.actions.append(
            (
                "upload_file",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "file": file,
                    "filename": filename,
                    "title": title,
                },
            )
        )

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        if not getattr(self, "_stream_enabled", False) or getattr(self, "_start_stream_fails", False):
            return None
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "start_stream",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": initial_text,
                    "ts": ts,
                },
            )
        )
        return ts

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.actions.append(
            (
                "append_task",
                {
                    "channel": channel,
                    "ts": ts,
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": details,
                },
            )
        )
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts, "text": final_text}))
        return True

    async def set_thread_title(self, channel, thread_ts, title):
        self.actions.append(
            ("set_thread_title", {"channel": channel, "thread_ts": thread_ts, "title": title})
        )

    async def set_thread_status(self, channel, thread_ts, status):
        self.actions.append(
            ("set_thread_status", {"channel": channel, "thread_ts": thread_ts, "status": status})
        )

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        self.actions.append(("fetch_message", {"channel": channel, "ts": ts}))
        return self._fetch_message_result

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        self.actions.append(("fetch_thread_replies", {"channel": channel, "thread_ts": thread_ts, "limit": limit, "warn_on_pagination": warn_on_pagination}))
        return self._fetch_thread_replies_result
