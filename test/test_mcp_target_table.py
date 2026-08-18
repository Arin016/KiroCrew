"""The published target table, and what it is allowed to override.

The daemon used to read its routing only from its own environment, which a live
process cannot change -- so changing which servers are stubbed meant respawning
it and draining every pooled backend. These tests pin the file that replaces
that.

Two properties carry the design, and both are here because getting either wrong
is silent. Precedence is per TABLE, so unstubbing takes effect. And a backend is
spawned from the mapping on disk AT THAT MOMENT: the acquisition reloads before
resolving, so neither a just-published server nor a just-changed command can be
served from a stale cached copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.mcp_gateway.gatewayd import (
    _acquire_backend,
    _TargetUnknown,
    default_target_cache,
    env_target_resolver,
    make_target_table_resolver,
)
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.mcp_gateway.target_table import (
    TARGET_TABLE_VERSION,
    TargetTableCache,
    TargetTableReader,
    default_target_table_path,
    lookup_target,
    write_target_table,
)
from kiro_crew.platform_compat import IS_POSIX

_HASH = "abc123"


def _pool_key(server_name: str = "test-srv") -> PoolKey:
    return PoolKey(
        server_name=server_name,
        agent_name="kirocrew",
        command_args_hash=_HASH,
        effective_env_hash="e",
        work_dir="/tmp/w",
        binary_version="1",
        os_uid=1000,
        sandbox_mode="off",
        autoapprove_set_hash="a",
        approval_mode="interactive",
        trust_all_tools=False,
        config_snapshot_hash="c",
    )


def _cache(path: Path) -> TargetTableCache:
    """A cache refreshed once, the way the acquisition would.

    The resolver deliberately cannot load the file itself, so a test that never
    refreshed would assert against an empty cache instead of the table.
    """
    cache = TargetTableCache(TargetTableReader(path))
    cache.refresh()
    return cache


def _write_raw(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _clear_env() -> None:
    """Remove every key the env resolver could read for the test server."""
    for key in (
        "KIROCREW_MCP_TARGET_TEST_SRV",
        f"KIROCREW_MCP_TARGET_TEST_SRV__{_HASH}",
        "MC_MCP_TARGET_TEST_SRV",
        f"MC_MCP_TARGET_TEST_SRV__{_HASH}",
    ):
        os.environ.pop(key, None)


# ── Precedence ────────────────────────────────────────────────────────────


def test_table_resolves_a_server_the_env_does_not_name(tmp_path: Path) -> None:
    """A server stubbed after the daemon started must still resolve."""
    table = tmp_path / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/srv --stdio"})
    resolve = make_target_table_resolver(_cache(table))

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        result = resolve(_pool_key())

    assert result is not None, "the published table is the daemon's live routing"
    command, args, _env, _work_dir = result
    assert command == "/usr/bin/srv"
    assert args == ["--stdio"]


def test_table_miss_does_not_fall_back_to_the_env(tmp_path: Path) -> None:
    """Precedence is per table, which is what makes UNSTUBBING take effect.

    A daemon's environment still names every server that was stubbed when it
    spawned. Merging the two sources key-by-key would let that entry keep
    resolving a server the operator just unstubbed, so the removal would appear
    to do nothing until the next restart -- the exact coupling this file exists
    to remove.
    """
    table = tmp_path / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_OTHER_SRV": "/usr/bin/other"})
    resolve = make_target_table_resolver(_cache(table))

    with patch.dict(
        os.environ, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/stale"}, clear=False
    ):
        assert resolve(_pool_key()) is None


def test_absent_table_falls_back_to_the_env(tmp_path: Path) -> None:
    """The environment is the floor.

    It is always present at spawn, so a daemon whose publish failed, or one from
    a build that never wrote a table, keeps resolving exactly as before rather
    than failing every spawn.
    """
    resolve = make_target_table_resolver(_cache(tmp_path / "missing.json"))

    with patch.dict(
        os.environ, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/from-env"}, clear=False
    ):
        result = resolve(_pool_key())

    assert result is not None
    assert result[0] == "/usr/bin/from-env"


def test_an_empty_table_resolves_nothing(tmp_path: Path) -> None:
    """An empty table is a real state -- nothing is stubbed -- not a fallback."""
    table = tmp_path / "targets.json"
    assert write_target_table(table, {})
    resolve = make_target_table_resolver(_cache(table))

    with patch.dict(
        os.environ, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/stale"}, clear=False
    ):
        assert resolve(_pool_key()) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param({"version": TARGET_TABLE_VERSION + 99, "targets": {}}, id="version"),
        pytest.param({"version": TARGET_TABLE_VERSION, "targets": []}, id="shape"),
        pytest.param(
            {"version": TARGET_TABLE_VERSION, "targets": {"KIROCREW_MCP_TARGET_TEST_SRV": 7}},
            id="non-string-value",
        ),
    ],
)
def test_an_unusable_table_is_ignored_in_favour_of_the_env(
    tmp_path: Path, payload: object
) -> None:
    """Every "cannot trust this file" case degrades to the environment."""
    table = tmp_path / "targets.json"
    if isinstance(payload, str):
        table.write_text(payload, encoding="utf-8")
    else:
        _write_raw(table, payload)
    resolve = make_target_table_resolver(_cache(table))

    with patch.dict(
        os.environ, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/from-env"}, clear=False
    ):
        result = resolve(_pool_key())

    assert result is not None, "an unusable table must not break every spawn"
    assert result[0] == "/usr/bin/from-env"


@pytest.mark.skipif(not IS_POSIX, reason="mode bits carry no ACL information here")
def test_a_group_writable_table_is_refused(tmp_path: Path) -> None:
    """The table decides which executables the daemon launches.

    A file another account can write must not be honoured, so the check refuses
    it and the environment -- which only whoever spawns the daemon can set --
    remains the answer.
    """
    table = tmp_path / "targets.json"
    _write_raw(
        table,
        {
            "version": TARGET_TABLE_VERSION,
            "targets": {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/attacker"},
        },
    )
    table.chmod(0o660)
    resolve = make_target_table_resolver(_cache(table))

    with patch.dict(
        os.environ, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/from-env"}, clear=False
    ):
        result = resolve(_pool_key())

    assert result is not None
    assert result[0] == "/usr/bin/from-env", "a group-writable table must not be trusted"


@pytest.mark.skipif(not IS_POSIX, reason="mode bits carry no ACL information here")
def test_the_published_table_is_owner_only(tmp_path: Path) -> None:
    table = tmp_path / "nested" / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/srv"})
    assert stat.S_IMODE(table.stat().st_mode) == 0o600


# ── Freshness: the acquisition reads the table AT SPAWN TIME ──────────────


def test_the_resolver_itself_does_no_filesystem_io(tmp_path: Path) -> None:
    """The resolver runs on the event loop, so the read is not its job.

    Reloading belongs to the acquisition, which is async and offloads it -- the
    same split the sibling declared-env read on this path already uses.
    """
    table = tmp_path / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/srv"})
    resolve = make_target_table_resolver(_cache(table))

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the resolver must not touch the filesystem")

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        with patch.object(Path, "stat", _boom), patch.object(Path, "read_text", _boom):
            result = resolve(_pool_key())

    assert result is not None
    assert result[0] == "/usr/bin/srv"


def test_a_refresh_picks_up_a_rewritten_table(tmp_path: Path) -> None:
    """Routing changes under a daemon that keeps serving."""
    table = tmp_path / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/first"})
    cache = _cache(table)
    resolve = make_target_table_resolver(cache)

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        first = resolve(_pool_key())
        assert write_target_table(
            table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/second"}
        )
        cache.refresh()
        second = resolve(_pool_key())

    assert first is not None and first[0] == "/usr/bin/first"
    assert second is not None and second[0] == "/usr/bin/second"


async def _acquire(cache: TargetTableCache, *, wired: bool):
    async def _refresh() -> None:
        await asyncio.to_thread(cache.refresh)

    return await _acquire_backend(
        BackendPool(max_backends=1),
        _pool_key(),
        make_target_table_resolver(cache),
        refresh_targets=_refresh if wired else None,
    )


def test_a_spawn_reloads_before_resolving_an_unknown_target(tmp_path: Path) -> None:
    """A server stubbed since the last reload must not be reported unknown.

    The stub treats an unknown target as TERMINAL and deliberately does NOT fall
    back to a per-session exec, so that a genuinely broken backend cannot
    crash-loop per session. An unknown target must therefore be exact: a session
    that asked for a just-stubbed server would otherwise lose it for its whole
    life.
    """
    table = tmp_path / "targets.json"
    cache = _cache(table)  # nothing published when the cache was loaded

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        assert write_target_table(
            table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/nonexistent/srv"}
        )

        with pytest.raises(_TargetUnknown):
            asyncio.run(_acquire(_cache(tmp_path / "missing.json"), wired=False))

        # With the reload wired, the mapping is found. The spawn then fails on
        # the bogus command, which is a DIFFERENT error -- reaching it proves the
        # target resolved rather than being rejected as unknown.
        with pytest.raises(Exception) as seen:
            asyncio.run(_acquire(cache, wired=True))
        assert not isinstance(seen.value, _TargetUnknown)


def test_a_spawn_reloads_even_when_the_cached_lookup_would_succeed(
    tmp_path: Path,
) -> None:
    """A STALE SUCCESS is the case a miss-only reload leaves broken.

    When a server's target command changes, the cached copy still resolves --
    successfully, to the previous command. Reloading only on a miss would spawn
    that old executable for as long as the cached copy survived, so the reload
    happens before resolving rather than after a failure.
    """
    table = tmp_path / "targets.json"
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/bin/old-cmd"})
    cache = _cache(table)
    # The operator changes the target; the cache still holds the old one.
    assert write_target_table(table, {"KIROCREW_MCP_TARGET_TEST_SRV": "/bin/new-cmd"})
    assert cache.current() == {"KIROCREW_MCP_TARGET_TEST_SRV": "/bin/old-cmd"}

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        with pytest.raises(Exception):
            asyncio.run(_acquire(cache, wired=True))

    # The reload ran as part of the acquisition, not as a retry after a miss.
    assert cache.current() == {"KIROCREW_MCP_TARGET_TEST_SRV": "/bin/new-cmd"}


def test_the_daemon_reads_the_table_beside_its_socket(tmp_path: Path) -> None:
    """The WIRING: the writer is the gateway, the reader is the daemon.

    Both derive the path from the same socket, so a mismatch would silently
    disable the file rather than fail.
    """
    socket_path = tmp_path / "gateway.sock"
    assert write_target_table(
        default_target_table_path(socket_path),
        {"KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/published"},
    )
    cache = default_target_cache(socket_path)
    cache.refresh()

    with patch.dict(os.environ, {}, clear=False):
        _clear_env()
        result = make_target_table_resolver(cache)(_pool_key())

    assert result is not None
    assert result[0] == "/usr/bin/published"


# ── Lookup order, shared by both sources ──────────────────────────────────


def test_the_table_is_write_gated_against_the_agent_on_both_paths() -> None:
    """The reader's owner/mode check cannot refuse a SAME-UID write.

    It refuses other accounts, which is what makes a foreign-owned table
    untrusted -- but the table maps a server name to a command the broker execs,
    and the broker does not run under the agent's sandbox. So the agent's own
    file and shell tools are refused the write by the same registry that already
    covers the browse launch config and the on-call schedule: inputs to a
    security or authorization decision that must stay readable. Kiro Crew
    publishes the file directly, not through the gate, so its own write works.

    Both registries are asserted because a leaf protected on one path only is
    reachable through the other, and the gated path is derived from
    ``default_target_table_path`` rather than written out again -- otherwise
    renaming the runtime directory would silently move the file out from under
    the gate while every other test still passed.
    """
    from kiro_crew import security
    from kiro_crew.mcp_gateway.rewriter import default_socket_path

    table = default_target_table_path(default_socket_path())
    leaf = f"{table.parent.name}/{table.name}"

    assert leaf in security._WRITE_PROTECTED_BASH_LEAVES
    for prefix in security.crew_home_prefixes():
        assert f"{prefix}/{leaf}" in security._WRITE_PROTECTED_HOME_PATHS


def test_lookup_prefers_the_args_hashed_key() -> None:
    """Two agents sharing a server name with different target args."""
    mapping = {
        "KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/bare",
        f"KIROCREW_MCP_TARGET_TEST_SRV__{_HASH}": "/usr/bin/hashed",
    }
    assert lookup_target(mapping, "test-srv", _HASH) == "/usr/bin/hashed"


def test_lookup_accepts_the_legacy_prefix() -> None:
    """An overlay written before the rename still resolves."""
    mapping = {"MC_MCP_TARGET_TEST_SRV": "/usr/bin/legacy"}
    assert lookup_target(mapping, "test-srv", _HASH) == "/usr/bin/legacy"


def test_lookup_prefers_the_current_prefix_over_the_legacy_one() -> None:
    mapping = {
        "KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/current",
        "MC_MCP_TARGET_TEST_SRV": "/usr/bin/legacy",
    }
    assert lookup_target(mapping, "test-srv", _HASH) == "/usr/bin/current"


def test_both_sources_share_one_lookup_order(tmp_path: Path) -> None:
    """The env resolver and the table resolver must agree about which key wins.

    They are two callers of the same lookup; a second copy of the order in
    either one would be free to drift, and the symptom would be a server that
    resolves to another agent's command.
    """
    resolve = make_target_table_resolver(_cache(tmp_path / "missing.json"))
    env = {
        "KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/bare",
        f"KIROCREW_MCP_TARGET_TEST_SRV__{_HASH}": "/usr/bin/hashed",
    }
    with patch.dict(os.environ, env, clear=False):
        via_table_resolver = resolve(_pool_key())
        via_env_resolver = env_target_resolver(_pool_key())

    assert via_table_resolver == via_env_resolver
    assert via_env_resolver is not None
    assert via_env_resolver[0] == "/usr/bin/hashed"


def test_default_path_sits_beside_the_socket() -> None:
    assert default_target_table_path(Path("/run/kc/gateway.sock")) == Path(
        "/run/kc/targets.json"
    )
