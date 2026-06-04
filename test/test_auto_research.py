"""Tests for auto_research builtin app handlers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    _safe_campaign_dir,
    _validate_campaign_id,
    check_stagnation,
    create_campaign,
    get_campaign,
    get_findings,
    list_campaigns,
    update_campaign_status,
    validate_campaign,
    write_guidance,
    write_status,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    """Isolate DB and research dir per test."""
    with patch(
        "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH",
        tmp_path / "test.db",
    ), patch(
        "kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR",
        tmp_path / "research",
    ):
        yield tmp_path


class TestPathValidation:
    def test_valid_hex_id(self):
        assert _validate_campaign_id("a1b2c3d4")

    def test_rejects_traversal(self):
        assert not _validate_campaign_id("../etc/passwd")

    def test_rejects_non_hex(self):
        assert not _validate_campaign_id("ABCDEFGH")
        assert not _validate_campaign_id("a1b2c3d")
        assert not _validate_campaign_id("a1b2c3d4e")

    def test_rejects_empty(self):
        assert not _validate_campaign_id("")

    def test_safe_dir_rejects_invalid(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            assert _safe_campaign_dir("../etc") is None
            assert _safe_campaign_dir("a1b2c3d4") is not None


class TestValidation:
    def test_question_too_short(self):
        r = validate_campaign({"question": "short", "sources": ["web"]})
        assert not r["can_start"]

    def test_valid_passes(self):
        r = validate_campaign(
            {"question": "How do teams handle API rate limiting in services?", "sources": ["web"]}
        )
        assert r["can_start"]

    def test_no_sources(self):
        r = validate_campaign({"question": "A valid research question here ok", "sources": []})
        assert not r["can_start"]

    def test_sub_questions_warning(self):
        r = validate_campaign(
            {
                "question": "A valid research question here ok",
                "sources": ["web"],
                "sub_questions": ["one"],
            }
        )
        assert r["can_start"]
        assert any("sub-question" in w.lower() for w in r["warnings"])

    def test_high_cycles_cost_warning(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 60}
        )
        assert r["can_start"]
        assert any("$" in w for w in r["warnings"])

    def test_exceeds_hard_cap(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 101}
        )
        assert not r["can_start"]

    def test_single_campaign_enforcement(self):
        c = create_campaign(
            {"question": "First research question about something", "sources": ["web"]}
        )
        update_campaign_status(c["id"], CampaignStatus.RUNNING)
        r = validate_campaign(
            {"question": "Second research question about something", "sources": ["web"]}
        )
        assert not r["can_start"]

    def test_returns_estimates(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 40}
        )
        assert r["estimated_cycles"] == 40
        assert r["estimated_duration_min"] == 80


class TestStagnation:
    def test_no_dir(self):
        assert not check_stagnation("a1b2c3d4")

    def test_fewer_than_5(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            d = tmp_path / "a1b2c3d4" / "findings"
            d.mkdir(parents=True)
            for i in range(4):
                (d / f"cycle_{i+1:03d}.json").write_text(json.dumps({"new_findings_count": 0}))
            assert not check_stagnation("a1b2c3d4")

    def test_5_zeros_stagnant(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            d = tmp_path / "a1b2c3d4" / "findings"
            d.mkdir(parents=True)
            for i in range(5):
                (d / f"cycle_{i+1:03d}.json").write_text(json.dumps({"new_findings_count": 0}))
            assert check_stagnation("a1b2c3d4")

    def test_recent_finding_not_stagnant(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            d = tmp_path / "a1b2c3d4" / "findings"
            d.mkdir(parents=True)
            for i in range(4):
                (d / f"cycle_{i+1:03d}.json").write_text(json.dumps({"new_findings_count": 0}))
            (d / "cycle_005.json").write_text(json.dumps({"new_findings_count": 1}))
            assert not check_stagnation("a1b2c3d4")

    def test_malformed_json_safe(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            d = tmp_path / "a1b2c3d4" / "findings"
            d.mkdir(parents=True)
            for i in range(4):
                (d / f"cycle_{i+1:03d}.json").write_text(json.dumps({"new_findings_count": 0}))
            (d / "cycle_005.json").write_text("bad json{")
            assert not check_stagnation("a1b2c3d4")


class TestFileInterface:
    def test_write_status(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            write_status("a1b2c3d4", "running")
            d = json.loads((tmp_path / "a1b2c3d4" / "status.json").read_text())
            assert d["status"] == "running"

    def test_write_status_rejects_invalid(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            write_status("../etc", "running")
            assert not (tmp_path / ".." / "etc").exists()

    def test_write_guidance(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            write_status("a1b2c3d4", "running")
            write_guidance("a1b2c3d4", "focus on X")
            assert (tmp_path / "a1b2c3d4" / "guidance.txt").read_text() == "focus on X"

    def test_get_findings_sorted(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            d = tmp_path / "a1b2c3d4" / "findings"
            d.mkdir(parents=True)
            (d / "cycle_002.json").write_text(json.dumps({"cycle": 2, "new_findings_count": 1}))
            (d / "cycle_001.json").write_text(json.dumps({"cycle": 1, "new_findings_count": 1}))
            assert [f["cycle"] for f in get_findings("a1b2c3d4")] == [1, 2]

    def test_get_findings_rejects_invalid(self):
        assert get_findings("../etc") == []


class TestCRUD:
    def test_create(self):
        c = create_campaign({"question": "How do teams handle rate limiting?", "sources": ["web"]})
        assert len(c["id"]) == 8
        assert c["status"] == "ready"

    def test_update_running(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        update_campaign_status(c["id"], CampaignStatus.RUNNING)
        assert get_campaign(c["id"])["started_at"] is not None

    def test_update_complete(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        update_campaign_status(c["id"], CampaignStatus.COMPLETE)
        assert get_campaign(c["id"])["completed_at"] is not None

    def test_update_failed(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        update_campaign_status(c["id"], CampaignStatus.FAILED, error_message="crashed")
        assert get_campaign(c["id"])["error_message"] == "crashed"

    def test_terminal_status_blocks_transition(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        update_campaign_status(c["id"], CampaignStatus.COMPLETE)
        r = update_campaign_status(c["id"], CampaignStatus.RUNNING)
        assert "error" in r
        assert get_campaign(c["id"])["status"] == CampaignStatus.COMPLETE

    def test_list_newest_first(self):
        create_campaign({"question": "First research question about something", "sources": ["web"]})
        time.sleep(0.01)
        create_campaign(
            {"question": "Second research question about something", "sources": ["web"]}
        )
        camps = list_campaigns()
        assert camps[0]["created_at"] >= camps[1]["created_at"]

    def test_get_not_found(self):
        assert get_campaign("a1b2c3d4") is None

    def test_get_rejects_invalid(self):
        assert get_campaign("../etc") is None

    def test_delete_removes_row_and_dir(self):
        from kiro_claw.apps.builtins.auto_research.handlers import (
            _campaign_dir,
            delete_campaign,
        )

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        cid = c["id"]
        (_campaign_dir(cid) / "FINDINGS.md").write_text("# Report")
        assert delete_campaign(cid)["deleted"] is True
        assert get_campaign(cid) is None
        assert not _safe_campaign_dir(cid).exists()

    def test_delete_missing(self):
        from kiro_claw.apps.builtins.auto_research.handlers import delete_campaign

        assert "error" in delete_campaign("a1b2c3d4")


class TestStatusEnum:
    def test_all_8(self):
        expected = {
            "ready",
            "running",
            "paused",
            "stagnant",
            "needs_input",
            "complete",
            "failed",
            "stopped",
        }
        assert {s.value for s in CampaignStatus} == expected


class TestSuggestParse:
    def test_parses_embedded_json_array(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _parse_subquestions

        assert _parse_subquestions('here you go: ["a", "b", "c"] done') == ["a", "b", "c"]

    def test_caps_at_20(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _parse_subquestions

        assert len(_parse_subquestions(json.dumps([f"q{i}" for i in range(50)]))) == 20

    def test_junk_returns_empty(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _parse_subquestions

        assert _parse_subquestions("no array here") == []


# --- Redaction ---
class TestRedaction:
    def test_redact_finding_with_security_module(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        # Should not crash even if security module has issues
        finding = {
            "summary": "test",
            "sources_checked": ["http://example.com"],
            "new_findings_count": 1,
        }
        result = _redact_finding(finding)
        assert "summary" in result

    def test_redact_finding_handles_non_string_values(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"cycle": 1, "new_findings_count": 3, "summary": "test"}
        result = _redact_finding(finding)
        assert result["cycle"] == 1
        assert result["new_findings_count"] == 3

    def test_redact_finding_handles_list_values(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"sources_checked": ["http://a.com", "http://b.com"], "sources_empty": []}
        result = _redact_finding(finding)
        assert isinstance(result["sources_checked"], list)


# --- Audit ---


class TestAudit:
    def test_audit_does_not_crash(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _audit

        # Should not raise even if sel module is unavailable
        _audit("test_operation", "a1b2c3d4")

    def test_audit_with_extras(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _audit

        _audit("campaign_created", "a1b2c3d4", extra_field="value")


# --- Campaign ID validation in handlers ---


class TestHandlerValidation:
    def test_update_rejects_invalid_id(self):
        result = update_campaign_status("../etc", CampaignStatus.RUNNING)
        assert "error" in result

    def test_write_guidance_rejects_invalid(self, tmp_path: Path):
        with patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            write_guidance("../etc", "text")
            # Should not create any file outside research dir
            assert not (tmp_path / ".." / "etc" / "guidance.txt").exists()


# --- Edge cases in validation ---


class TestValidationEdgeCases:
    def test_max_cycles_exactly_50_no_warning(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 50}
        )
        assert r["can_start"]
        assert not any("$" in w for w in r["warnings"])

    def test_max_cycles_exactly_100_no_error(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 100}
        )
        assert r["can_start"]

    def test_default_max_cycles_30(self):
        r = validate_campaign({"question": "A valid research question here ok", "sources": ["web"]})
        assert r["estimated_cycles"] == 30
        assert r["estimated_duration_min"] == 60


# --- Auth ---


class TestRequireAuth:
    def test_returns_none_when_user_present(self):
        from unittest.mock import MagicMock

        from kiro_claw.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = "user123"
        request.query = {}
        assert _require_auth(request) is None

    def test_returns_401_when_no_user(self):
        from unittest.mock import MagicMock

        from kiro_claw.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = None
        resp = _require_auth(request)
        assert resp is not None
        assert resp.status == 401

    def test_rejects_raw_token(self):
        # Raw token alone (without middleware-set user) is rejected — no fail-open.
        from unittest.mock import MagicMock

        from kiro_claw.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = None
        resp = _require_auth(request)
        assert resp is not None
        assert resp.status == 401


# --- Redaction edge cases ---


class TestRedactionNested:
    def test_recursive_nested_dict(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"metadata": {"nested": "value", "deep": {"level": "data"}}, "cycle": 1}
        result = _redact_finding(finding)
        assert isinstance(result["metadata"], dict)
        assert isinstance(result["metadata"]["deep"], dict)

    def test_recursive_list_of_dicts(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"items": [{"name": "a"}, {"name": "b"}], "cycle": 1}
        result = _redact_finding(finding)
        assert len(result["items"]) == 2

    def test_mixed_list(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"mixed": ["text", 42, {"k": "v"}, ["nested"]]}
        result = _redact_finding(finding)
        assert result["mixed"][1] == 42


# --- HTTP Handler tests ---


class TestHTTPHandlers:
    @pytest.fixture
    def app(self, tmp_path: Path):
        from aiohttp import web

        from kiro_claw.apps.builtins.auto_research.handlers import register_routes

        @web.middleware
        async def _inject_user(request, handler):
            request["user"] = "test-user"
            return await handler(request)

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH",
            tmp_path / "t.db",
        ), patch(
            "kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR",
            tmp_path / "r",
        ):
            a = web.Application(middlewares=[_inject_user])
            register_routes(a)
            yield a

    @pytest.mark.asyncio
    async def test_validate(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/validate",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                assert r.status == 200
                assert (await r.json())["can_start"] is True

    @pytest.mark.asyncio
    async def test_suggest_uses_llm_pool(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        class _FakePool:
            async def send(self, prompt: str, timeout: float = 0) -> str:
                return '["What are the constraints?", "What alternatives exist?"]'

            async def shutdown(self) -> None:
                pass

        async with TestClient(TestServer(app)) as c:
            # Inject after startup so the on_startup hook's real pool is replaced.
            app["auto_research_llm_pool"] = _FakePool()
            r = await c.post(
                "/api/apps/auto-research/suggest",
                json={"question": "How should we design the caching layer for this service?"},
            )
            assert r.status == 200
            assert (await r.json())["sub_questions"] == [
                "What are the constraints?",
                "What alternatives exist?",
            ]

    @pytest.mark.asyncio
    async def test_nudge_resumes_needs_input(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.apps.builtins.auto_research import handlers as h

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "questions.json").write_text('{"question": "Which DB?"}')
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": "Use SQLite"}
                )
                assert r.status == 200
                # Answering clears the question and writes the guidance.
                assert not (h._campaign_dir(cid) / "questions.json").exists()
                assert (h._campaign_dir(cid) / "guidance.txt").read_text() == "Use SQLite"

    @pytest.mark.asyncio
    async def test_report_endpoint(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.apps.builtins.auto_research import handlers as h

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Report\nKey finding.")
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}/report")
                assert r.status == 200
                assert "Key finding." in (await r.json())["report"]

    @pytest.mark.asyncio
    async def test_create_list_get(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                assert r.status == 201
                cid = (await r.json())["id"]
                r = await c.get("/api/apps/auto-research/campaigns")
                assert r.status == 200
                assert len(await r.json()) == 1
                r = await c.get(f"/api/apps/auto-research/campaigns/{cid}")
                assert r.status == 200

    @pytest.mark.asyncio
    async def test_action_start_stop(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"}
                )
                assert (await r.json())["status"] == "running"
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "stop"}
                )
                assert (await r.json())["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_delete_campaign(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.apps.builtins.auto_research import handlers as h

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                (h._campaign_dir(cid) / "FINDINGS.md").write_text("# Report")
                r = await c.delete(f"/api/apps/auto-research/campaigns/{cid}")
                assert r.status == 200
                assert (await r.json())["deleted"] is True
                assert await (await c.get("/api/apps/auto-research/campaigns")).json() == []
                assert (await c.delete(f"/api/apps/auto-research/campaigns/{cid}")).status == 404

    @pytest.mark.asyncio
    async def test_action_start_on_running_returns_409(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                await c.patch(f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"})
                # Re-issuing start on a running campaign must be rejected, not relaunch.
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "start"}
                )
                assert r.status == 409

    @pytest.mark.asyncio
    async def test_action_resume_from_failed_clears_error(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                cr = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await cr.json())["id"]
                update_campaign_status(cid, CampaignStatus.FAILED, error_message="stalled")
                # A failed campaign is recoverable: resume must succeed (not 409)
                # and clear the stale failure message.
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "resume"}
                )
                assert r.status == 200
                camp = await (await c.get(f"/api/apps/auto-research/campaigns/{cid}")).json()
                assert camp["status"] == "running"
                assert not camp["error_message"]

    @pytest.mark.asyncio
    async def test_action_unknown(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.patch(
                    f"/api/apps/auto-research/campaigns/{cid}", json={"action": "boom"}
                )
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_nudge(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": "focus"}
                )
                assert r.status == 200

    @pytest.mark.asyncio
    async def test_nudge_empty(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.post(
                    "/api/apps/auto-research/campaigns",
                    json={"question": "How do teams handle rate limiting?", "sources": ["web"]},
                )
                cid = (await r.json())["id"]
                r = await c.post(
                    f"/api/apps/auto-research/campaigns/{cid}/nudge", json={"text": ""}
                )
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_auth_rejected(self, tmp_path: Path):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.apps.builtins.auto_research.handlers import register_routes

        # No middleware → no user set → handlers must reject with 401
        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            no_auth_app = web.Application()
            register_routes(no_auth_app)
            async with TestClient(TestServer(no_auth_app)) as c:
                r = await c.get("/api/apps/auto-research/campaigns")
                assert r.status == 401

    @pytest.mark.asyncio
    async def test_get_invalid_id(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.get("/api/apps/auto-research/campaigns/ZZZZZZZZ")
                assert r.status == 400

    @pytest.mark.asyncio
    async def test_action_nonexistent_404(self, app, tmp_path: Path):
        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_claw.apps.builtins.auto_research.handlers.DB_PATH", tmp_path / "t.db"
        ), patch("kiro_claw.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path / "r"):
            async with TestClient(TestServer(app)) as c:
                r = await c.patch(
                    "/api/apps/auto-research/campaigns/deadbeef", json={"action": "start"}
                )
                assert r.status == 404


class TestUpdateNonexistent:
    def test_update_missing_campaign_returns_error(self):
        assert update_campaign_status("deadbeef", CampaignStatus.RUNNING) == {
            "error": "campaign not found"
        }


class TestRedactCampaignFields:
    def test_redacts_sub_questions_and_sources_json(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_campaign

        c = _redact_campaign(
            {
                "question": "q",
                "name": "n",
                "error_message": None,
                "sub_questions": json.dumps(["how does X work?", "what about Y?"]),
                "sources": json.dumps(["web", "internal"]),
                "success_criteria": "done when build passes",
            }
        )
        # Fields stay JSON-decodable lists after redaction.
        assert isinstance(json.loads(c["sub_questions"]), list)
        assert isinstance(json.loads(c["sources"]), list)
        assert len(json.loads(c["sub_questions"])) == 2
        # success_criteria flows through redaction (benign text unchanged).
        assert c["success_criteria"] == "done when build passes"

    def test_handles_malformed_json_fields(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _redact_campaign

        c = _redact_campaign({"sub_questions": "not json{", "sources": "[bad"})
        # Malformed fields left untouched, no crash.
        assert c["sub_questions"] == "not json{"


# --- Worker loop launch (autonudge) ---


class TestLoopLaunch:
    @pytest.mark.asyncio
    async def test_launch_arms_autonudge(self, monkeypatch):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        state.get_or_create_slot.return_value = SimpleNamespace(key=f"research-{c['id']}")
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        svc.add.assert_awaited_once()
        kw = svc.add.call_args.kwargs
        assert kw["slot_key"] == f"research-{c['id']}"
        assert c["id"] in kw["message"]
        assert state.get_or_create_slot.call_args.kwargs["agent"] == "kiroclaw-research"
        # Worker slot is auto-approved so the loop doesn't stall on tool prompts.
        assert state.get_or_create_slot.return_value._trust is True

    @pytest.mark.asyncio
    async def test_launch_writes_brief(self, monkeypatch):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        c = create_campaign(
            {
                "question": "Compare SQLite and PostgreSQL for desktop apps",
                "sub_questions": ["concurrency model?", "deployment tradeoffs?"],
                "sources": ["web"],
                "success_criteria": "tests pass and build is green",
            }
        )
        svc = MagicMock()
        svc.add = AsyncMock()
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        state = MagicMock()
        state.get_or_create_slot.return_value = SimpleNamespace(key=f"research-{c['id']}")
        await h._launch_loop(SimpleNamespace(app={"state": state}), c["id"])
        brief = (h._campaign_dir(c["id"]) / "brief.md").read_text()
        assert "Compare SQLite and PostgreSQL" in brief
        assert "concurrency model?" in brief
        assert "Definition of Done" in brief
        assert "tests pass and build is green" in brief

    @pytest.mark.asyncio
    async def test_launch_noop_without_service(self, monkeypatch):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        monkeypatch.setattr(h, "_autonudge_instance", lambda: None)
        await h._launch_loop(SimpleNamespace(app={}), "a1b2c3d4")  # must not raise

    @pytest.mark.asyncio
    async def test_stop_removes_loop(self, monkeypatch):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.remove = AsyncMock()
        svc.get_by_slot.return_value = SimpleNamespace(id="loop1")
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=True)
        svc.remove.assert_awaited_once_with("loop1")

    @pytest.mark.asyncio
    async def test_pause_deactivates_loop(self, monkeypatch):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        svc = MagicMock()
        svc.update = AsyncMock()
        svc.get_by_slot.return_value = SimpleNamespace(id="loop1")
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        await h._stop_loop("a1b2c3d4", remove=False)
        svc.update.assert_awaited_once_with("loop1", active=False)


# --- kiroclaw-research core agent install ---


class TestResearchAgentInstall:
    def test_installs_kiroclaw_research(self, monkeypatch, tmp_path):
        from kiro_claw import agent

        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {"name": "kiroclaw", "prompt": "file://x", "mcpServers": {}, "tools": []},
        )
        agent._install_research_agent()
        data = json.loads((tmp_path / "kiroclaw-research.json").read_text())
        assert data["name"] == "kiroclaw-research"
        assert "research" in data["prompt"].lower()


# --- Watchdog unresponsive grace ---


class TestUnresponsiveDeadline:
    def test_generous_floor_and_scaling(self):
        from kiro_claw.apps.builtins.auto_research.handlers import (
            _FIRST_CYCLE_GRACE_SECS,
            _unresponsive_deadline,
        )

        # Small idle -> generous floor (deep research cycles take minutes), not
        # the tight idle*2 that falsely failed healthy slow cycles.
        assert _unresponsive_deadline(60) == _FIRST_CYCLE_GRACE_SECS
        # Large idle scales above the floor.
        assert _unresponsive_deadline(400) == 800


# --- auto_approve (unattended) persistence ---


class TestAutoApprovePersist:
    def test_auto_approve_persists(self):
        c = create_campaign(
            {
                "question": "Research question about something here",
                "sources": ["web"],
                "auto_approve": True,
            }
        )
        assert get_campaign(c["id"])["auto_approve"] == 1

    def test_auto_approve_defaults_off(self):
        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        assert get_campaign(c["id"])["auto_approve"] == 0


# --- D11 clarification questions + question-mode brief ---


class TestClarificationQuestions:
    def test_pending_question_surfaced(self):
        from kiro_claw.apps.builtins.auto_research.handlers import _campaign_dir

        c = create_campaign(
            {"question": "Research question about something here", "sources": ["web"]}
        )
        (_campaign_dir(c["id"]) / "questions.json").write_text(
            json.dumps({"question": "Which framework should I assume?"})
        )
        assert get_campaign(c["id"])["pending_question"] == "Which framework should I assume?"

    def test_brief_question_mode(self):
        from kiro_claw.apps.builtins.auto_research.handlers import (
            _campaign_dir,
            _get_db,
            _write_brief,
        )

        for auto in (True, False):
            c = create_campaign(
                {
                    "question": "Research question about something here",
                    "sources": ["web"],
                    "auto_approve": auto,
                }
            )
            db = _get_db()
            row = db.execute(
                "SELECT question, sub_questions, sources, max_cycles, idle_secs, "
                "success_criteria, auto_approve FROM campaigns WHERE id = ?",
                (c["id"],),
            ).fetchone()
            db.close()
            _write_brief(c["id"], row)
            brief = (_campaign_dir(c["id"]) / "brief.md").read_text()
            # Attended exposes the questions directive; unattended omits it entirely
            # (no LLM-facing "you may ask" — no-pause is code-enforced instead).
            assert ("Questions allowed" in brief) is (not auto)


class TestUnattendedQuestionEnforcement:
    """Code-enforced guarantee: unattended campaigns never pause for input."""

    def _seed(self, tmp_path: Path):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        d = tmp_path / "a1b2c3d4"
        d.mkdir()
        (d / "questions.json").write_text('{"question": "?"}')
        return h, d

    def test_unattended_discards_question_and_does_not_pause(self, tmp_path: Path):
        h, d = self._seed(tmp_path)
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", True) is False
            assert not (d / "questions.json").exists()  # discarded

    def test_attended_keeps_question_and_pauses(self, tmp_path: Path):
        h, d = self._seed(tmp_path)
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", False) is True
            assert (d / "questions.json").exists()  # preserved for the user

    def test_no_question_no_pause(self, tmp_path: Path):
        from kiro_claw.apps.builtins.auto_research import handlers as h

        (tmp_path / "a1b2c3d4").mkdir()
        with patch.object(h, "RESEARCH_DIR", tmp_path):
            assert h._should_pause_for_question("a1b2c3d4", False) is False
