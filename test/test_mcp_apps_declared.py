"""Tests for per-server MCP Apps declaration tri-state reporting.

Covers :meth:`BackendPool.apps_declared_by_server` contract:
- True  = a tools/list response declared at least one ui:// resource
- False = a tools/list was observed and declared nothing
- ABSENT = no listing observed yet (NOT False)

Plus the OR-merge rule for multiple backends on the same server, and
:meth:`GatewayManager.apps_declared` malformed-reply → {} fallback.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey


def _pool_key(server: str = "test-server", agent: str = "agent-a") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="jkl012",
    )


def _mock_backend(key: PoolKey) -> Backend:
    """Minimal mock Backend with fields the method under test reads."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 99999
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    stdin = MagicMock()
    stdin.close = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()

    stdout = MagicMock()

    now = time.monotonic()
    backend = Backend(
        pool_key=key,
        process=proc,
        stdin=stdin,
        stdout=stdout,
        created_at=now,
        last_used_at=now,
    )
    return backend


# -------------------------------------------------------------------
# BackendPool.apps_declared_by_server tests
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_true_when_listing_has_ui_uri() -> None:
    """Backend that saw a listing WITH a ui:// resource → server maps to True."""
    pool = BackendPool(max_backends=4)
    key = _pool_key()
    backend = _mock_backend(key)
    backend._apps_tools_listed = True
    backend._apps_declared_uris = {"render_app": "ui://my-app/index.html"}
    await pool.add(key, backend)

    result = await pool.apps_declared_by_server()
    assert result["test-server"] is True


@pytest.mark.asyncio
async def test_declared_false_when_listing_is_empty() -> None:
    """Backend that saw a listing with NO ui:// resource → server maps to False."""
    pool = BackendPool(max_backends=4)
    key = _pool_key()
    backend = _mock_backend(key)
    backend._apps_tools_listed = True
    backend._apps_declared_uris = {}
    await pool.add(key, backend)

    result = await pool.apps_declared_by_server()
    assert result["test-server"] is False


@pytest.mark.asyncio
async def test_absent_when_no_listing_observed() -> None:
    """Backend that has NOT seen a tools/list → server is ABSENT from mapping."""
    pool = BackendPool(max_backends=4)
    key = _pool_key()
    backend = _mock_backend(key)
    backend._apps_tools_listed = False
    backend._apps_declared_uris = {}
    await pool.add(key, backend)

    result = await pool.apps_declared_by_server()
    assert "test-server" not in result


@pytest.mark.asyncio
async def test_or_merge_two_backends_same_server() -> None:
    """Two backends for one server (different agents) are OR-ed, in EITHER order.

    Both orders are asserted, because insertion order is iteration order and so
    a last-writer-wins bug (``out[name] = bool(...)`` instead of ``or``) still
    yields True when the DECLARING backend happens to be added last — the case
    below with it added FIRST is the one that catches it.
    """
    for declaring_first in (True, False):
        pool = BackendPool(max_backends=4)

        key_a = _pool_key(server="shared-server", agent="agent-a")
        backend_a = _mock_backend(key_a)
        backend_a._apps_tools_listed = True
        backend_a._apps_declared_uris = {"tool": "ui://app/page"}  # declares

        key_b = _pool_key(server="shared-server", agent="agent-b")
        backend_b = _mock_backend(key_b)
        backend_b._apps_tools_listed = True
        backend_b._apps_declared_uris = {}  # no declaration

        first = (key_a, backend_a) if declaring_first else (key_b, backend_b)
        second = (key_b, backend_b) if declaring_first else (key_a, backend_a)
        await pool.add(*first)
        await pool.add(*second)

        result = await pool.apps_declared_by_server()
        assert result["shared-server"] is True, f"declaring_first={declaring_first}"


@pytest.mark.asyncio
async def test_private_backend_is_reported() -> None:
    """A connection-private backend counts, not just a pooled one.

    ``poolable_servers`` defaults to EMPTY, so on a default install every server
    is acquired privately into ``_exclusive`` and nothing lands in ``_backends``.
    A shared-only walk therefore reports "not checked yet" forever for every
    server, however many apps have actually rendered — which is the whole column
    reading unknown on the configuration most users run.
    """
    pool = BackendPool(max_backends=4)
    key = _pool_key(server="private-server")
    backend = _mock_backend(key)
    backend._apps_tools_listed = True
    backend._apps_declared_uris = {"render_app": "ui://private/index.html"}

    async def _spawn() -> Backend:
        return backend

    await pool.acquire_exclusive(key, "stub-uuid-1", _spawn)
    # Precondition: this backend really is private, not in the shared index.
    assert not pool._backends, "test would pass vacuously if it landed in _backends"

    result = await pool.apps_declared_by_server()
    assert result["private-server"] is True


@pytest.mark.asyncio
async def test_private_and_pooled_backends_or_merge() -> None:
    """One server with a pooled backend AND a private one still OR-merges.

    Ordering matters for the same reason as the pooled-only merge test: the
    private map is iterated second, so a declaring PRIVATE backend beside a
    silent pooled one is the case a last-writer-wins bug would get wrong.
    """
    pool = BackendPool(max_backends=4)

    pooled_key = _pool_key(server="mixed-server", agent="agent-a")
    pooled = _mock_backend(pooled_key)
    pooled._apps_tools_listed = True
    pooled._apps_declared_uris = {}  # observed, declares nothing
    await pool.add(pooled_key, pooled)

    private_key = _pool_key(server="mixed-server", agent="agent-b")
    private = _mock_backend(private_key)
    private._apps_tools_listed = True
    private._apps_declared_uris = {"tool": "ui://app/page"}  # declares

    async def _spawn() -> Backend:
        return private

    await pool.acquire_exclusive(private_key, "stub-uuid-2", _spawn)

    result = await pool.apps_declared_by_server()
    assert result["mixed-server"] is True


# -------------------------------------------------------------------
# The REAL harvest path — not a stand-in with the flags hand-set
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_sets_the_observation_flag_and_uris() -> None:
    """Drive `_maybe_intercept_ui_result` with a real tools/list response.

    Every other test in this file hand-sets `_apps_tools_listed`, so none of them
    exercises the production line that WRITES it. Deleting that line therefore
    left the whole suite green while, in production, no server would ever leave
    "not checked yet" — a silent permanent regression. This test closes that hole
    by asserting on the flag AFTER the real method has run.
    """
    from kiro_crew.mcp_gateway.backend import _PendingRequest

    backend = _mock_backend(_pool_key())
    assert backend._apps_tools_listed is False

    pending = _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list")
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "tools": [
                {
                    "name": "draw",
                    "description": "draw something",
                    "inputSchema": {"type": "object"},
                    "_meta": {"ui": {"resourceUri": "ui://excalidraw/canvas"}},
                },
            ],
        },
    }

    intercepted = await backend._maybe_intercept_ui_result(pending, msg)

    # A listing is never "intercepted" — it is harvested and delivered normally.
    assert intercepted is False
    assert backend._apps_tools_listed is True
    assert backend._apps_declared_uris == {"draw": "ui://excalidraw/canvas"}


@pytest.mark.asyncio
async def test_harvest_of_a_listing_without_apps_still_counts_as_observed() -> None:
    """A listing that declares nothing must flip the flag but leave uris empty.

    This is what separates "no app found" from "not checked yet" at the source:
    the flag records that we LOOKED, independently of what we found.
    """
    from kiro_crew.mcp_gateway.backend import _PendingRequest

    backend = _mock_backend(_pool_key())
    pending = _PendingRequest(stub_uuid="stub-1", original_id=1, method="tools/list")
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "plain"}]}}

    await backend._maybe_intercept_ui_result(pending, msg)

    assert backend._apps_tools_listed is True
    assert backend._apps_declared_uris == {}


# -------------------------------------------------------------------
# GatewayManager.apps_declared malformed reply fallback
# -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_apps_declared_returns_empty_on_malformed() -> None:
    """Manager.apps_declared() returns {} when broker reply lacks 'servers' dict."""
    from kiro_crew.mcp_gateway.manager import GatewayManager

    mgr = GatewayManager.__new__(GatewayManager)

    # _query returning a reply without "servers" key
    with patch.object(mgr, "_query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"type": "apps-declared"}  # no 'servers'
        result = await mgr.apps_declared()
        assert result == {}

    # _query returning reply with non-dict servers
    with patch.object(mgr, "_query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"servers": "not-a-dict"}
        result = await mgr.apps_declared()
        assert result == {}
