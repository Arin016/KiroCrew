"""Tests for the per-session Tool Search reload escape hatch (issue #8082).

``AcpProvider.reload_tool_search()`` is KiroCrew's manual lever for the
compaction hole: once kiro-cli defers an MCP tool spec, a later ``/compact``
shrinks the context but does NOT re-inject the deferred specs, so those tools
stay invisible for the rest of the session. The deferral engine lives in
kiro-cli on the far side of the ACP boundary and cannot be fixed here; the one
thing KiroCrew can do is rewrite the ``<work_dir>/.kiro/settings/cli.json``
overlay and RESTART the backend (shutdown + start) so kiro-cli re-reads the
overlay and recomputes deferral against the current (post-``/compact``) context.

These tests mirror the conventions in ``test_acp_tool_search.py`` (the
``_build_provider`` helper patching ``kiro_crew.providers.acp.AcpClient`` with a
MagicMock, the ``tmp_path`` cli.json helper, ``@pytest.mark.asyncio`` for the
async surface). Overlay writes are asserted against the real cli.json contents
via ``json.loads``, and the restart is asserted by spying ``shutdown`` /
``start``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.acp import AcpProvider


def _build_provider(backend: str) -> AcpProvider:
    """Build an AcpProvider with a mocked client (mirrors test_acp_tool_search.py)."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


def _cli_json(tmp_path):
    return tmp_path / ".kiro" / "settings" / "cli.json"


def _kiro_provider(tmp_path):
    """A kiro-backed provider (backend == "" == ACP_BACKEND_KIRO) with Tool
    Search enabled, no active turn, and shutdown/start spied so the restart is
    observable without a live kiro-cli."""
    provider = _build_provider(backend="")
    provider._client._work_dir = tmp_path
    provider._tool_search = True
    provider.has_active_turn = MagicMock(return_value=False)
    provider.shutdown = AsyncMock()
    provider.start = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_kiro_reload_rewrites_overlay_and_restarts(tmp_path):
    # (1) kiro backend, tool_search=True, no active turn -> rewrites the overlay
    # and restarts (shutdown then start), returning True.
    provider = _kiro_provider(tmp_path)

    result = await provider.reload_tool_search()

    assert result is True
    data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
    assert data["toolSearch.enabled"] is True
    provider.shutdown.assert_awaited_once()
    provider.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_threshold_override_written_before_restart(tmp_path):
    # (2) a per-call threshold override lands in cli.json and updates the
    # provider instance, AND the overlay carrying that override is written
    # BEFORE start() is awaited. The ordering matters: start() re-applies the
    # overlay too, but reload_tool_search writes it explicitly first so the
    # override is authoritative regardless of start()'s internal ordering. We
    # enforce it by recording an ordering token from BOTH the overlay-write
    # path (wrapping _apply_tool_search_overlay to snapshot what actually
    # reached disk) and from start(), then asserting the sequence.
    provider = _kiro_provider(tmp_path)
    order: list[str] = []

    real_apply = provider._apply_tool_search_overlay

    def _record_overlay():
        real_apply()
        # Record the override values as they exist on disk at write time, so
        # the assertion proves the OVERRIDDEN overlay (not a later default
        # re-write) preceded the restart.
        on_disk = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        order.append(("overlay", on_disk["toolSearch.minPct"], on_disk["toolSearch.minTokens"]))

    provider._apply_tool_search_overlay = _record_overlay
    provider.start.side_effect = lambda: order.append(("start",))

    result = await provider.reload_tool_search(min_pct=12, min_tokens=1234)

    assert result is True
    assert provider._tool_search_min_pct == 12
    assert provider._tool_search_min_tokens == 1234
    data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
    assert data["toolSearch.minPct"] == 12
    assert data["toolSearch.minTokens"] == 1234
    # The override overlay was written, carried the override, and did so before
    # start() ran.
    assert order == [("overlay", 12, 1234), ("start",)]


@pytest.mark.asyncio
async def test_claude_backend_returns_false_no_restart(tmp_path):
    # (3) claude backend -> returns False and never restarts.
    provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
    provider._client._work_dir = tmp_path
    provider._tool_search = True
    provider.has_active_turn = MagicMock(return_value=False)
    provider.shutdown = AsyncMock()
    provider.start = AsyncMock()

    result = await provider.reload_tool_search()

    assert result is False
    provider.shutdown.assert_not_awaited()
    provider.start.assert_not_awaited()
    assert not _cli_json(tmp_path).exists()


@pytest.mark.asyncio
async def test_tool_search_none_returns_false_no_restart(tmp_path):
    # (4) self._tool_search is None (unmanaged overlay) -> False, no restart.
    provider = _kiro_provider(tmp_path)
    provider._tool_search = None

    result = await provider.reload_tool_search()

    assert result is False
    provider.shutdown.assert_not_awaited()
    provider.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_search_false_returns_false_no_restart(tmp_path):
    # (5) self._tool_search is False (nothing deferred) -> False, no restart.
    provider = _kiro_provider(tmp_path)
    provider._tool_search = False

    result = await provider.reload_tool_search()

    assert result is False
    provider.shutdown.assert_not_awaited()
    provider.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_turn_returns_false_no_restart(tmp_path):
    # (6) a turn is active -> False, no restart (never race a streaming prompt).
    provider = _kiro_provider(tmp_path)
    provider.has_active_turn = MagicMock(return_value=True)

    result = await provider.reload_tool_search()

    assert result is False
    provider.shutdown.assert_not_awaited()
    provider.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_failure_rolls_back_thresholds(tmp_path):
    # (7) start() raises -> the thresholds roll back to the pre-call snapshot
    # and the overlay reflects the rollback (not the attempted override).
    provider = _kiro_provider(tmp_path)
    provider._tool_search_min_pct = 7
    provider._tool_search_min_tokens = 777
    provider.start.side_effect = RuntimeError("spawn failed")

    with pytest.raises(RuntimeError, match="spawn failed"):
        await provider.reload_tool_search(min_pct=42, min_tokens=9999)

    # Thresholds are back to the pre-call values.
    assert provider._tool_search_min_pct == 7
    assert provider._tool_search_min_tokens == 777
    # And the overlay on disk reflects the rollback, not the failed override.
    data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
    assert data["toolSearch.minPct"] == 7
    assert data["toolSearch.minTokens"] == 777
