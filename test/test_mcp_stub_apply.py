"""The broker's existence follows the stub set, in all three directions.

Found on a real pod: stubbing the FIRST server reported ok but started nothing,
because the apply path only ever restarted an already-running broker. That state
— no manager yet, because nothing had been stubbed — is created by making the stub
opt-in, so it had no prior coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack.gateway import GatewayOrchestrator


def _orch(stub: list[str]) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=list(stub)))
    orch._mcp_gateway_manager = None
    orch.dashboard_state = SimpleNamespace(_mcp_gateway_manager=None)
    # Real interface, not a bare attribute: the apply path must refresh the
    # session defaults so the NEXT session is launched with the new routing
    # instead of the provider factory's boot-time capture.
    orch.sessions = SimpleNamespace(refresh_defaults=AsyncMock())
    return orch


@pytest.mark.asyncio
async def test_stubbing_the_first_server_starts_the_broker(monkeypatch) -> None:
    orch = _orch(["alpha-mcp"])
    manager = MagicMock()
    calls: list[str] = []

    async def _init() -> None:
        calls.append("init")
        orch._mcp_gateway_manager = manager

    async def _stop() -> None:  # pragma: no cover - must not run
        calls.append("stop")

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert calls == ["init"], "the first stubbed server must START a broker, not skip"
    assert out["applied"] is True
    assert out["stub_servers"] == ["alpha-mcp"]
    # The dashboard reads the manager off state; a start nobody published is invisible.
    assert orch.dashboard_state._mcp_gateway_manager is manager
    orch.sessions.refresh_defaults.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unstubbing_the_last_server_stops_the_broker(monkeypatch) -> None:
    orch = _orch([])
    orch._mcp_gateway_manager = MagicMock()
    calls: list[str] = []

    async def _init() -> None:  # pragma: no cover - must not run
        calls.append("init")

    async def _stop() -> None:
        calls.append("stop")
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert calls == ["stop"], "an empty stub set has nothing for a broker to serve"
    assert out["applied"] is True
    assert orch.dashboard_state._mcp_gateway_manager is None


@pytest.mark.asyncio
async def test_changing_the_set_republishes_without_restarting(monkeypatch) -> None:
    """The rewriter still re-runs, but the daemon is left alone.

    The rewriter reads the stub set, so re-running it is what re-emits the
    stubs and republishes the target table. Respawning the daemon to achieve
    that drained every pooled backend and every in-flight call for one
    server's bit -- and could not help the sessions that were already open,
    whose toolset is fixed at ``session/new``.
    """
    orch = _orch(["alpha-mcp", "beta-mcp"])
    serving = MagicMock(name="serving")
    orch._mcp_gateway_manager = serving
    calls: list[str] = []

    async def _init() -> None:  # pragma: no cover - must not run
        calls.append("init")

    async def _stop() -> None:  # pragma: no cover - must not run
        calls.append("stop")

    async def _rewrite() -> dict[str, str]:
        calls.append("rewrite")
        return {"KIROCREW_MCP_TARGET_ALPHA_MCP": "alpha-mcp"}

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(orch, "_rewrite_mcp_overlay", _rewrite)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    # The rewrite still runs -- it is what republishes the table a serving
    # daemon reads -- but the daemon itself is left alone.
    assert calls == ["rewrite"], "a serving broker must not be torn down to re-route"
    assert orch._mcp_gateway_manager is serving
    assert orch.dashboard_state._mcp_gateway_manager is serving
    assert out["applied"] is True
    assert out["stub_servers"] == ["alpha-mcp", "beta-mcp"]
    orch.sessions.refresh_defaults.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_failed_rewrite_is_not_reported_as_applied(monkeypatch) -> None:
    """A rewrite that failed leaves the previous routing live.

    Reporting it as applied would draw a settled switch over a change the
    broker never saw, which is worse than the error the dashboard shows.
    """
    orch = _orch(["alpha-mcp"])
    serving = MagicMock(name="serving")
    orch._mcp_gateway_manager = serving

    async def _rewrite() -> None:
        return None

    monkeypatch.setattr(orch, "_rewrite_mcp_overlay", _rewrite)
    monkeypatch.setattr(orch, "_init_mcp_gateway", AsyncMock())
    monkeypatch.setattr(orch, "_stop_mcp_broker", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert out["applied"] is False
    assert orch._mcp_gateway_manager is serving


@pytest.mark.asyncio
async def test_the_response_names_the_routed_set_not_the_deprecated_key(monkeypatch) -> None:
    """The dashboard reads this payload back; echoing `poolable_servers` would
    report a key the config no longer drives."""
    orch = _orch(["alpha-mcp"])

    async def _init() -> None:
        orch._mcp_gateway_manager = MagicMock()

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert "stub_servers" in out
    assert "poolable_servers" not in out


# ── _rewrite_mcp_overlay itself ────────────────────────────────────────────
# The tests above mock this method out to exercise the apply's branching, so
# its OWN two decisions -- whether to publish, and what a failed publish means
# -- need exercising here or they are untested.


def _rewrite_orch(tmp_path) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = SimpleNamespace(
        mcp_gateway=SimpleNamespace(
            stub_servers=["alpha-mcp"],
            socket_path=str(tmp_path / "gateway.sock"),
            overlay_dir=str(tmp_path / "overlay"),
            enabled=True,
        ),
        agent=SimpleNamespace(sandbox="off", approval_mode="interactive"),
    )
    return orch


@pytest.mark.asyncio
async def test_the_mapping_is_published_on_every_build(monkeypatch, tmp_path) -> None:
    """Publishing on EVERY build is what removes the freshness question.

    A table that exists cannot be older than the environment of any daemon that
    could read it, so the daemon needs no generation, clock or process-start
    comparison to decide whether its copy is current -- and none of those can be
    skewed by a clock step, a VM restore, or a supervisor respawn.
    """
    orch = _rewrite_orch(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.rewrite_agents",
        lambda **_kw: (None, {"KIROCREW_MCP_TARGET_ALPHA_MCP": "alpha-mcp"}),
    )

    def _write(path, targets):
        seen["path"] = path
        seen["targets"] = targets
        return True

    monkeypatch.setattr("kiro_crew.slack.gateway.write_target_table", _write)

    out = await orch._rewrite_mcp_overlay()

    assert out == {"KIROCREW_MCP_TARGET_ALPHA_MCP": "alpha-mcp"}
    assert seen["path"] == tmp_path / "targets.json"
    assert seen["targets"] == {"KIROCREW_MCP_TARGET_ALPHA_MCP": "alpha-mcp"}


@pytest.mark.asyncio
async def test_a_publish_that_fails_returns_none(monkeypatch, tmp_path) -> None:
    """Fail closed: a daemon reads the table as authoritative.

    Serving on an unpublished or half-written table would route from a stub set
    nobody asked for, so neither a broker start nor an applied report may
    proceed on one.
    """
    orch = _rewrite_orch(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.rewrite_agents",
        lambda **_kw: (None, {"KIROCREW_MCP_TARGET_ALPHA_MCP": "alpha-mcp"}),
    )
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.write_target_table", lambda path, targets: False
    )

    assert await orch._rewrite_mcp_overlay() is None


@pytest.mark.asyncio
async def test_a_failed_rewrite_never_publishes(monkeypatch, tmp_path) -> None:
    """A half-built mapping must not reach the daemon."""
    orch = _rewrite_orch(tmp_path)
    writes: list[object] = []

    def _boom(**_kw):
        raise RuntimeError("rewriter exploded")

    monkeypatch.setattr("kiro_crew.slack.gateway.rewrite_agents", _boom)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.write_target_table",
        lambda path, targets: writes.append(path) or True,
    )

    assert await orch._rewrite_mcp_overlay() is None
    assert writes == []
