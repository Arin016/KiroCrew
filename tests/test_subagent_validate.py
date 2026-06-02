"""Tests for _validate_agent fallback chain in subagent.py.

We mock heavy dependencies at sys.modules level so subagent.py can be
imported without the full kiro_claw runtime.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeAgent:
    name: str


# Stub out heavy transitive imports before importing subagent
_STUBS = [
    "kiro_claw.context",
    "kiro_claw.hooks",
    "kiro_claw.providers",
    "kiro_claw.providers.base",
    "kiro_claw.sel",
    "kiro_claw.session",
    "kiro_claw.slack",
    "kiro_claw.slack.format",
    "kiro_claw.stats",
]


@pytest.fixture(autouse=True)
def _stub_modules():
    """Inject stub modules so subagent.py can be imported."""
    originals = {}
    for mod_name in _STUBS:
        originals[mod_name] = sys.modules.get(mod_name)
        stub = types.ModuleType(mod_name)
        # providers.base needs specific names
        if mod_name == "kiro_claw.providers.base":
            stub.EVENT_COMPLETE = "complete"
            stub.EVENT_PERMISSION_REQUEST = "permission"
            stub.EVENT_TEXT_CHUNK = "text"
            stub.LLMEvent = type("LLMEvent", (), {})
        if mod_name == "kiro_claw.hooks":
            stub.TOOL_AUTO_APPROVE = "auto"
            stub.TOOL_DENY = "deny"
        if mod_name == "kiro_claw.slack.format":
            stub.extract_options = lambda x: []
        if mod_name == "kiro_claw.stats":
            stub.Stats = MagicMock
        if mod_name == "kiro_claw.sel":
            stub.sel = MagicMock()
        if mod_name == "kiro_claw.context":
            stub.ContextBuilder = MagicMock
        if mod_name == "kiro_claw.session":
            stub.SessionManager = MagicMock
        sys.modules[mod_name] = stub

    # Clear cached subagent module so it reimports with stubs
    sys.modules.pop("kiro_claw.subagent", None)

    yield

    # Restore
    for mod_name in _STUBS:
        if originals[mod_name] is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = originals[mod_name]
    sys.modules.pop("kiro_claw.subagent", None)


def test_found_returns_requested():
    from kiro_claw.subagent import _validate_agent

    with patch(
        "kiro_claw.aim_agents.list_agents",
        return_value=[_FakeAgent("code-reviewer"), _FakeAgent("kiroclaw")],
    ):
        name, err = _validate_agent("code-reviewer")
        assert name == "code-reviewer"
        assert err == ""


def test_not_found_falls_back_to_kiroclaw():
    from kiro_claw.subagent import _validate_agent

    with patch(
        "kiro_claw.aim_agents.list_agents",
        return_value=[_FakeAgent("kiroclaw")],
    ):
        name, err = _validate_agent("nonexistent")
        assert name == ""
        assert err == ""


def test_unknown_agent_falls_back_silently():
    from kiro_claw.subagent import _validate_agent

    with patch(
        "kiro_claw.aim_agents.list_agents",
        return_value=[_FakeAgent("kiroclaw")],
    ):
        name, err = _validate_agent("nonexistent")
        assert name == ""
        assert err == ""


def test_empty_input_returns_empty():
    from kiro_claw.subagent import _validate_agent

    name, err = _validate_agent("")
    assert name == ""
    assert err == ""
