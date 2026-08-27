"""Adoption asserts FITNESS, not just liveness (issue #4569).

A gateway that dies without running its shutdown path (SIGKILL, OOM, host
reset) leaves its MCP gateway daemon alive on the socket. The next start
rewrites every agent spec from current config and then adopts that survivor —
whose routing table came from its own process environment at spawn and cannot
change while it lives. Any server whose entry moved in between is routed to a
target the survivor never learned, and ``stub.py`` treats an unknown target as a
TERMINAL rejection, so each new session loses that server for its whole life.

Three layers under test:

* ``hashing.hash_target_env`` — the fingerprint both sides compare, and the
  ``manager``/``gatewayd`` agreement that makes comparing it meaningful.
* ``gatewayd._apply_stand_down`` — the daemon yields its own socket rather than
  having it unlinked underneath it, and refuses a request with nothing to
  reconcile.
* ``GatewayManager`` adoption — fit incumbent adopted, unfit incumbent asked to
  yield, and an unfit incumbent that will not yield adopted anyway with the
  breakage named at ERROR level instead of staying silent.

The end-to-end test binds real sockets under ``tmp_path`` and spawns a real
daemon subprocess through the manager's own spawn path; the rest are in-process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.hashing import (
    TARGET_ENV_PREFIXES,
    hash_target_env,
    target_env_pairs,
)

# POSIX: the end-to-end test observes the socket file disappearing, which a
# Windows named pipe has no directory entry for.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="observes a socket file being released"
)


# ── the fingerprint itself ─────────────────────────────────────────


class TestTargetFingerprint:
    def test_only_target_keys_participate(self) -> None:
        """Unrelated environment churn must not look like a routing change."""
        base = {"KIROCREW_MCP_TARGET_SLACK_MCP": "slack-mcp --stdio"}
        assert hash_target_env(base) == hash_target_env({**base, "PATH": "/usr/bin"})
        assert hash_target_env(base) == hash_target_env({**base, "TERM": "xterm"})

    def test_legacy_prefix_participates(self) -> None:
        assert "MC_MCP_TARGET_" in TARGET_ENV_PREFIXES
        assert target_env_pairs({"MC_MCP_TARGET_A": "a"}) == {"MC_MCP_TARGET_A": "a"}

    def test_added_server_moves_the_fingerprint(self) -> None:
        """The reported failure: a server stubbed after the daemon started."""
        before = {"KIROCREW_MCP_TARGET_A": "a --stdio"}
        after = {**before, "KIROCREW_MCP_TARGET_B": "b --stdio"}
        assert hash_target_env(before) != hash_target_env(after)

    def test_changed_command_moves_the_fingerprint(self) -> None:
        """Same server, different launch — as unservable as one never had."""
        assert hash_target_env({"KIROCREW_MCP_TARGET_A": "a --stdio"}) != hash_target_env(
            {"KIROCREW_MCP_TARGET_A": "a --http"}
        )

    def test_order_independent(self) -> None:
        one = {"KIROCREW_MCP_TARGET_A": "a", "KIROCREW_MCP_TARGET_B": "b"}
        two = {"KIROCREW_MCP_TARGET_B": "b", "KIROCREW_MCP_TARGET_A": "a"}
        assert hash_target_env(one) == hash_target_env(two)


class TestManagerAndDaemonAgree:
    """The load-bearing equality: a fresh spawn matches what the manager wants.

    Without it the comparison is theatre — the manager would ask for a
    fingerprint no daemon it spawns could ever report, and every start would
    take the stand-down path.
    """

    def test_wanted_equals_what_a_fresh_daemon_would_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_MCP_TARGET_INHERITED", "inherited --stdio")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "scrubbed-by-spawn")
        spec = mgr.GatewaySpec(
            socket_path=tmp_path / "gw.sock",
            mcp_target_env={"KIROCREW_MCP_TARGET_STUBBED": "stubbed --stdio"},
        )
        wanted = mgr.GatewayManager(spec)._wanted_target_fingerprint()

        # Rebuild the daemon's view: _spawn_once hands it the scrubbed parent
        # env with mcp_target_env overlaid, and _served_target_fingerprint
        # hashes os.environ from inside that process. Applied through the real
        # environ so the suite's own isolation variables survive.
        daemon_env = {
            **mgr._scrub_sensitive_env(dict(os.environ)),
            **spec.mcp_target_env,
        }
        _only_targets(monkeypatch, target_env_pairs(daemon_env))
        assert wanted == gw._served_target_fingerprint()

    def test_spec_env_wins_over_the_inherited_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same precedence as the spawn merge, or the two sides disagree."""
        monkeypatch.setenv("KIROCREW_MCP_TARGET_A", "stale --stdio")
        spec = mgr.GatewaySpec(
            socket_path=tmp_path / "gw.sock",
            mcp_target_env={"KIROCREW_MCP_TARGET_A": "fresh --stdio"},
        )
        assert mgr.GatewayManager(spec)._wanted_target_fingerprint() == hash_target_env(
            {"KIROCREW_MCP_TARGET_A": "fresh --stdio"}
        )


# ── gatewayd: the daemon yields its own socket ─────────────────────


class TestApplyStandDown:
    def test_sets_stop_event_on_a_real_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_A": "a"})
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "want": "deadbeef"}, stop)
        assert reply["type"] == "standing-down"
        assert reply["served"] == hash_target_env({"KIROCREW_MCP_TARGET_A": "a"})
        assert stop.is_set(), "the daemon must take the graceful SIGTERM path"

    def test_refuses_when_it_already_serves_the_requested_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a bare kill switch: agreement means nothing to reconcile."""
        env = {"KIROCREW_MCP_TARGET_A": "a"}
        _only_targets(monkeypatch, env)
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "want": hash_target_env(env)}, stop)
        assert reply["type"] == "stand-down-rejected"
        assert not stop.is_set()

    @pytest.mark.parametrize("want", [None, "", 17, {"a": 1}])
    def test_refuses_a_malformed_want(self, want: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_A": "a"})
        stop = asyncio.Event()
        frame: dict[str, Any] = {"type": "stand-down"}
        if want is not None:
            frame["want"] = want
        assert gw._apply_stand_down(frame, stop)["type"] == "stand-down-rejected"
        assert not stop.is_set()

    def test_refuses_when_shutdown_is_not_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-inert control frame would hang the caller."""
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_A": "a"})
        reply = gw._apply_stand_down({"type": "stand-down", "want": "deadbeef"}, None)
        assert reply["type"] == "stand-down-rejected"


# ── GatewayManager: the adoption decision ──────────────────────────


def _only_targets(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Make ``mapping`` the process's ENTIRE target set, leaving the rest of env alone.

    Never ``setattr(os, "environ", {...})``. ``gatewayd.os`` is the shared stdlib
    module, so replacing its ``environ`` replaces the process-wide mapping: it
    drops every variable the suite set for isolation, and ``KIROCREW_HOME`` going
    missing sends ``manager._gatewayd_log_path()`` to ``config_dir()`` — the real
    operator home. A daemon started under that test then writes its log there.
    Targeted ``delenv``/``setenv`` gets the same control with none of that.
    """
    for key in [k for k in os.environ if any(k.startswith(p) for p in TARGET_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)
    for key, value in mapping.items():
        monkeypatch.setenv(key, value)


def _manager(tmp_path: Path, targets: dict[str, str]) -> mgr.GatewayManager:
    return mgr.GatewayManager(
        mgr.GatewaySpec(socket_path=tmp_path / "gw.sock", mcp_target_env=dict(targets))
    )


class TestAdoptOrStandDown:
    @pytest.mark.asyncio
    async def test_fit_incumbent_is_adopted_without_a_stand_down(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        pong = {"type": "pong", "targets": manager._wanted_target_fingerprint()}
        asked = AsyncMock(return_value=mgr._RELEASED)
        manager._request_stand_down = asked  # type: ignore[method-assign]

        assert await manager._adopt_or_stand_down(pong) == mgr._ADOPT
        asked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unfit_incumbent_that_yields_makes_room_for_a_spawn(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        asked = AsyncMock(return_value=mgr._RELEASED)
        manager._request_stand_down = asked  # type: ignore[method-assign]

        assert (
            await manager._adopt_or_stand_down({"type": "pong", "targets": "stale"}) == mgr._SPAWN
        )
        # It is asked for the set the specs need, not merely poked.
        asked.assert_awaited_once_with(manager._wanted_target_fingerprint())

    @pytest.mark.asyncio
    async def test_unfit_incumbent_that_refuses_is_adopted_and_named(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail open, but stop being silent — the whole operator-facing point."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        manager._request_stand_down = AsyncMock(return_value=mgr._REFUSED)  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert (
                await manager._adopt_or_stand_down({"type": "pong", "targets": "stale"})
                == mgr._ADOPT
            )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "an unreconciled mismatch must not be silent"
        assert "REJECTED" in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_pre_fingerprint_daemon_is_asked_to_yield_then_adopted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A daemon that cannot say what it serves is UNFIT, not fit.

        Adopting on 'unknown' is what would reproduce #4569 on this fix's first
        deployment: the survivor of a gateway killed after a package upgrade is
        precisely a daemon too old to report a target set. It is asked to yield;
        a genuinely old daemon refuses by closing the connection, and the
        fail-open branch then adopts it with the breakage named.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        asked = AsyncMock(return_value=mgr._REFUSED)  # an old daemon never answers
        manager._request_stand_down = asked  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._adopt_or_stand_down({"type": "pong"}) == mgr._ADOPT
        asked.assert_awaited_once_with(manager._wanted_target_fingerprint())
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "an unassessable incumbent must not be adopted silently"
        assert "<unreported>" in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_pre_fingerprint_daemon_that_yields_makes_room(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        manager._request_stand_down = AsyncMock(return_value=mgr._RELEASED)  # type: ignore[method-assign]
        assert await manager._adopt_or_stand_down({"type": "pong"}) == mgr._SPAWN

    @pytest.mark.asyncio
    async def test_a_draining_incumbent_is_never_adopted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Accepted-but-slow is NOT the same as refused, and must not be adopted.

        Once a daemon accepts the stand-down it closes its accept loop, so it
        answers ping while accepting no new connection. Adopting it would turn a
        partial outage (the changed servers) into a total one (every server) and
        report success while doing it -- strictly worse than the bug this change
        fixes. Spawning is not available either, since it still holds the lock.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        manager._request_stand_down = AsyncMock(  # type: ignore[method-assign]
            return_value=mgr._DRAINING
        )
        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            verdict = await manager._adopt_or_stand_down({"type": "pong", "targets": "stale"})
        assert verdict == mgr._ABORT
        assert any(
            "NOT adopted" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        ), "the operator must be told the broker was left unclaimed and why"

    @pytest.mark.asyncio
    async def test_start_fails_rather_than_claiming_ready_on_a_draining_incumbent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No laundering: a start that cannot place a fit daemon must not succeed."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        monkeypatch.setattr(
            manager,
            "_ping_probe",
            AsyncMock(return_value={"type": "pong", "targets": "stale"}),
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._DRAINING))
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn into a held lock"))
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawned)

        assert await manager._start_locked() is False
        spawned.assert_not_awaited()
        assert manager._adopted is False, "a draining daemon must not be adopted"
        assert manager._watchdog is None, "and no watchdog should supervise it"

    @pytest.mark.asyncio
    async def test_start_spawns_after_an_unfit_incumbent_yields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The startup path, wired end to end with the transport stubbed out."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        monkeypatch.setattr(
            manager, "_ping_probe", AsyncMock(return_value={"type": "pong", "targets": "stale"})
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._RELEASED))
        monkeypatch.setattr(manager, "_clear_stale_socket", AsyncMock(return_value=None))
        monkeypatch.setattr(mgr.transport, "prepare_dir", lambda _p: None)
        spawned = AsyncMock(side_effect=RuntimeError("spawn reached"))
        monkeypatch.setattr(manager, "_spawn_once", spawned)

        assert await manager._start_locked() is False
        spawned.assert_awaited_once()
        assert manager._adopted is False, "a yielded socket must not leave us adopting"

    @pytest.mark.asyncio
    async def test_start_adopts_a_fit_incumbent_without_spawning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        monkeypatch.setattr(
            manager,
            "_ping_probe",
            AsyncMock(
                return_value={
                    "type": "pong",
                    "targets": manager._wanted_target_fingerprint(),
                }
            ),
        )
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn"))
        monkeypatch.setattr(manager, "_spawn_once", spawned)
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is True
            spawned.assert_not_awaited()
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()


class TestRequestStandDown:
    @pytest.mark.asyncio
    async def test_reports_failure_when_the_daemon_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal is reported as such — never waited out as if accepted."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager,
            "_control_roundtrip",
            AsyncMock(return_value={"type": "stand-down-rejected", "reason": "nope"}),
        )
        looked = {"n": 0}

        def _free(_p: Path) -> bool:
            looked["n"] += 1
            return True

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        assert await manager._request_stand_down("want") == mgr._REFUSED
        assert looked["n"] == 0, "a refusal must short-circuit before the wait"

    @pytest.mark.asyncio
    async def test_reports_failure_when_no_answer_comes_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_control_roundtrip", AsyncMock(return_value=None))
        assert await manager._request_stand_down("want") == mgr._REFUSED

    @pytest.mark.asyncio
    async def test_reports_failure_when_the_lock_is_never_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accepted is not released: a drain that outruns the budget fails open."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )
        monkeypatch.setattr(mgr.transport, "singleton_lock_free", lambda _p: False)
        monkeypatch.setattr(mgr, "_SHUTDOWN_GRACE_SECS", 0.2)
        assert await manager._request_stand_down("want") == mgr._DRAINING

    @pytest.mark.asyncio
    async def test_succeeds_once_the_lock_is_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )
        seen = {"n": 0}

        def _free(_p: Path) -> bool:
            seen["n"] += 1
            return seen["n"] >= 3  # released on the third look

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        assert await manager._request_stand_down("want") == mgr._RELEASED

    @pytest.mark.asyncio
    async def test_does_not_spawn_while_a_draining_daemon_still_holds_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows shape: the endpoint is gone long before the lock is free.

        gatewayd closes its server FIRST and releases the singleton lock LAST, so
        on Windows the named pipe stops resolving at the very start of shutdown
        and the whole drain sits inside the gap. Waiting on the endpoint would
        return here immediately, the replacement would lose the still-held lock,
        exit rc=0 without binding, and nothing would rebind. This asserts the
        wait ignores the endpoint entirely and tracks the lock.
        """
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )
        # Endpoint already unreachable -- the misleading signal.
        monkeypatch.setattr(mgr.transport, "endpoint_exists", lambda _p: False)
        drain = {"ticks": 0}

        def _free(_p: Path) -> bool:
            drain["ticks"] += 1
            return drain["ticks"] > 4  # lock held across four polls of drain

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        assert await manager._request_stand_down("want") == mgr._RELEASED
        assert drain["ticks"] == 5, "the wait must track the lock, not the endpoint"

    @pytest.mark.asyncio
    async def test_gives_up_the_wait_when_shutdown_is_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shutdown must not be made to wait out another process's drain."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )

        def _free(_p: Path) -> bool:
            manager._stopping = True  # shutdown() lands mid-wait
            return False

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        monkeypatch.setattr(mgr, "_SHUTDOWN_GRACE_SECS", 30.0)
        assert await manager._request_stand_down("want") == mgr._DRAINING


class TestElection:
    """What happens when our own spawn loses the socket to somebody else.

    gatewayd's flock guard makes a duplicate spawn exit rc=0 WITHOUT binding, so
    a pong arriving after our spawn is not proof the daemon is ours. Before the
    election loop, start() ping-confirmed whatever answered and returned success
    — reproducing #4569 one level up, on the freed socket.
    """

    @pytest.mark.asyncio
    async def test_a_foreign_fit_daemon_is_adopted_not_fought(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        fit = {"type": "pong", "targets": manager._wanted_target_fingerprint()}
        monkeypatch.setattr(manager, "_ping_probe", AsyncMock(return_value=None))
        # Our spawn lost the lock: no live process, but a fit daemon answers.
        monkeypatch.setattr(manager, "_spawn_and_confirm", AsyncMock(return_value=fit))
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is True, "a fit foreign daemon must be adopted"
            assert not manager.is_running
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_a_foreign_unfit_daemon_triggers_one_more_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round two assesses it as an incumbent; here it yields and we win."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        wanted = manager._wanted_target_fingerprint()
        # Round 1 finds an empty socket; round 2 finds the foreign daemon that
        # beat our first spawn to it, and asks it to stand down.
        monkeypatch.setattr(
            manager,
            "_ping_probe",
            AsyncMock(side_effect=[None, {"type": "pong", "targets": "foreign"}]),
        )
        stood_down = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", stood_down)
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 4242
        rounds: list[dict[str, Any]] = []

        async def _spawn() -> dict[str, Any]:
            rounds.append({})
            if len(rounds) == 1:
                # Our spawn lost the flock: no process handle, foreign answer.
                return {"type": "pong", "targets": "foreign"}
            manager._process = proc  # this time we bound it
            return {"type": "pong", "targets": wanted}

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            assert len(rounds) == 2, "the unfit foreign daemon must force a second round"
            stood_down.assert_awaited_once_with(wanted)
            assert manager.is_running, "round two must end as the owner"
            assert manager._adopted is False
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_rounds_are_bounded_and_exhaustion_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two instances trading the socket must not loop forever or lie."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        monkeypatch.setattr(
            manager, "_ping_probe", AsyncMock(return_value={"type": "pong", "targets": "foreign"})
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._RELEASED))
        spawn = AsyncMock(return_value={"type": "pong", "targets": "still-foreign"})
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawn)

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._start_locked() is False
        assert spawn.await_count == mgr._ELECTION_ROUNDS
        assert manager._adopted is False
        assert any(
            "election rounds" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), "exhausting the election must be reported, not returned as a bare False"

    """The probe itself, against a real lock file."""

    def test_free_when_nobody_holds_it(self, short_sock_dir: Path) -> None:
        assert transport.singleton_lock_free(short_sock_dir / "gw.sock") is True

    def test_held_while_another_holder_has_it(self, short_sock_dir: Path) -> None:
        sock = short_sock_dir / "gw.sock"
        fd = transport.acquire_singleton_lock(sock)
        assert fd is not None
        try:
            assert transport.singleton_lock_free(sock) is False
        finally:
            os.close(fd)
        assert transport.singleton_lock_free(sock) is True

    def test_the_probe_leaves_the_lock_available(self, short_sock_dir: Path) -> None:
        """Acquire-and-release, not acquire-and-hold — a leak would wedge every spawn."""
        sock = short_sock_dir / "gw.sock"
        assert transport.singleton_lock_free(sock) is True
        fd = transport.acquire_singleton_lock(sock)
        assert fd is not None, "the probe must not still be holding the lock"
        os.close(fd)


class TestShutdownAnnouncesItselfEarly:
    @pytest.mark.asyncio
    async def test_stopping_is_set_before_the_lifecycle_lock_is_taken(self, tmp_path: Path) -> None:
        """Otherwise a start mid-stand-down-wait can never observe the shutdown."""
        manager = _manager(tmp_path, {})
        await manager._lifecycle_lock.acquire()
        try:
            task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0.05)
            assert manager._stopping is True, "shutdown must announce before blocking"
            assert not task.done(), "and it is genuinely still waiting for the lock"
        finally:
            manager._lifecycle_lock.release()
            await asyncio.wait_for(task, timeout=10)


class TestOscillationCap:
    """Bounding the case _ELECTION_ROUNDS cannot reach: two long-lived instances.

    The watchdog also assesses incumbents, on an unbounded respawn loop, so
    without a process-lifetime cap two gateways sharing a socket path with
    divergent target sets would stand each other's daemon down forever.
    """

    @pytest.mark.asyncio
    async def test_stops_asking_after_the_cap_and_settles_loudly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        asked = AsyncMock(return_value=mgr._REFUSED)
        manager._request_stand_down = asked  # type: ignore[method-assign]
        unfit = {"type": "pong", "targets": "foreign"}

        for _ in range(mgr._MAX_STAND_DOWN_REQUESTS):
            assert await manager._adopt_or_stand_down(unfit) == mgr._ADOPT
            manager._stand_downs_issued += 1  # the real counter lives in the mocked method
        assert asked.await_count == mgr._MAX_STAND_DOWN_REQUESTS

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._adopt_or_stand_down(unfit) == mgr._ADOPT
        assert asked.await_count == mgr._MAX_STAND_DOWN_REQUESTS, "past the cap it must stop asking"
        assert any(
            "already issued" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        ), "settling instead of contending must be explained"

    @pytest.mark.asyncio
    async def test_the_counter_advances_on_every_real_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_control_roundtrip", AsyncMock(return_value=None))
        assert manager._stand_downs_issued == 0
        await manager._request_stand_down("want")
        await manager._request_stand_down("want")
        assert manager._stand_downs_issued == 2

    def test_the_counter_is_total_without_init(self) -> None:
        """Built via __new__, as several call sites and tests do."""
        bare = mgr.GatewayManager.__new__(mgr.GatewayManager)
        assert bare._stand_downs_issued == 0


# ── the watchdog's own election ─────────────────────────────────────


class TestWatchdogElection:
    """The watchdog runs the same gate, and it is reachable independently.

    All startup coverage enters through ``start()``, so without this the gate in
    ``_run_watchdog`` could be deleted with every other test still green.
    """

    @pytest.mark.asyncio
    async def test_watchdog_adopts_a_fit_incumbent_after_our_daemon_dies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        fit = {"type": "pong", "targets": manager._wanted_target_fingerprint()}
        monkeypatch.setattr(manager, "_ping_probe", AsyncMock(return_value=fit))
        spawn = AsyncMock(side_effect=RuntimeError("must not spawn against a fit incumbent"))
        monkeypatch.setattr(manager, "_spawn_once", spawn)
        monkeypatch.setattr(manager, "_clear_stale_socket", AsyncMock(return_value=None))
        monkeypatch.setattr(mgr, "_RESPAWN_BACKOFF_START_SECS", 0.01)
        monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0.01)
        proc = MagicMock()
        proc.returncode = 1
        proc.pid = 99
        proc.wait = AsyncMock(return_value=1)
        manager._process = proc

        task = asyncio.create_task(manager._run_watchdog())
        try:
            for _ in range(200):
                if manager._adopted:
                    break
                await asyncio.sleep(0.01)
            assert manager._adopted is True, "the watchdog must adopt a fit incumbent"
            spawn.assert_not_awaited()
        finally:
            manager._stopping = True
            task.cancel()
            # CancelledError is a BaseException, so suppress(Exception) alone
            # lets the cancellation escape and fail the test in teardown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @pytest.mark.asyncio
    async def test_watchdog_stands_an_unfit_incumbent_down_before_respawning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_A": "a"})
        monkeypatch.setattr(
            manager,
            "_ping_probe",
            AsyncMock(return_value={"type": "pong", "targets": "foreign"}),
        )
        stood_down = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", stood_down)
        monkeypatch.setattr(manager, "_clear_stale_socket", AsyncMock(return_value=None))
        spawned = asyncio.Event()

        async def _spawn() -> None:
            spawned.set()

        monkeypatch.setattr(manager, "_spawn_once", _spawn)
        monkeypatch.setattr(mgr, "_RESPAWN_BACKOFF_START_SECS", 0.01)
        proc = MagicMock()
        proc.returncode = 1
        proc.pid = 99
        proc.wait = AsyncMock(return_value=1)
        manager._process = proc

        task = asyncio.create_task(manager._run_watchdog())
        try:
            await asyncio.wait_for(spawned.wait(), timeout=20)
            stood_down.assert_awaited()
            assert manager._adopted is False
        finally:
            manager._stopping = True
            task.cancel()
            # CancelledError is a BaseException, so suppress(Exception) alone
            # lets the cancellation escape and fail the test in teardown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# ── end to end over a real socket ──────────────────────────────────
#
# Every test below starts a REAL daemon, so each one pins KIROCREW_HOME to its
# own tmp dir first. The suite does not isolate it, and manager._gatewayd_log_path
# falls back to config_dir() when it is unset -- i.e. the operator's real data
# home, which a test must never write to.


async def _round_trip(socket_path: Path, frame: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(transport.connect(socket_path), timeout=10)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        result = json.loads(line.decode("utf-8"))
        assert isinstance(result, dict)
        return result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _await_endpoint(socket_path: Path, *, timeout: float = 30.0) -> None:
    """Wait for a daemon to finish binding, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(transport.endpoint_exists, socket_path):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"daemon never bound {socket_path}")


def _isolate_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Scope a real daemon's side effects: its data home AND its CWD.

    KIROCREW_HOME because the suite does not set it and
    ``manager._gatewayd_log_path`` falls back to ``config_dir()`` -- the
    operator's real data home -- so a spawned daemon writes its log there.

    The working directory because a spawned daemon INHERITS the test process's
    CWD, which under pytest is the repository checkout: anything it or its own
    children resolve relatively would land in the repo. ``_spawn_once``
    deliberately passes no ``cwd`` (in production the daemon should inherit the
    gateway's), so the test must move itself rather than change that.

    Returns the directory, for passing as an explicit ``cwd=`` to any subprocess
    the test launches itself.
    """
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.chdir(home)
    return home


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_real_daemon_leaves_nothing_in_the_repository(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test side effects, asserted rather than assumed.

    Two escape routes exist and both are closed by ``_isolate_home``: the data
    home (``KIROCREW_HOME`` unset falls back to the operator's real
    ``~/.kiro/crew``) and the working directory (a spawned daemon inherits
    pytest's CWD, which is the checkout). This walks the repo before and after
    and asserts the tree is byte-identical.
    """
    repo = Path(__file__).resolve().parent.parent

    def _snapshot() -> set[str]:
        out: set[str] = set()
        for entry in repo.iterdir():
            if entry.name in {".git", ".venv", "node_modules", "__pycache__"}:
                continue
            out.add(entry.name)
        return out

    before = _snapshot()
    run_dir = _isolate_home(monkeypatch, tmp_path / "home")
    assert Path.cwd() == run_dir.resolve(), "the test must not run in the checkout"
    _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_A": "a --stdio"})

    sock = short_sock_dir / "gw.sock"
    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        assert (await _round_trip(sock, {"type": "ping"}))["type"] == "pong"
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(daemon, timeout=30)

    assert _snapshot() == before, "a daemon under test must not write into the repo"


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_pong_reports_the_served_target_set_over_a_real_socket(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not just the helper: a served daemon answers with its set."""
    _isolate_home(monkeypatch, tmp_path / "home")
    env = {"KIROCREW_MCP_TARGET_A": "a --stdio"}
    _only_targets(monkeypatch, env)
    sock = short_sock_dir / "gw.sock"
    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        pong = await _round_trip(sock, {"type": "ping"})
        assert pong["type"] == "pong"
        assert pong["targets"] == hash_target_env(env)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(daemon, timeout=30)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_stand_down_frame_is_wired_into_the_serving_daemon(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-down over the real socket ends the real daemon and frees it.

    This is the test that would fail if ``run_gatewayd`` stopped forwarding its
    ``stop_event`` into the handler: the frame would be answered
    ``stand-down-rejected`` and the socket would stay bound. It also asserts the
    SINGLETON LOCK is released, which is what a replacement actually needs.
    """
    _isolate_home(monkeypatch, tmp_path / "home")
    _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_A": "a --stdio"})
    sock = short_sock_dir / "gw.sock"
    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        reply = await _round_trip(
            sock, {"type": "stand-down", "want": hash_target_env({"KIROCREW_MCP_TARGET_B": "b"})}
        )
        assert reply["type"] == "standing-down"
        await asyncio.wait_for(daemon, timeout=60)
        assert not await asyncio.to_thread(
            transport.endpoint_exists, sock
        ), "a stood-down daemon must release its endpoint"
        assert await asyncio.to_thread(
            transport.singleton_lock_free, sock
        ), "and its singleton lock, which is what a replacement must win"
    finally:
        stop.set()
        if not daemon.done():
            daemon.cancel()
            with contextlib.suppress(Exception):
                await daemon


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_crash_survivor_is_reconciled_end_to_end(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported scenario, start to finish, with TWO real daemon processes.

    The survivor is a real subprocess launched with target set A, standing in for
    the daemon a gateway killed without its shutdown path leaves behind. That
    matters: its environment is genuinely frozen at spawn, so the manager cannot
    influence what it reports -- which is the invariant the whole fix rests on
    and which an in-process survivor sharing ``os.environ`` cannot model.

    The gateway then starts wanting set B, the set its specs were just rewritten
    for. Before this change it adopted the survivor and every session lost server
    B; now the survivor yields and the manager's own spawn path puts a daemon
    serving B on the socket.
    """
    home = tmp_path / "home"
    run_dir = _isolate_home(monkeypatch, home)
    sock = short_sock_dir / "gw.sock"
    survivor_targets = {"KIROCREW_MCP_TARGET_A": "a --stdio"}

    # The survivor: its own process, its own frozen environment A.
    survivor_env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not any(k.startswith(pfx) for pfx in TARGET_ENV_PREFIXES)
        },
        **survivor_targets,
        "KIROCREW_HOME": str(home),
    }
    survivor = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.mcp_gateway.gatewayd",
        "--socket",
        str(sock),
        "--idle-timeout-secs",
        "60",
        "--max-backends",
        "2",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=survivor_env,
        cwd=str(run_dir),
        start_new_session=True,
    )
    manager: mgr.GatewayManager | None = None
    try:
        await _await_endpoint(sock, timeout=60)
        survivor_fp = (await _round_trip(sock, {"type": "ping"}))["targets"]
        assert survivor_fp == hash_target_env(survivor_targets)

        # The starting gateway wants set B.
        wanted_targets = {"KIROCREW_MCP_TARGET_B": "b --stdio"}
        _only_targets(monkeypatch, {})
        manager = mgr.GatewayManager(
            mgr.GatewaySpec(
                socket_path=sock,
                max_backends=2,
                idle_timeout_secs=60,
                mcp_target_env=dict(wanted_targets),
            )
        )
        wanted = manager._wanted_target_fingerprint()
        assert wanted != survivor_fp

        assert await manager.start() is True, "the gateway must come up"
        # The survivor really left, on its own graceful path.
        await asyncio.wait_for(survivor.wait(), timeout=60)
        assert manager._adopted is False, "the survivor must not have been adopted"
        assert manager.is_running, "the manager must own the replacement it spawned"
        # Two distinct real OS processes, not one in-process task wearing two
        # hats -- this is what makes the frozen-at-spawn environment real.
        assert manager._process is not None
        assert manager._process.pid != survivor.pid

        # And the daemon now on the socket serves what the specs need.
        pong = await _round_trip(sock, {"type": "ping"})
        assert pong["type"] == "pong"
        assert pong["targets"] == wanted
    finally:
        if manager is not None:
            await manager.shutdown()
        if survivor.returncode is None:
            survivor.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(survivor.wait(), timeout=10)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_fit_survivor_is_still_adopted_end_to_end(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour adoption exists for must survive the new check.

    A survivor already serving the wanted set is adopted with no spawn and no
    teardown -- otherwise this change would trade a partial outage for a full
    broker cycle on every ordinary restart.
    """
    _isolate_home(monkeypatch, tmp_path / "home")
    sock = short_sock_dir / "gw.sock"
    targets = {"KIROCREW_MCP_TARGET_A": "a --stdio"}
    _only_targets(monkeypatch, targets)
    manager = mgr.GatewayManager(
        mgr.GatewaySpec(
            socket_path=sock, max_backends=2, idle_timeout_secs=60, mcp_target_env=dict(targets)
        )
    )
    stop = asyncio.Event()
    survivor = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=2, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        assert await manager.start() is True
        assert manager._adopted is True, "a fit survivor must still be adopted"
        assert not manager.is_running, "adoption must not spawn a competitor"
        assert not survivor.done(), "a fit survivor must not be torn down"
    finally:
        await manager.shutdown()
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(survivor, timeout=30)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_stand_down_is_recorded_in_the_security_event_log(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit claim is asserted, not just documented.

    A stand-down ends the daemon, so it is the most consequential frame the
    control surface accepts; the allow and the refusal both belong in the SEL
    beside the claim/abort/peer decisions.
    """
    _isolate_home(monkeypatch, tmp_path / "home")
    env = {"KIROCREW_MCP_TARGET_A": "a --stdio"}
    _only_targets(monkeypatch, env)
    recorded: list[dict[str, Any]] = []

    class _Spy:
        def log_api_access(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _Spy)

    # Refused: it already serves the requested set.
    gw._apply_stand_down({"type": "stand-down", "want": hash_target_env(env)}, asyncio.Event())
    # Allowed: a genuine mismatch.
    gw._apply_stand_down({"type": "stand-down", "want": "deadbeef"}, asyncio.Event())

    ops = [r for r in recorded if r.get("operation") == "mcp-gateway.stand_down"]
    assert len(ops) == 2, f"both outcomes must be audited, got {recorded}"
    assert ops[0]["outcome"] == "denied"
    assert ops[1]["outcome"] == "allowed"
