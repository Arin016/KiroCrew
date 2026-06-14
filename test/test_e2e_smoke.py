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
    sep = "&" if "?" in path else "?"
    url = f"http://localhost:{gateway.port}{path}{sep}token={gateway.token}"
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


# --- API round-trip tests ---


def _api_delete(gateway, path: str) -> dict:
    """DELETE an API endpoint with token auth, return parsed JSON."""
    url = f"http://localhost:{gateway.port}{path}?token={gateway.token}"
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post_with_session(gateway, path: str, body: dict, session_key: str = "dashboard:ui") -> dict:
    """POST with token auth + X-Session-Key header."""
    url = f"http://localhost:{gateway.port}{path}?token={gateway.token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Session-Key", session_key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_cron_create_and_delete(gateway):
    """Can create a cron job via API and then delete it."""
    body = {"name": "e2e-test-cron", "every": 3600, "message": "hello from e2e"}
    data = _api_post(gateway, "/api/crons", body)
    assert "id" in data
    job_id = data["id"]
    assert job_id

    try:
        # Verify it appears in the list
        crons = _api_get(gateway, "/api/crons")
        assert any(j["id"] == job_id for j in crons["jobs"])
    finally:
        _api_delete(gateway, f"/api/crons/{job_id}")


def test_lessons_create_and_list(gateway):
    """Can create a lesson and see it in the list."""
    body = {"rule": "e2e test lesson", "category": "knowledge"}
    _api_post_with_session(gateway, "/api/lessons", body)

    data = _api_get(gateway, "/api/lessons")
    assert "lessons" in data
    assert any("e2e test lesson" in str(lesson) for lesson in data["lessons"])


def test_chat_slot_lifecycle(gateway):
    """Create a slot, get its detail, then delete it."""
    # Create
    create_data = _api_post(gateway, "/api/chat/slots", {})
    slot_key = create_data["key"]
    assert slot_key

    try:
        # Detail
        detail = _api_get(gateway, f"/api/chat/slots/{slot_key}")
        assert detail["key"] == slot_key
        assert "messages" in detail
    finally:
        _api_delete(gateway, f"/api/chat/slots/{slot_key}")


def test_session_detail(gateway):
    """Can fetch detail for the fixture-seeded session (returns message list).

    The minimal fixture ships ``dashboard_starter.jsonl`` -- assert against
    that known session rather than depending on ambient state.
    """
    sessions = _api_get(gateway, "/api/sessions")
    assert sessions["total"] >= 1, "minimal fixture must seed at least one session"
    # Use the fixture-seeded session (known precondition)
    first_key = sessions["sessions"][0]["key"]

    detail = _api_get(gateway, f"/api/sessions/{first_key}")
    assert isinstance(detail, list)


def test_session_search(gateway):
    """Session search endpoint returns results without errors."""
    data = _api_get(gateway, "/api/sessions/search?q=test")
    assert "sessions" in data
