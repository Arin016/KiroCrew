"""E2E smoke tests using ``spawn_feature_gateway`` harness.

Phase 5 of the KiroClaw Testing & Release Plan. These tests spawn a real
gateway subprocess, hit its HTTP endpoints with the token from the READY
line, and verify core functionality works end-to-end.

Gated behind ``KIROCLAW_E2E=1`` because they spawn a real gateway process
(5-15s startup). CI runs them via ToD shared fleet; local devs opt in.

Requires: ``kiro_claw.testing.harness`` (CR-275699038, merged) and composable
CLI flags (CR-274772612, merged).
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

# Gate all tests behind KIROCLAW_E2E so they don't slow down local pytest runs.
pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCLAW_E2E"),
    reason="E2E smoke tests. Set KIROCLAW_E2E=1 to run.",
)


@pytest.fixture(scope="module")
def gateway():
    """Spawn one gateway for all smoke tests (amortize 5-15s startup).

    No state cleanup between tests; each test must be tolerant of
    prior-test side effects, or use a fresh per-test gateway.
    """
    from kiro_claw.testing.harness import spawn_feature_gateway

    with spawn_feature_gateway(fixture="minimal", approval="reads") as handle:
        yield handle


def _api_get(gateway, path: str) -> dict:
    """GET an API endpoint with token auth, return parsed JSON."""
    url = f"http://localhost:{gateway.port}{path}?token={gateway.token}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post(gateway, path: str, body: dict) -> dict:
    """POST to an API endpoint with token auth, return parsed JSON."""
    url = f"http://localhost:{gateway.port}{path}?token={gateway.token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# --- Smoke tests ---


def test_health_check(gateway):
    """Gateway responds to /api/status with uptime and version."""
    data = _api_get(gateway, "/api/status")
    assert "uptime" in data
    assert "version" in data


def test_sessions_list(gateway):
    """Gateway lists sessions (minimal fixture ships one starter session)."""
    data = _api_get(gateway, "/api/sessions")
    assert "sessions" in data
    # minimal fixture ships dashboard_starter.jsonl
    assert data["total"] >= 1


def test_create_chat_slot(gateway):
    """Can create a new chat slot via the API."""
    data = _api_post(gateway, "/api/chat/slots", {})
    assert "key" in data
    slot_key = data["key"]
    assert slot_key  # non-empty string


def test_cron_list_fixture_crons(gateway):
    """Cron list returns the fixture's crons (minimal has 2: active + paused)."""
    data = _api_get(gateway, "/api/crons")
    assert "jobs" in data
    # minimal fixture ships 2 crons
    assert len(data["jobs"]) >= 2


def test_memory_workspace_exists(gateway):
    """Memory workspace directory exists in the seeded home."""
    workspace_dir = gateway.home / "workspace" / "memory"
    assert workspace_dir.is_dir()
    assert (workspace_dir / "preferences.md").is_file()
