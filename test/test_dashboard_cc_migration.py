"""Tests for /api/cc/* dashboard endpoints (kiro→CC migration UI)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.dashboard.handlers.agents import (
    _summarize_mirror_result,
    api_cc_aim_missing,
    api_cc_aim_sync,
    api_cc_mirror_preview,
    api_cc_mirror_run,
)


def _request(body: dict | None = None) -> MagicMock:
    mock_state = MagicMock()
    request = MagicMock()
    request.app = {"state": mock_state}
    request.json = AsyncMock(return_value=body if body is not None else {})
    request.get = MagicMock(return_value="anonymous")
    return request


_EMPTY_MIRROR = {"agents": [], "mcp": [], "skills": [], "errors": []}


_SAMPLE_MIRROR = {
    "agents": [
        {"name": "alpha", "action": "mirrored"},
        {"name": "beta", "action": "skipped (already exists)"},
        {"name": "gamma", "action": "skipped_aim"},
    ],
    "mcp": [{"name": "mcp.json", "action": "mirrored", "count": 4}],
    "skills": [{"name": "code-search", "action": "mirrored"}],
    "errors": [],
}


class TestSummarizer:
    def test_empty(self):
        s = _summarize_mirror_result(_EMPTY_MIRROR)
        assert s == {
            "agents_total": 0,
            "mcp_total": 0,
            "skills_total": 0,
            "mirrored": 0,
            "skipped": 0,
            "errors": 0,
        }

    def test_counts(self):
        s = _summarize_mirror_result(_SAMPLE_MIRROR)
        assert s["agents_total"] == 3
        assert s["mcp_total"] == 1
        assert s["skills_total"] == 1
        assert s["mirrored"] == 3  # alpha + mcp + skill
        assert s["skipped"] == 2  # beta + gamma
        assert s["errors"] == 0


class TestMirrorPreview:
    @pytest.mark.asyncio
    async def test_empty_kiro_returns_zero_counts(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_EMPTY_MIRROR,
        ) as mocked:
            resp = await api_cc_mirror_preview(_request())
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["summary"]["mirrored"] == 0
        assert body["summary"]["agents_total"] == 0
        # dry_run must be True for preview
        mocked.assert_called_once_with(dry_run=True, force=False)

    @pytest.mark.asyncio
    async def test_populated_kiro_reports_counts(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_SAMPLE_MIRROR,
        ):
            resp = await api_cc_mirror_preview(_request())
        body = json.loads(resp.body)
        assert body["summary"]["mirrored"] == 3
        assert body["summary"]["skipped"] == 2
        assert len(body["agents"]) == 3

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            side_effect=RuntimeError("disk full"),
        ):
            resp = await api_cc_mirror_preview(_request())
        assert resp.status == 500
        body = json.loads(resp.body)
        assert "disk full" in body["error"]


class TestMirrorRun:
    @pytest.mark.asyncio
    async def test_force_flag_propagates(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_SAMPLE_MIRROR,
        ) as mocked:
            resp = await api_cc_mirror_run(_request({"force": True}))
        assert resp.status == 200
        mocked.assert_called_once_with(dry_run=False, force=True)

    @pytest.mark.asyncio
    async def test_default_force_false(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_SAMPLE_MIRROR,
        ) as mocked:
            await api_cc_mirror_run(_request({}))
        _, kwargs = mocked.call_args
        assert kwargs == {"dry_run": False, "force": False}

    @pytest.mark.asyncio
    async def test_pushes_refresh(self):
        req = _request({})
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_SAMPLE_MIRROR,
        ):
            await api_cc_mirror_run(req)
        req.app["state"].push_refresh.assert_any_call("agents")
        req.app["state"].push_refresh.assert_any_call("skills")

    @pytest.mark.asyncio
    async def test_partial_outcome_when_errors(self):
        with_errors = {
            **_SAMPLE_MIRROR,
            "errors": [{"source": "/tmp/bad.json", "error": "permission denied"}],
        }
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=with_errors,
        ):
            resp = await api_cc_mirror_run(_request({"force": False}))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["summary"]["errors"] == 1
        assert body["errors"][0]["source"].endswith("bad.json")

    @pytest.mark.asyncio
    async def test_invalid_json_body_defaults(self):
        # request.json raises -> handler treats body as {}
        req = _request({})
        req.json = AsyncMock(side_effect=ValueError("not json"))
        with patch(
            "kiro_claw.dashboard.handlers.agents.mirror_kiro_to_cc",
            return_value=_EMPTY_MIRROR,
        ) as mocked:
            resp = await api_cc_mirror_run(req)
        assert resp.status == 200
        mocked.assert_called_once_with(dry_run=False, force=False)


class TestAimMissing:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc",
            return_value=["foo/bar", "baz"],
        ):
            resp = await api_cc_aim_missing(_request())
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["missing"] == ["foo/bar", "baz"]

    @pytest.mark.asyncio
    async def test_empty(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc",
            return_value=[],
        ):
            resp = await api_cc_aim_missing(_request())
        body = json.loads(resp.body)
        assert body["missing"] == []

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc",
            side_effect=OSError("aim missing"),
        ):
            resp = await api_cc_aim_missing(_request())
        assert resp.status == 500


class TestAimSync:
    @pytest.mark.asyncio
    async def test_install_all_missing_when_no_packages(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc",
            return_value=["alpha", "beta"],
        ), patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin",
            return_value=(True, "ok"),
        ) as install:
            resp = await api_cc_aim_sync(_request({}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["installed"] == ["alpha", "beta"]
        assert body["failed"] == []
        assert install.call_count == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        results = {
            "alpha": (True, "ok"),
            "beta": (False, "network down"),
        }
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc",
            return_value=["alpha", "beta"],
        ), patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin",
            side_effect=lambda pkg, **_: results[pkg],
        ):
            resp = await api_cc_aim_sync(_request(None))
        body = json.loads(resp.body)
        assert body["installed"] == ["alpha"]
        assert body["failed"] == [{"package": "beta", "error": "network down"}]

    @pytest.mark.asyncio
    async def test_explicit_packages_overrides_missing_lookup(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc"
        ) as missing, patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin",
            return_value=(True, "ok"),
        ) as install:
            resp = await api_cc_aim_sync(_request({"packages": ["only/this"]}))
        assert resp.status == 200
        # No fallback to the missing lookup when caller named a specific pkg
        missing.assert_not_called()
        install.assert_called_once()
        args, kwargs = install.call_args
        assert args[0] == "only/this"
        assert kwargs == {"standalone": True}

    @pytest.mark.asyncio
    async def test_empty_packages_list_installs_nothing(self):
        # Per API contract, packages=[] means "install nothing"; only
        # packages=null falls back to installing all missing (AutoSDE r1 #23).
        with patch(
            "kiro_claw.dashboard.handlers.agents.installed_kiro_packages_missing_from_cc"
        ) as missing, patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin"
        ) as install:
            resp = await api_cc_aim_sync(_request({"packages": []}))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body == {"installed": [], "failed": []}
        missing.assert_not_called()
        install.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_invalid_package_names(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin"
        ) as install:
            resp = await api_cc_aim_sync(_request({"packages": ["good", "../bad"]}))
        assert resp.status == 400
        install.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_non_list_packages(self):
        resp = await api_cc_aim_sync(_request({"packages": "alpha"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_install_exception_recorded_as_failure(self):
        with patch(
            "kiro_claw.dashboard.handlers.agents.install_cc_plugin",
            side_effect=RuntimeError("boom"),
        ):
            resp = await api_cc_aim_sync(_request({"packages": ["alpha"]}))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["installed"] == []
        assert body["failed"][0]["package"] == "alpha"
        assert "boom" in body["failed"][0]["error"]
