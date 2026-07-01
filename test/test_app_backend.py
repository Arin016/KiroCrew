"""Tests for kiro_claw.apps.backend — backend process management."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from kiro_claw.apps.backend import (
    AppProcess,
    _find_free_port,
    get_app_process,
    list_app_processes,
    start_app_backend,
    stop_app_backend,
)
from kiro_claw.apps.manager import APP_MANIFEST_FILENAME, install_app


def _sandbox_can_spawn() -> bool:
    """True if the OS sandbox can launch a surviving child on this host.

    start_app_backend() fail-closes to None when the sandbox launcher can't
    start — e.g. GitHub hosted runners allow unshare(NEWUSER) but deny the
    launcher's separate unshare(NEWNS) (errno 1). sandbox._probe_unshare() gives
    a false positive there (it does NEWUSER|NEWNS in a SINGLE unshare call), so
    gate the real-spawn tests on the production path itself: wrap a trivial
    command exactly as the backend does and confirm it exits 0. Reusing
    wrap_argv() means this probe can never drift from start_app_backend().
    """
    try:
        from kiro_claw import sandbox as _sb

        argv, cleanup = _sb.wrap_argv([sys.executable, "-c", "pass"], mode="standard")
    except Exception:  # noqa: BLE001 — any probe failure => treat as "can't spawn"
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


# Evaluated once per worker at collection; the two lifecycle tests below need a
# real sandboxed backend to come up and stay up.
_needs_sandbox_spawn = pytest.mark.skipif(
    not _sandbox_can_spawn(),
    reason="OS sandbox cannot spawn a surviving child here (e.g. GitHub hosted "
    "runners deny unshare(NEWNS)); start_app_backend() correctly fail-closes to None",
)


def _make_app_with_backend(tmp_path, name="backend-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Backend App",
        "description": "App with a backend",
        "author": "tester",
        "backend": {
            "entryPoint": "backend/server.py",
            "port": "auto",
            "healthCheck": "/health",
        },
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    # Create a minimal backend that starts an HTTP server
    (src / "backend").mkdir()
    (src / "backend" / "server.py").write_text(
        'import http.server, os, sys\n'
        'port = int(os.environ.get("PORT", 9100))\n'
        'class H(http.server.BaseHTTPRequestHandler):\n'
        '    def do_GET(self):\n'
        '        self.send_response(200)\n'
        '        self.end_headers()\n'
        '        self.wfile.write(b"ok")\n'
        '    def log_message(self, *a): pass\n'
        'http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()\n'
    )
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch, worker_id):
    home = tmp_path / "kiroclaw-home"
    home.mkdir()
    monkeypatch.setenv("KIROCLAW_HOME", str(home))
    import kiro_claw.apps.backend as bmod

    # Under xdist (-n auto) each worker runs in its OWN process with its own
    # _allocated_ports dict, so two workers both auto-allocate 9100 and the real
    # servers collide (EADDRINUSE). Give each worker a DISJOINT port window so
    # parallel real-spawn tests never contend. (Production is single-process; this
    # only matters for the test harness.)
    if worker_id and worker_id != "master":
        try:
            idx = int(worker_id.replace("gw", "")) if worker_id.startswith("gw") else 0
        except ValueError:
            idx = 0
        base = 9100 + idx * 20
        monkeypatch.setattr(bmod, "_MIN_PORT", base)
        monkeypatch.setattr(bmod, "_MAX_PORT", base + 20)

    def _reap() -> None:
        # KILL any spawned backend processes, not just clear the tracking dicts — a
        # test that spawns a real server and doesn't stop it would otherwise leave the
        # process holding its port, so the next test's auto-allocated port collides
        # (EADDRINUSE). Before the spawn survival-check this leak was silently tolerated
        # (the colliding spawn was reported as 'started' anyway); now it's caught, so the
        # fixture must clean up properly. Use stop_app_backend → it killpg's the whole
        # process group (the sandbox wraps the child, so a plain terminate misses it).
        import socket as _sock
        ports = [getattr(ap, "port", 0) for ap in bmod._processes.values()]
        for name in list(bmod._processes.keys()):
            try:
                bmod.stop_app_backend(name)
            except Exception:  # noqa: BLE001
                pass
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        # Wait for each killed server's port to actually be released so the next test's
        # auto-allocation can't re-pick a still-occupied port (EADDRINUSE).
        for port in ports:
            if not port:
                continue
            for _ in range(50):  # up to ~5s
                s = _sock.socket()
                try:
                    s.bind(("127.0.0.1", port))
                    s.close()
                    break
                except OSError:
                    s.close()
                    time.sleep(0.1)

    _reap()       # clean slate before the test
    yield home
    _reap()       # and reap anything the test left running


class TestPortAllocation:
    def test_find_free_port(self):
        port = _find_free_port()
        assert 9100 <= port <= 9200


class TestAppProcess:
    def test_to_dict(self):
        ap = AppProcess(app_name="test", port=9100, pid=123, healthy=True)
        d = ap.to_dict()
        assert d["app_name"] == "test"
        assert d["port"] == 9100
        assert d["healthy"] is True


class TestBackendLifecycle:
    def test_no_backend_returns_none(self, tmp_path, app_env):
        # App without backend section
        src = tmp_path / "source" / "no-backend"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "no-backend", "version": "1.0.0",
            "displayName": "No Backend", "description": "No backend",
        }))
        install_app(src)
        result = start_app_backend("no-backend")
        assert result is None

    @_needs_sandbox_spawn
    def test_start_and_stop(self, tmp_path, app_env):
        src = _make_app_with_backend(tmp_path)
        install_app(src)
        ap = start_app_backend("backend-app")
        assert ap is not None
        assert ap.port > 0
        assert ap.pid > 0
        # Process should be in the list
        procs = list_app_processes()
        assert len(procs) == 1
        assert procs[0]["app_name"] == "backend-app"
        # Stop it
        stopped = stop_app_backend("backend-app")
        assert stopped is True
        assert list_app_processes() == []

    def test_stop_not_running(self, app_env):
        assert stop_app_backend("nonexistent") is False

    @_needs_sandbox_spawn
    def test_get_process(self, tmp_path, app_env):
        src = _make_app_with_backend(tmp_path)
        install_app(src)
        start_app_backend("backend-app")
        ap = get_app_process("backend-app")
        assert ap is not None
        assert ap.app_name == "backend-app"
        stop_app_backend("backend-app")

    def test_get_process_not_running(self, app_env):
        assert get_app_process("nonexistent") is None

    def test_missing_entry_point(self, tmp_path, app_env):
        src = tmp_path / "source" / "bad-entry"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "bad-entry", "version": "1.0.0",
            "displayName": "Bad Entry", "description": "Missing entry",
            "backend": {"entryPoint": "nonexistent.py"},
        }))
        install_app(src)
        result = start_app_backend("bad-entry")
        assert result is None

    def test_immediate_exit_is_not_reported_as_started(self, tmp_path, app_env, monkeypatch):
        # A backend that dies right away (e.g. EADDRINUSE port collision) must NOT be
        # reported as started — otherwise the gateway proxies to a dead port (502) and
        # respawns onto the same doomed port forever (the crash-loop we hit). The spawn
        # verifies the child survived its bind; an immediate exit → None + cleared state.
        import kiro_claw.apps.backend as bmod

        # Widen the survival-check grace window for this test only. The boom.py child
        # exits immediately ONCE it runs, but under heavy pytest-xdist parallelism
        # (-n auto, ~32 workers) the sandboxed interpreter can take well over the default
        # 1.6s window just to start, so proc.poll() still reports it alive across the
        # whole default window and the dying process gets mis-reported as 'started'
        # (flaky failure on loaded build hosts). The poll loop breaks as soon as the
        # child exits, so a longer ceiling only costs wall-time when the host is starved.
        monkeypatch.setattr(bmod, "_SPAWN_SURVIVAL_CHECKS", 100)  # up to ~20s ceiling
        src = tmp_path / "source" / "die-app"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "die-app", "version": "1.0.0",
            "displayName": "Die", "description": "exits immediately",
            "backend": {"entryPoint": "boom.py", "port": "auto", "healthCheck": "/health"},
        }))
        # boom.py: a backend that dies the instant it runs. The stderr line mimics
        # the real EADDRINUSE crash this test guards against, but is cosmetic here —
        # the test asserts on the None return, not the log contents. It is a
        # deliberate fake, not a real bind; fixture stderr like this is the kind of
        # thing that can mislead a static analyzer into flagging a phantom port.
        (src / "boom.py").write_text(
            'import sys\n'
            'sys.stderr.write("OSError: [Errno 98] address already in use\\n")\n'
            'sys.exit(1)\n'
        )
        install_app(src)
        result = start_app_backend("die-app")
        assert result is None
        # the STARTING placeholder was cleared — a later retry isn't wedged
        assert "die-app" not in bmod._processes

    def test_concurrent_starts_single_flight_one_spawn(self, tmp_path, app_env, monkeypatch):
        # Two concurrent start_app_backend calls for the same app must not both spawn
        # onto the same auto-allocated port (the TOCTOU that crash-looped the loser).
        # The STARTING placeholder single-flights them: exactly one spawn body runs,
        # both callers converge on the SAME resolved process. We mock the spawn body so
        # the test exercises the COORDINATION (placeholder + await) without two real
        # sandboxed os.fork()s racing (a fork-in-threads deadlock unrelated to this fix).
        import threading

        import kiro_claw.apps.backend as bmod

        src = _make_app_with_backend(tmp_path)
        install_app(src)

        spawn_calls = {"n": 0}
        gate = threading.Event()

        def _fake_body(app_name, manifest):
            spawn_calls["n"] += 1
            gate.wait(timeout=5)  # hold the placeholder in-flight while the 2nd call arrives
            ap = AppProcess(app_name=app_name, port=9137, pid=4242, healthy=True,
                            started_at=0.0)
            with bmod._lock:
                bmod._processes[app_name] = ap
                bmod._allocated_ports[app_name] = 9137
            return ap

        monkeypatch.setattr(bmod, "_start_app_backend_body", _fake_body)

        results: list = []
        barrier = threading.Barrier(2)

        def _go():
            barrier.wait()
            results.append(start_app_backend("backend-app"))

        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.3)   # let one claim the placeholder + the other hit the await
        gate.set()        # release the single spawn body
        for t in threads:
            t.join(timeout=10)

        # exactly ONE spawn body ran (single-flighted), both callers got the same proc
        assert spawn_calls["n"] == 1, f"spawn body ran {spawn_calls['n']} times (race not single-flighted)"
        non_none = [r for r in results if r is not None]
        assert len(non_none) == 2, f"a caller got None: {results}"
        assert {r.port for r in non_none} == {9137}
        assert len(list_app_processes()) == 1
        # cleanup the fake-process state so it can't leak into the next test
        with bmod._lock:
            bmod._processes.clear()
            bmod._allocated_ports.clear()

    def test_await_inflight_spawn_timeout_clears_stale_placeholder(self, app_env):
        # If a spawn body hangs without raising (so the owner's None/exception cleanup
        # never fires), an awaiting caller hits the deadline with the placeholder still
        # STARTING. It must clear that placeholder and return None — otherwise the app is
        # wedged in 'starting' forever and every later call re-enters the 20s wait.
        import kiro_claw.apps.backend as bmod

        with bmod._lock:
            bmod._processes["wedged-app"] = AppProcess(
                app_name="wedged-app", starting=True, started_at=0.0
            )
        # Short timeout so the test is fast; the placeholder never resolves.
        result = bmod._await_inflight_spawn("wedged-app", timeout=0.3)
        assert result is None
        # The stale placeholder is gone, so a fresh start_app_backend can spawn again.
        assert "wedged-app" not in bmod._processes
