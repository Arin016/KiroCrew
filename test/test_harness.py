"""Tests for ``test/harness.py`` — gateway-spawning context manager.

Most tests exercise the harness's internal helpers in isolation with a
stand-in for ``subprocess.Popen``. Spawning a real gateway takes 5–15s
and pulls in the full MCP probe / config init / dashboard bind path, so
the end-to-end test is gated behind ``KIROCLAW_HARNESS_INTEGRATION``.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from harness import (
    DEFAULT_READY_TIMEOUT,
    READY_PREFIX,
    GatewayHandle,
    GatewaySpawnError,
    _drain_stderr,
    _resolve_workspace_src,
    _terminate_process_group,
    _wait_for_ready_line,
    spawn_feature_gateway,
)


class FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for ``_wait_for_ready_line``.

    Exposes just the surface the harness touches: ``poll``, ``stdout``,
    ``returncode``, ``pid``. ``stdout`` is backed by an OS pipe (not a
    BytesIO) so ``selectors.register`` can poll it the same way it polls
    a real subprocess. Pre-populate via ``stdout_lines``; the bytes are
    written to the pipe at construction and the write end is closed so
    ``select`` reports EOF after the buffered bytes are consumed.
    """

    def __init__(self, stdout_lines: list[bytes]) -> None:
        read_fd, write_fd = os.pipe()
        if stdout_lines:
            os.write(write_fd, b"".join(stdout_lines))
        os.close(write_fd)  # EOF on read side once buffered bytes are drained
        # Default buffering wraps the underlying FileIO in a BufferedReader,
        # which exposes ``read1()`` — same shape ``subprocess.Popen.stdout``
        # has when ``stdout=PIPE``.
        self.stdout = os.fdopen(read_fd, "rb")
        self.stderr: Optional[io.BytesIO] = None
        self.returncode: Optional[int] = None
        self.pid = 99999  # unused — _wait_for_ready_line never signals

    def poll(self) -> Optional[int]:
        return self.returncode


# ── _wait_for_ready_line ────────────────────────────────────────────────


def test_ready_line_parses_valid_payload() -> None:
    payload = '{"port": 52093, "token": "abc", "pid": 1234, "home": "/tmp/x"}'
    fake = FakePopen([f"{READY_PREFIX}{payload}\n".encode()])

    result = _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=[])  # type: ignore[arg-type]

    assert result["port"] == 52093
    assert result["token"] == "abc"
    assert result["pid"] == 1234


def test_ready_line_skips_lines_before_ready() -> None:
    """Pre-READY chatter (e.g. ``Created default config``) must not break parsing."""
    fake = FakePopen([
        b"Some startup log\n",
        b"Another line\n",
        f'{READY_PREFIX}{{"port": 1, "token": "t", "pid": 1, "home": "/h"}}\n'.encode(),
    ])

    result = _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=[])  # type: ignore[arg-type]

    assert result["port"] == 1


def test_ready_line_raises_on_early_exit_with_stderr() -> None:
    """Subprocess exits before READY → exception includes stderr tail."""
    # Prime the proc as already-exited so the harness's poll() check fires
    # on the next iteration after consuming whatever output we provided.
    fake = FakePopen([b"some output\n"])
    fake.returncode = 2  # simulate already-exited subprocess
    stderr = [b"FATAL: missing config\n", b"Traceback (most recent call last):\n"]

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=5.0, stderr_buffer=stderr)  # type: ignore[arg-type]

    assert "exited with code 2" in str(exc.value)
    assert "FATAL: missing config" in str(exc.value)


def test_ready_line_raises_on_timeout_with_stderr() -> None:
    """No READY line within timeout → exception includes stderr tail.

    Pins the contract under test: even with stdout fully drained (EOF at
    once) but proc.poll() reporting alive forever, the deadline must
    still fire because the selector poll is bounded.
    """
    fake = FakePopen([b"line1\n", b"line2\n"])  # never emits READY
    # FakePopen's pipe writer was already closed in __init__ — the read
    # side will see EOF, but proc.poll() returns None (alive). The
    # selector keeps reporting EOF readiness; the harness keeps looping
    # until the deadline fires.
    stderr = [b"WARNING something\n"]

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=0.3, stderr_buffer=stderr)  # type: ignore[arg-type]

    assert "did not emit" in str(exc.value)
    assert "WARNING something" in str(exc.value)
    assert "KIROCLAW_HARNESS_READY_TIMEOUT" in str(exc.value)


def test_ready_line_timeout_fires_when_subprocess_silent_but_alive() -> None:
    """Deadline must fire even when stdout never produces data.

    Pins the contract introduced by the selectors-based loop: a subprocess
    that's alive but never writes to stdout must hit the timeout, not
    block forever in a stdout read.
    """
    # Empty pipe + proc.poll() returning None forever simulates a hung
    # subprocess that's silent on stdout. Without the selector-based
    # bounded poll, this would hang the harness indefinitely.
    fake = FakePopen([])  # no output
    fake.returncode = None  # explicitly alive

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=0.4, stderr_buffer=[])  # type: ignore[arg-type]

    assert "did not emit" in str(exc.value)


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not-json",                          # invalid JSON
        "[1, 2, 3]",                         # valid JSON but not a dict
        '"just-a-string"',                   # valid JSON, scalar
        '{"foo": 1}',                        # dict, missing both required keys
        '{"port": 1}',                       # dict, missing token
        '{"token": "t", "pid": 1}',          # dict, missing port
    ],
)
def test_ready_line_raises_on_malformed_payload(bad_payload: str) -> None:
    fake = FakePopen([f"{READY_PREFIX}{bad_payload}\n".encode()])

    with pytest.raises(GatewaySpawnError) as exc:
        _wait_for_ready_line(fake, timeout=1.0, stderr_buffer=[])  # type: ignore[arg-type]

    msg = str(exc.value)
    assert "malformed" in msg or "expected dict" in msg or "missing required key" in msg


# ── _terminate_process_group ────────────────────────────────────────────


def test_terminate_handles_already_exited_proc() -> None:
    """Already-exited proc → no-op, no exception."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait(timeout=5)
    assert proc.poll() is not None  # already exited

    # Should not raise even though the process is gone.
    _terminate_process_group(proc)


def test_terminate_falls_back_to_sigkill() -> None:
    """Process that ignores SIGTERM gets SIGKILL after the grace period.

    The child Python process registers ``SIG_IGN`` for SIGTERM, then
    prints ``READY`` to stdout. The parent reads that line before
    triggering the terminate — guaranteeing the handler is in place
    before SIGTERM lands, regardless of how slow the build host is at
    starting Python. Without that handshake the test races on cold
    sandboxes where Python startup exceeds an arbitrary sleep, lets
    SIGTERM reach the default disposition, and the child exits -15
    instead of being killed -9.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('READY', flush=True);"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Block until the child confirms the SIG_IGN handler is registered.
        assert proc.stdout is not None
        ready_line = proc.stdout.readline()
        assert ready_line == b"READY\n", f"child did not signal ready: {ready_line!r}"

        with patch("harness.TERMINATE_GRACE_SECONDS", 0.5):
            _terminate_process_group(proc)
        assert proc.poll() is not None
        # On POSIX, SIGKILL is signal 9; returncode is -9 when killed.
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


# ── _resolve_workspace_src / GatewayHandle / _drain_stderr ──────────────


def test_resolve_workspace_src_finds_package() -> None:
    """When run from inside the package, returns ``<pkg>/src``."""
    src = _resolve_workspace_src()
    assert (src / "kiro_claw" / "__init__.py").exists()


def test_gateway_handle_is_frozen() -> None:
    handle = GatewayHandle(
        url="http://localhost:1/?token=t",
        port=1,
        token="t",
        home=Path("/tmp/x"),
        proc=None,  # type: ignore[arg-type]
    )
    with pytest.raises(FrozenInstanceError):
        handle.port = 2  # type: ignore[misc]


def test_drain_stderr_accumulates() -> None:
    """``_drain_stderr`` reads from a stream into the buffer until EOF."""
    stream = io.BytesIO(b"first chunk\nsecond chunk\n")
    # ``_drain_stderr`` calls ``stream.read1(4096)``; ``BytesIO`` lacks
    # read1, so wire it manually for the test.
    stream.read1 = lambda n: stream.read(n)  # type: ignore[attr-defined,assignment]

    buffer: list[bytes] = []
    _drain_stderr(stream, buffer)

    assert b"".join(buffer) == b"first chunk\nsecond chunk\n"


# ── spawn_feature_gateway: orchestration with mocked Popen ──────────────


def _make_fake_proc_with_ready(payload: str) -> FakePopen:
    """Build a FakePopen that emits a READY line and has a working stderr."""
    proc = FakePopen([f"{READY_PREFIX}{payload}\n".encode()])
    proc.stderr = io.BytesIO(b"")
    proc.stderr.read1 = lambda n: proc.stderr.read(n)  # type: ignore[attr-defined,assignment]
    return proc


def test_spawn_feature_gateway_happy_path() -> None:
    """End-to-end with mocked subprocess: spawn → READY → teardown.

    Doesn't spin a real gateway (covered by the gated integration test);
    instead patches ``subprocess.Popen`` so the orchestration logic in
    ``spawn_feature_gateway`` runs end-to-end with controlled output.
    """
    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )

    terminated: dict[str, bool] = {"called": False}

    def fake_terminate(_proc: object) -> None:
        terminated["called"] = True

    captured_cmd: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kw: object) -> FakePopen:
        captured_cmd.append(cmd)
        return fake_proc

    with patch("harness.subprocess.Popen", side_effect=fake_popen), patch(
        "harness._terminate_process_group", side_effect=fake_terminate
    ):
        with spawn_feature_gateway(fixture="empty") as handle:
            assert handle.port == 51234
            assert handle.token == "t-abc"
            assert handle.url == "http://localhost:51234/?token=t-abc"
            assert handle.home.exists()  # tmp dir created by harness
            captured_home = handle.home

    assert terminated["called"] is True
    # Tmp home is cleaned up on context exit.
    assert not captured_home.exists()

    # Spawn invokes ``kiroclaw gateway --test-mode --seed <fixture>`` —
    # seeding is atomic with gateway startup (no separate seed pass).
    assert captured_cmd, "Popen was not called"
    cmd_str = " ".join(captured_cmd[0])
    assert "gateway" in cmd_str
    assert "--test-mode" in cmd_str
    assert "--seed empty" in cmd_str
    assert "--approval reads" in cmd_str
    # ``crons`` defaults to False so the safe ``--no-crons`` flag is
    # included — a stray cron firing during an unrelated test is the
    # exact flake the default guards against.
    assert "--no-crons" in captured_cmd[0]


def test_spawn_feature_gateway_crons_opt_in() -> None:
    """``crons=True`` drops ``--no-crons`` so the scheduler thread runs.

    Pins the API contract for cron-exercising tests (Mesh-1479 follow-up):
    setting ``crons=True`` MUST allow the gateway to start its scheduler;
    the harness has no other lever for tests that need ``cron_add`` /
    ``cron_remove`` to actually fire.
    """
    fake_proc = _make_fake_proc_with_ready(
        '{"port": 51234, "token": "t-abc", "pid": 9876, "home": "/tmp/x"}'
    )
    captured_cmd: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kw: object) -> FakePopen:
        captured_cmd.append(cmd)
        return fake_proc

    with patch("harness.subprocess.Popen", side_effect=fake_popen), patch(
        "harness._terminate_process_group"
    ):
        with spawn_feature_gateway(fixture="empty", crons=True):
            pass

    assert captured_cmd, "Popen was not called"
    assert "--no-crons" not in captured_cmd[0]
    # Sanity: rest of the test-mode shape is unchanged
    assert "--test-mode" in captured_cmd[0]
    assert "--seed" in captured_cmd[0]


def test_parallel_spawns_get_distinct_homes_and_ports() -> None:
    """Two concurrent spawns must each get their own tmp home + port.

    Pins the PRD acceptance "parallel invocations don't share
    KIROCLAW_HOME or port". Mocks Popen so each call returns a fake
    process emitting a different READY payload; the harness must
    propagate the distinct ports and create separate tmp dirs.
    """
    procs = iter(
        [
            _make_fake_proc_with_ready(
                '{"port": 50001, "token": "t-A", "pid": 1, "home": "/ignored"}'
            ),
            _make_fake_proc_with_ready(
                '{"port": 50002, "token": "t-B", "pid": 2, "home": "/ignored"}'
            ),
        ]
    )

    with patch(
        "harness.subprocess.Popen", side_effect=lambda *a, **kw: next(procs)
    ), patch("harness._terminate_process_group"):
        with spawn_feature_gateway(fixture="empty") as outer:
            with spawn_feature_gateway(fixture="empty") as inner:
                # Distinct ports propagated from the (different) READY lines.
                assert outer.port != inner.port
                # Each invocation gets its own tmp home directory.
                assert outer.home != inner.home
                assert outer.home.exists()
                assert inner.home.exists()


def test_default_timeout_constant() -> None:
    """Sanity check: default timeout is the documented 60s."""
    assert DEFAULT_READY_TIMEOUT == 60.0


# ── End-to-end (gated) ──────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("KIROCLAW_HARNESS_INTEGRATION"),
    reason=(
        "Real-gateway integration test. Set KIROCLAW_HARNESS_INTEGRATION=1 to run. "
        "Requires the composable-gateway CLI flags (--test-mode + --seed) to be "
        "present on the local feature branch."
    ),
)
def test_spawn_real_gateway_round_trip() -> None:
    """End-to-end: spawn, hit URL, exit, assert process is gone."""
    import urllib.error
    import urllib.request

    with spawn_feature_gateway(fixture="empty") as handle:
        assert handle.port > 0
        assert handle.token
        assert handle.home.exists()
        # Hit the dashboard root. ``urllib.request.urlopen`` follows
        # redirects automatically and raises ``HTTPError`` for any 4xx/5xx,
        # so the success path is just a 200 here. Auth-related rejections
        # surface as ``HTTPError`` and are also acceptable — what we're
        # really testing is that the gateway is reachable, not the
        # specific response policy of an unauthenticated request.
        req = urllib.request.Request(handle.url)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 403)
        proc = handle.proc

    # Outside the with-block: process is gone, home is removed.
    assert proc.poll() is not None
    assert not handle.home.exists()
