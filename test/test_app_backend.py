"""Tests for kiro_claw.apps.backend — backend process management."""
from __future__ import annotations

import json

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
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kiroclaw-home"
    home.mkdir()
    monkeypatch.setenv("KIROCLAW_HOME", str(home))
    # Clear process tracking between tests
    import kiro_claw.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


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
