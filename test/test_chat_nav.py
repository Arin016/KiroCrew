"""Tests for chat_nav link summary resolution."""

from __future__ import annotations

import pytest

from kiro_claw.dashboard.chat_nav import _build_link_summary_prompt, _resolve_link_summaries


class TestBuildLinkSummaryPrompt:
    def test_single_link_no_context(self):
        links = [{"url": "https://code.amazon.com/reviews/CR-123"}]
        prompt = _build_link_summary_prompt(links)
        assert "1. URL: https://code.amazon.com/reviews/CR-123" in prompt
        assert "Context:" not in prompt

    def test_single_link_with_context(self):
        links = [{"url": "https://quip-amazon.com/abc", "context": "Design doc for memory"}]
        prompt = _build_link_summary_prompt(links)
        assert "Context: Design doc for memory" in prompt

    def test_multiple_links(self):
        links = [
            {"url": "https://a.com"},
            {"url": "https://b.com", "context": "ctx"},
        ]
        prompt = _build_link_summary_prompt(links)
        assert "1. URL: https://a.com" in prompt
        assert "2. URL: https://b.com" in prompt

    def test_context_truncated_at_300(self):
        links = [{"url": "https://x.com", "context": "a" * 500}]
        prompt = _build_link_summary_prompt(links)
        # Context should be truncated
        assert "a" * 300 in prompt
        assert "a" * 301 not in prompt

    def test_empty_context_stripped(self):
        links = [{"url": "https://x.com", "context": "   "}]
        prompt = _build_link_summary_prompt(links)
        assert "Context:" not in prompt


class TestResolveLinkSummaries:
    @pytest.mark.asyncio
    async def test_parses_numbered_lines(self, monkeypatch):
        """LLM returns numbered lines like '1. Label Here'."""
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        class FakeEvent:
            def __init__(self, kind, text=""):
                self.kind = kind
                self.text = text

        class FakeClient:
            async def stream(self, prompt):
                yield FakeEvent(EVENT_TEXT_CHUNK, "1. Nav Panel Feature CR\n2. Memory V2 Design Doc\n")
                yield FakeEvent(EVENT_COMPLETE)

            async def reject_tool(self, rid):
                pass

        class FakeSessions:
            async def get_or_create(self, key):
                return FakeClient(), False, False

            def release(self, key):
                pass

        class FakeState:
            sessions = FakeSessions()

        result = await _resolve_link_summaries(
            FakeState(),
            [{"url": "https://cr.com", "context": ""}, {"url": "https://quip.com", "context": ""}],
        )
        assert result == ["Nav Panel Feature CR", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_preserves_labels_starting_with_digits(self, monkeypatch):
        """Labels like '2024 Design Roadmap' should not be corrupted."""
        from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        class FakeEvent:
            def __init__(self, kind, text=""):
                self.kind = kind
                self.text = text

        class FakeClient:
            async def stream(self, prompt):
                yield FakeEvent(EVENT_TEXT_CHUNK, "2024 Design Roadmap\n3-phase rollout plan\n")
                yield FakeEvent(EVENT_COMPLETE)

            async def reject_tool(self, rid):
                pass

        class FakeSessions:
            async def get_or_create(self, key):
                return FakeClient(), False, False

            def release(self, key):
                pass

        class FakeState:
            sessions = FakeSessions()

        result = await _resolve_link_summaries(
            FakeState(),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == ["2024 Design Roadmap", "3-phase rollout plan"]


class TestApiEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_json(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard.chat_nav import api_chat_nav_resolve_links

        app = web.Application()
        app["state"] = None
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/nav/resolve-links", data="not json")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_links(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard.chat_nav import api_chat_nav_resolve_links

        app = web.Application()
        app["state"] = None
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": []})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard import chat_nav
        from kiro_claw.dashboard.chat_nav import api_chat_nav_resolve_links

        async def mock_resolve(state, links):
            return ["Summary " + str(i) for i in range(len(links))]

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/nav/resolve-links",
                json={"links": [{"url": "https://x.com", "context": "test"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["summaries"] == ["Summary 0"]

    @pytest.mark.asyncio
    async def test_caps_at_20(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard import chat_nav
        from kiro_claw.dashboard.chat_nav import api_chat_nav_resolve_links

        received = []

        async def mock_resolve(state, links):
            received.extend(links)
            return ["s"] * len(links)

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            links = [{"url": f"https://{i}.com"} for i in range(25)]
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": links})
            assert resp.status == 200
            assert len(received) == 20
