"""The stub's handshake budget: where it applies, and that a bad override cannot kill it.

``handshake`` covers connect + Register + the gateway's reply -- local IPC with
gatewayd, nothing more. The target server's own startup happens later and inside
the daemon, so no slow backend can spend this budget; the only condition that
reaches it is a gateway listening but slow to answer under host contention. Every
other refusal shape raises inside ``handshake`` and returns without waiting,
which is what makes a generous default free on the paths users actually hit --
asserted below so that reasoning stays true rather than remaining a comment.

Expiry costs pooling rather than correctness (the stub degrades to a per-session
``execvpe``), and it is silent apart from one ``stub_fallback.jsonl`` line. That
combination is why a regression here would not surface as a bug report: sessions
would simply stop sharing backends, adding load to the host whose load caused the
stall.

Named ``test_mcp_gateway_stub_*`` deliberately -- that is the prefix the macOS
job's glob selects, matching the sibling stub suites. Under another name this
coverage would silently be Linux-and-Windows-only.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from kiro_crew.mcp_gateway import stub, transport


def _register_payload() -> dict[str, Any]:
    """The minimum Register frame ``handshake`` needs; it only reads ``stub_uuid``."""
    return {"type": "register", "stub_uuid": "handshake-timeout-probe"}


async def _serve(handler: Any, sock_dir: Any) -> tuple[Any, Any]:
    sock = sock_dir / "gw.sock"
    transport.prepare_dir(sock)
    server = await transport.serve(sock, handler, limit=1 << 16)
    return server, sock


def test_default_budget_absorbs_a_contended_host(monkeypatch) -> None:
    """Unset override yields a budget sized for a stalled gateway, not a fast one.

    The floor is the point of the test. A budget in the low single digits is
    within reach of ordinary local-IPC scheduling delay on a loaded host, and
    every expiry silently drops a session out of the pool.
    """
    monkeypatch.delenv(stub._HANDSHAKE_TIMEOUT_ENV, raising=False)
    assert stub._handshake_timeout_secs() >= 10.0


def test_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv(stub._HANDSHAKE_TIMEOUT_ENV, "42.5")
    assert stub._handshake_timeout_secs() == 42.5


@pytest.mark.parametrize(
    "raw",
    [
        "soon",          # not a number
        "",              # set but empty
        "0",             # wait_for(0) would defeat pooling outright
        "-1",            # negative
        "nan",           # every comparison false
        "inf",           # not a usable deadline
        "300",           # at the exclusive bound
        "1e9",           # typo that would hang startup for years
    ],
)
def test_unusable_override_yields_the_default(monkeypatch, raw: str) -> None:
    """A bad override must degrade to the default, never raise.

    An exception here would escape ``_amain``'s ``wait_for`` call *before*
    ``fallback_exec`` runs, breaking the always-degrade-to-per-session guarantee
    -- the same trap the logging-level guard exists for. So the property under
    test is "returns the default", not merely "does not crash".
    """
    monkeypatch.delenv(stub._HANDSHAKE_TIMEOUT_ENV, raising=False)
    monkeypatch.setenv(stub._HANDSHAKE_TIMEOUT_ENV, raw)
    assert stub._handshake_timeout_secs() == stub._HANDSHAKE_TIMEOUT_SECS


@pytest.mark.asyncio
async def test_refusals_do_not_consume_the_budget(short_sock_dir) -> None:
    """An absent gateway returns immediately, so a large budget is free.

    This is the load-bearing half of sizing the default generously: if the
    common refusal shapes waited out the timeout, raising it would slow every
    session that starts while the gateway is down.
    """
    missing = short_sock_dir / "absent.sock"
    started = asyncio.get_running_loop().time()
    with pytest.raises(stub.FallbackRequestedError):
        await stub.handshake(str(missing), _register_payload())
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0, f"connect refusal waited {elapsed:.2f}s instead of failing fast"


@pytest.mark.asyncio
async def test_amain_degrades_using_the_resolved_budget(
    short_sock_dir, monkeypatch, tmp_path
) -> None:
    """The real call site consults the resolver, and expiry reaches fallback.

    Drives ``_amain`` rather than ``handshake`` directly: the budget is applied
    by the ``wait_for`` in ``_amain``, so testing ``handshake`` alone would leave
    the wiring unpinned. The endpoint accepts and then says nothing, which is the
    one shape that can actually reach the timeout.
    """
    accepted = asyncio.Event()
    held: list[Any] = []

    def on_connect(_reader: Any, writer: Any) -> None:
        # Accept, read nothing, reply never. Keep a reference so the transport is
        # not closed by garbage collection, which would turn this into an EOF
        # (a different, already-covered fallback branch).
        accepted.set()
        held.append(writer)

    server, sock = await _serve(on_connect, short_sock_dir)

    # A tiny override keeps the test fast AND proves the resolver governs the
    # wait: at the default this would sit here for ten seconds.
    monkeypatch.setenv(stub._HANDSHAKE_TIMEOUT_ENV, "0.2")

    reasons: list[str] = []
    monkeypatch.setattr(
        stub, "log_fallback", lambda reason, *a, **k: reasons.append(reason)
    )
    execs: list[Any] = []
    # execvpe never returns in production; a recording no-op lets _amain finish.
    monkeypatch.setattr(stub, "fallback_exec", lambda args: execs.append(args))

    argv = [
        "--server", "probe",
        "--agent", "kirocrew",
        "--target-command", sys.executable,
        "--work-dir", str(tmp_path),
        "--socket", str(sock),
    ]
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        rc = await asyncio.wait_for(stub._amain(argv), timeout=30)
    finally:
        elapsed = loop.time() - started
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()

    assert accepted.is_set(), "the endpoint never accepted, so nothing was tested"
    assert reasons == ["handshake_timeout"], reasons
    assert execs, "timeout did not reach fallback_exec; the session would have died"
    assert rc == 1
    # Bounds the wiring, not the clock: a call site that read the module constant
    # instead of calling the resolver would still land on handshake_timeout here,
    # just at the default budget. Only the elapsed time distinguishes the two.
    #
    # Derived from the constant rather than an absolute number because the
    # measured window is all of _amain -- including build_register_payload's
    # /proc walk and target-binary hashing -- and an absolute ceiling on an
    # instrumented run is a known flake class (coverage runs on one Python
    # version only). Half the default still fails an unwired call site, which
    # waits the full budget.
    assert elapsed < stub._HANDSHAKE_TIMEOUT_SECS / 2, (
        f"waited {elapsed:.2f}s for a 0.2s budget — the override was not applied"
    )
