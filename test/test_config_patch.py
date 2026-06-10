"""Tests for PATCH /api/config/kiroclaw validators (enum, int, float, bool, str)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _make_app() -> web.Application:
    from kiro_claw.dashboard.handlers import api_kiroclaw_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kiroclaw", api_kiroclaw_config_patch)
    return app


def _seed_config() -> dict:
    return {
        "agents": {"kiroclaw": {"kiro_agent": "kiroclaw", "workspace": "default", "memory_store": "default"}},
        "default_agent": "kiroclaw",
        "session": {"pool_agent": "", "timeout_secs": 3600, "autocompact_pct": 50.0},
        "agent": {"approval_mode": "auto", "sandbox": "auto", "enforce_denied_commands": "all"},
        "auto_update": False,
    }


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
    with patch("kiro_claw.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, path, value):
    return await client.patch("/api/config/kiroclaw", json={"path": path, "value": value})


# ── General ──────────────────────────────────────────────────────────────

class TestPatchGeneral:
    @pytest.mark.asyncio
    async def test_unknown_field_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "nonexistent.field", "x")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await c.patch("/api/config/kiroclaw", data=b"not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400


# ── Enum validator ───────────────────────────────────────────────────────

class TestEnumValidator:
    @pytest.mark.asyncio
    async def test_valid_enum_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", "interactive")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_enum_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", "bogus")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_enum_wrong_type_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", 123)
            assert resp.status == 400


# ── Int validator ────────────────────────────────────────────────────────

class TestIntValidator:
    @pytest.mark.asyncio
    async def test_valid_int_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", 120)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_int_below_min_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", -1)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_int_above_max_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", 100000)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_int_non_numeric_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", "abc")
            assert resp.status == 400


# ── Float validator ──────────────────────────────────────────────────────

class TestFloatValidator:
    @pytest.mark.asyncio
    async def test_valid_float_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 25.0)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_float_below_min_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 1.0)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_above_max_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 95.0)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_nan_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", float("nan"))
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_non_numeric_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", "abc")
            assert resp.status == 400


# ── Bool validator ───────────────────────────────────────────────────────

class TestBoolValidator:
    @pytest.mark.asyncio
    async def test_valid_bool_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "auto_update", True)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_bool_non_bool_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "auto_update", "true")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_instances_enabled_toggle(self, tmp_config) -> None:
        # The Instances settings panel flips instances.enabled via this endpoint.
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "instances.enabled", True)
            assert resp.status == 200
            resp = await _patch(c, "instances.enabled", "yes")  # non-bool rejected
            assert resp.status == 400
        # value is written nested under the instances section
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["instances"]["enabled"] is True


# ── Str validator (pool_agent) ───────────────────────────────────────────

class TestStrValidator:
    @pytest.mark.asyncio
    async def test_valid_agent_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "kiroclaw")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_empty_string_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_string_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", 123)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_exceeds_max_len_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "a" * 257)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "nonexistent")
            assert resp.status == 400
            data = await resp.json()
            assert "invalid value" in data["error"]
