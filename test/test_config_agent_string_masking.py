"""Masking of agent-writable free-text strings in the config API response.

Agent ``description`` and ``triggers`` are agent- and package-writable and are
NOT schema-sensitive, so the schema-driven walk in ``_masked_config_dict`` used
to let them through. PR #8472 masks the same class of strings on the roster
endpoint (GET /api/agents); this closes the config endpoint
(GET /api/config/kirocrew) so the ``backend-security-controls`` redaction rule
holds across the whole surface. As with all masking, it must apply ONLY to the
browser-facing view and NEVER to ``to_dict()``/``save()``.
"""

from __future__ import annotations

import json

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict


def _cfg_with_agent(**agent_kwargs) -> tuple[KiroCrewConfig, str]:
    cfg = KiroCrewConfig()
    name = "researcher"
    cfg.agents[name] = KiroCrewAgentConfig(**agent_kwargs)
    return cfg, name


def test_description_and_triggers_masked_in_view():
    """Both non-empty free-text fields are masked and their raw values vanish."""
    cfg, name = _cfg_with_agent(
        description="SECRET-DESCRIPTION-should-not-leak",
        triggers="SECRET-TRIGGERS-should-not-leak",
    )

    masked = _masked_config_dict(cfg)
    agent = masked["agents"][name]
    assert agent["description"] == _SENSITIVE_MASK
    assert agent["triggers"] == _SENSITIVE_MASK

    dumped = json.dumps(masked)
    assert "SECRET-DESCRIPTION-should-not-leak" not in dumped
    assert "SECRET-TRIGGERS-should-not-leak" not in dumped


def test_mask_does_not_leak_into_write_path():
    """to_dict()/save() must still carry the real values after masking."""
    cfg, name = _cfg_with_agent(
        description="real description",
        triggers="real triggers",
    )

    # Build the masked view (which must not mutate the underlying cfg).
    _masked_config_dict(cfg)

    persisted = cfg.to_dict()["agents"][name]
    assert persisted["description"] == "real description"
    assert persisted["triggers"] == "real triggers"


def test_empty_strings_left_as_empty():
    """Empty description/triggers stay empty, not replaced by the sentinel."""
    cfg, name = _cfg_with_agent(description="", triggers="")

    masked = _masked_config_dict(cfg)
    agent = masked["agents"][name]
    assert agent["description"] == ""
    assert agent["triggers"] == ""


def test_other_agent_fields_unchanged():
    """Non-sensitive structural fields survive the masked view verbatim."""
    cfg, name = _cfg_with_agent(
        description="hide me",
        triggers="hide me too",
        model="claude-sonnet",
        workspace="research-ws",
        source="some-package",
    )

    agent = _masked_config_dict(cfg)["agents"][name]
    assert agent["model"] == "claude-sonnet"
    assert agent["workspace"] == "research-ws"
    assert agent["source"] == "some-package"
