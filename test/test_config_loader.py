"""Property-based tests for config/loader.py.

Tests the KiroClawConfig loader validation logic using hypothesis
for property-based testing.
"""

from __future__ import annotations

import json
import logging
import platform
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from kiro_claw.config.loader import (
    _HAS_JSONSCHEMA,
    AgentConfig,
    DashboardConfig,
    MemoryConfig,
    MemoryStoreConfig,
    KiroClawAgentConfig,
    KiroClawConfig,
    ResolvedBindings,
    SecretaryConfig,
    SessionConfig,
    SlackConfig,
    SttConfig,
    WorkspaceConfig,
    _migrate_workspaces,
    config_dir,
    resolve_agent_bindings,
    resolve_memory_store_config,
    validate_kiro_agent_references,
    workspace_dir_for,
)

# Logger used by the loader module — needed for capturing warnings in tests
logger = logging.getLogger("kiro_claw.config.loader")

# ---------------------------------------------------------------------------
# Helpers / Strategies
# ---------------------------------------------------------------------------

# Fields with enum constraints and their allowed values
_ENUM_FIELDS: list[tuple[str, str, list[str]]] = [
    ("agent", "approval_mode", ["auto", "interactive"]),
    ("agent", "provider", ["acp", "bedrock"]),
    ("agent", "sandbox", ["auto", "off"]),
    ("agent", "log_level", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("memory", "embedding_provider", ["none", "ollama"]),
]

# Top-level keys recognised by the schema
_KNOWN_TOP_KEYS = {
    "agent",
    "session",
    "memory",
    "slack",
    "dashboard",
    "hooks",
    "agents",
    "default_agent",
    "workspaces",
    "default_workspace",
    "memory_stores",
    "default_memory_store",
    "auto_update",
}

# Skip marker for tests that require jsonschema validation
_requires_jsonschema = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="jsonschema not available — validation tests require it",
)


def _load_from_dict(data: object) -> KiroClawConfig:
    """Write *data* to a temp config file and load via KiroClawConfig.load()."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroClawConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _load_from_raw_string(content: str) -> KiroClawConfig:
    """Write raw string content to a temp file and load."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(content)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroClawConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _load_from_dict_with_logs(data: object) -> tuple[KiroClawConfig, list[str]]:
    """Load config and capture warning log messages."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_path",
            return_value=tmp,
        ):
            logger = logging.getLogger("kiro_claw.config.loader")
            messages: list[str] = []
            original_warning = logger.warning

            def capture_warning(msg: object, *args: object) -> None:
                try:
                    messages.append(str(msg) % args)
                except Exception:
                    messages.append(str(msg))
                original_warning(msg, *args)

            with unittest.mock.patch.object(logger, "warning", capture_warning):
                result = KiroClawConfig.load()
            return result, messages
    finally:
        tmp.unlink(missing_ok=True)


def _default_config() -> KiroClawConfig:
    """Return a default KiroClawConfig for comparison."""
    return KiroClawConfig()


# Hypothesis strategy for safe identifier strings (no control chars, JSON-safe)
_safe_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=15,
)

# Strategy for KiroClawAgentConfig instances
_kiroclaw_agent_config_st = st.builds(
    KiroClawAgentConfig,
    kiro_agent=st.text(min_size=0, max_size=20),
    workspace=_safe_name_st,
    memory_store=_safe_name_st,
)

# Strategy for WorkspaceConfig instances
_workspace_config_st = st.builds(
    WorkspaceConfig,
    dir=st.text(min_size=1, max_size=30),
)

# Strategy for MemoryStoreConfig instances
_memory_store_config_st = st.builds(
    MemoryStoreConfig,
    description=st.text(min_size=0, max_size=30),
    embedding_provider=st.sampled_from(["", "none", "ollama"]),
)

# Hypothesis strategy for generating valid KiroClawConfig instances
_agent_config_st = st.builds(
    AgentConfig,
    approval_mode=st.sampled_from(["auto", "interactive"]),
    streaming=st.booleans(),
    model=st.text(min_size=0, max_size=20),
    provider=st.sampled_from(["acp", "bedrock"]),
    bedrock_model_id=st.text(min_size=1, max_size=40),
    bedrock_region=st.sampled_from(["us-west-2", "us-east-1", "eu-west-1"]),
    default_agent=st.text(min_size=0, max_size=20),
    sandbox=st.sampled_from(["auto", "off"]),
    soft_stop_budget_secs=st.floats(min_value=0.5, max_value=60.0),
)

_session_config_st = st.builds(
    SessionConfig,
    timeout_secs=st.integers(min_value=60, max_value=7200),
)

_memory_config_st = st.builds(
    MemoryConfig,
    embedding_provider=st.sampled_from(["none", "ollama"]),
    embedding_url=st.just("http://localhost:11434"),
    allow_remote_embedding=st.booleans(),
    embedding_dim=st.sampled_from([256, 512, 1024]),
    embedding_timeout_secs=st.floats(min_value=1.0, max_value=30.0),
    semantic_confidence_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_dedup_threshold=st.floats(min_value=0.0, max_value=1.0),
    episodic_max_results=st.integers(min_value=1, max_value=50),
    episodic_max_count=st.integers(min_value=100, max_value=50000),
    semantic_keys=st.just([]),
    history_idle_hours=st.floats(min_value=0.5, max_value=24.0),
    history_max_days=st.integers(min_value=1, max_value=365),
    migrated=st.booleans(),
)

_slack_config_st = st.builds(
    SlackConfig,
    allowed_users=st.just([]),
    tracking_channels=st.just([]),
    open_channels=st.lists(st.from_regex(r"C[A-Z0-9]{8,10}", fullmatch=True), max_size=5),
    command=st.text(min_size=1, max_size=20),
    reactions_enabled=st.booleans(),
)

_dashboard_config_st = st.builds(
    DashboardConfig,
    url=st.text(min_size=0, max_size=50),
)

_secretary_config_st = st.builds(
    SecretaryConfig,
    enabled=st.booleans(),
    user_id=st.text(min_size=0, max_size=20),
    watched_channels=st.lists(st.text(min_size=1, max_size=15), max_size=3),
    poll_interval_seconds=st.integers(min_value=30, max_value=600),
    style_rules=st.lists(st.text(min_size=1, max_size=30), max_size=3),
    alert_keywords=st.lists(st.text(min_size=1, max_size=20), max_size=3),
    alert_on_name_mention=st.booleans(),
    test_mode=st.booleans(),
)

_kiroclaw_config_st = st.builds(
    KiroClawConfig,
    agent=_agent_config_st,
    session=_session_config_st,
    memory=_memory_config_st,
    slack=_slack_config_st,
    dashboard=_dashboard_config_st,
    secretary=_secretary_config_st,
    hooks=st.just({}),
    agents=st.dictionaries(
        keys=_safe_name_st,
        values=_kiroclaw_agent_config_st,
        min_size=0,
        max_size=3,
    ),
    default_agent=st.one_of(st.just(""), _safe_name_st),
    workspaces=st.dictionaries(
        keys=_safe_name_st,
        values=_workspace_config_st,
        min_size=0,
        max_size=3,
    ),
    default_workspace=st.text(min_size=1, max_size=20),
    memory_stores=st.dictionaries(
        keys=_safe_name_st,
        values=_memory_store_config_st,
        min_size=0,
        max_size=3,
    ),
    default_memory_store=st.one_of(st.just("default"), _safe_name_st),
    auto_update=st.booleans(),
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestConfigLoaderProperties:
    """Property-based tests for the config loader validation logic."""

    # Feature: config-schema, Property 6: KiroClawConfig load/to_dict round-trip
    @given(config=_kiroclaw_config_st)
    @settings(deadline=None)
    def test_load_to_dict_round_trip(
        self,
        config: KiroClawConfig,
    ) -> None:
        """Calling to_dict() then load() from that dict must yield an
        equivalent KiroClawConfig instance.

        **Validates: Requirements 2.4, 2.5, 9.4, 9.6**
        """
        d = config.to_dict()
        loaded = _load_from_dict(d)

        # Compare agent fields
        assert loaded.agent.approval_mode == config.agent.approval_mode
        assert loaded.agent.streaming == config.agent.streaming
        assert loaded.agent.model == config.agent.model
        assert loaded.agent.provider == config.agent.provider
        assert loaded.agent.bedrock_model_id == config.agent.bedrock_model_id
        assert loaded.agent.bedrock_region == config.agent.bedrock_region
        assert loaded.agent.default_agent == config.agent.default_agent
        assert loaded.agent.sandbox == config.agent.sandbox

        # Compare session
        assert loaded.session.timeout_secs == config.session.timeout_secs

        # Compare memory fields
        assert loaded.memory.embedding_provider == config.memory.embedding_provider
        assert loaded.memory.embedding_dim == config.memory.embedding_dim
        assert loaded.memory.allow_remote_embedding == config.memory.allow_remote_embedding
        assert loaded.memory.migrated == config.memory.migrated
        assert loaded.memory.episodic_max_results == config.memory.episodic_max_results
        assert loaded.memory.episodic_max_count == config.memory.episodic_max_count
        assert loaded.memory.history_max_days == config.memory.history_max_days

        # Compare slack
        assert loaded.slack.command == config.slack.command
        assert loaded.slack.allowed_users == config.slack.allowed_users
        assert loaded.slack.tracking_channels == config.slack.tracking_channels
        assert loaded.slack.open_channels == config.slack.open_channels
        assert loaded.slack.reactions_enabled == config.slack.reactions_enabled

        # Compare dashboard
        assert loaded.dashboard.url == config.dashboard.url
        assert loaded.dashboard.widget_density == config.dashboard.widget_density

        # Compare secretary
        assert loaded.secretary.enabled == config.secretary.enabled
        assert loaded.secretary.user_id == config.secretary.user_id
        assert loaded.secretary.watched_channels == config.secretary.watched_channels
        assert loaded.secretary.poll_interval_seconds == config.secretary.poll_interval_seconds
        assert loaded.secretary.style_rules == config.secretary.style_rules
        assert loaded.secretary.alert_keywords == config.secretary.alert_keywords
        assert loaded.secretary.alert_on_name_mention == config.secretary.alert_on_name_mention
        assert loaded.secretary.test_mode == config.secretary.test_mode

        # Compare top-level fields
        assert loaded.hooks == config.hooks
        assert loaded.default_workspace == config.default_workspace
        assert loaded.auto_update == config.auto_update

        # Compare workspaces (migration produces WorkspaceConfig objects)
        if config.workspaces:
            for ws_name, ws_cfg in config.workspaces.items():
                assert ws_name in loaded.workspaces
                assert loaded.workspaces[ws_name].dir == ws_cfg.dir
        else:
            # Empty workspaces → default entry synthesized
            assert "default" in loaded.workspaces
            assert loaded.workspaces["default"].dir == "workspace"

    # Feature: config-schema, Property 9: Type mismatch falls back to default
    @_requires_jsonschema
    @given(
        field_idx=st.integers(min_value=0, max_value=4),
        wrong_idx=st.integers(min_value=0, max_value=3),
    )
    @settings(deadline=None)
    def test_type_mismatch_falls_back_to_default(
        self,
        field_idx: int,
        wrong_idx: int,
    ) -> None:
        """When a config value has an incorrect type, load() must fall
        back to the field's default value.

        **Validates: Requirements 6.1, 6.2**
        """
        fields = [
            ("agent", "approval_mode", "string"),
            ("agent", "streaming", "boolean"),
            ("session", "timeout_secs", "integer"),
            ("memory", "embedding_dim", "integer"),
            ("memory", "allow_remote_embedding", "boolean"),
        ]
        wrong_values = [
            42,  # wrong for string/boolean
            "not_a_num",  # wrong for integer/boolean
            True,  # wrong for string/integer
            [1, 2, 3],  # wrong for all scalar types
        ]

        section, key, expected_type = fields[field_idx]
        wrong_value = wrong_values[wrong_idx]

        # Skip cases where the wrong_value accidentally has the right type
        type_map = {"string": str, "boolean": bool, "integer": int}
        expected_py = type_map[expected_type]
        if expected_type == "integer":
            assume(not isinstance(wrong_value, int) or isinstance(wrong_value, bool))
        elif expected_type == "boolean":
            assume(not isinstance(wrong_value, bool))
        else:
            assume(not isinstance(wrong_value, expected_py))

        data: dict = {section: {key: wrong_value}}
        loaded = _load_from_dict(data)
        defaults = _default_config()

        loaded_section = getattr(loaded, section)
        default_section = getattr(defaults, section)
        assert getattr(loaded_section, key) == getattr(
            default_section, key
        ), f"Expected default for {section}.{key} after type mismatch"

    # Feature: config-schema, Property 10: Enum violation falls back to default
    @_requires_jsonschema
    @given(
        field_idx=st.integers(min_value=0, max_value=3),
        bad_value=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_enum_violation_falls_back_to_default(
        self,
        field_idx: int,
        bad_value: str,
    ) -> None:
        """When a config key has an enum constraint and the value is not
        in the allowed set, load() must fall back to the field's default.

        **Validates: Requirements 6.3**
        """
        section, key, allowed = _ENUM_FIELDS[field_idx]
        assume(bad_value not in allowed)

        data: dict = {section: {key: bad_value}}
        loaded = _load_from_dict(data)
        defaults = _default_config()

        loaded_section = getattr(loaded, section)
        default_section = getattr(defaults, section)
        assert getattr(loaded_section, key) == getattr(default_section, key), (
            f"Expected default for {section}.{key} after enum violation "
            f"(value={bad_value!r}, allowed={allowed})"
        )

    # Feature: config-schema, Property 11: Unrecognized keys are detected
    @_requires_jsonschema
    @given(
        extra_keys=st.lists(
            st.text(
                alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
                min_size=2,
                max_size=15,
            ).filter(lambda k: k not in _KNOWN_TOP_KEYS),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_unrecognized_keys_detected(
        self,
        extra_keys: list[str],
    ) -> None:
        """When config.json contains unrecognized top-level keys,
        load() must detect and warn about them.

        **Validates: Requirements 6.4**
        """
        data: dict = {k: "some_value" for k in extra_keys}
        _, messages = _load_from_dict_with_logs(data)

        unrecognized_msgs = [m for m in messages if "unrecognized top-level keys" in m]
        assert len(unrecognized_msgs) > 0, (
            f"Expected warning about unrecognized keys {extra_keys}, " f"got messages: {messages}"
        )

        warning_text = unrecognized_msgs[0]
        for k in extra_keys:
            assert k in warning_text, f"Key '{k}' not mentioned in warning: {warning_text}"

    # Feature: config-schema, Property 12: load() always returns valid KiroClawConfig
    @given(
        content=st.one_of(
            st.text(min_size=0, max_size=200),
            st.just(""),
            st.just("null"),
            st.just("[]"),
            st.just("42"),
            st.just("{"),
            st.just('{"agent": "not_an_object"}'),
        ),
    )
    @settings(deadline=None)
    def test_load_always_returns_valid_config(
        self,
        content: str,
    ) -> None:
        """For any input content, load() must return a KiroClawConfig
        instance without raising an exception.

        **Validates: Requirements 6.6**
        """
        result = _load_from_raw_string(content)

        assert isinstance(result, KiroClawConfig)
        assert isinstance(result.agent, AgentConfig)
        assert isinstance(result.session, SessionConfig)
        assert isinstance(result.memory, MemoryConfig)
        assert isinstance(result.slack, SlackConfig)
        assert isinstance(result.dashboard, DashboardConfig)
        assert isinstance(result.hooks, dict)
        assert isinstance(result.workspaces, dict)
        assert isinstance(result.default_workspace, str)
        assert isinstance(result.auto_update, bool)

    # Feature: config-schema, Property 14: Deprecated fields are accepted during loading
    @_requires_jsonschema
    @given(
        command_val=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_deprecated_fields_accepted_during_loading(
        self,
        command_val: str,
    ) -> None:
        """When a field is marked deprecated, load() must still accept
        and apply the provided value (not fall back to default).

        Since there are currently no deprecated fields in the config,
        this test temporarily marks ``slack.command`` as deprecated and
        verifies the value is still loaded.

        **Validates: Requirements 8.2**
        """
        from kiro_claw.config import schema as schema_mod

        # Find and temporarily mark slack.command as deprecated
        target_entry = None
        for entry in schema_mod.SCHEMA_REGISTRY:
            if entry.path == "slack.command":
                target_entry = entry
                break
        assert target_entry is not None, "slack.command not in SCHEMA_REGISTRY"

        original_deprecated = target_entry.deprecated
        # Also patch JSON Schema x-meta
        slack_props = (
            schema_mod.JSON_SCHEMA.get("properties", {})
            .get("slack", {})
            .get("properties", {})
            .get("command", {})
        )
        original_xmeta_dep = slack_props.get("x-meta", {}).get("deprecated", False)

        try:
            object.__setattr__(target_entry, "deprecated", True)
            if "x-meta" in slack_props:
                slack_props["x-meta"]["deprecated"] = True

            data: dict = {"slack": {"command": command_val}}
            loaded = _load_from_dict(data)

            assert loaded.slack.command == command_val, (
                f"Expected deprecated field slack.command={command_val!r}, "
                f"got {loaded.slack.command!r}"
            )
        finally:
            object.__setattr__(target_entry, "deprecated", original_deprecated)
            if "x-meta" in slack_props:
                slack_props["x-meta"]["deprecated"] = original_xmeta_dep


# ---------------------------------------------------------------------------
# Phase 2: Agent-Workspace Bindings Property Tests
# ---------------------------------------------------------------------------


class TestAgentWorkspaceBindingsProperties:
    """Property-based tests for Phase 2 agent-workspace-bindings."""

    # Feature: agent-workspace-bindings, Property 1: New dataclass metadata completeness
    @given(
        cls_idx=st.integers(min_value=0, max_value=2),
    )
    @settings(deadline=None)
    def test_new_dataclass_metadata_completeness(
        self,
        cls_idx: int,
    ) -> None:
        """All fields of KiroClawAgentConfig, WorkspaceConfig, and
        MemoryStoreConfig carry required metadata (label, help).

        **Validates: Requirements 1.1, 3.1, 5.1**
        """
        import dataclasses

        classes = [KiroClawAgentConfig, WorkspaceConfig, MemoryStoreConfig]
        cls = classes[cls_idx]

        fields = dataclasses.fields(cls)
        assert len(fields) > 0, f"{cls.__name__} has no fields"

        for f in fields:
            meta = dict(f.metadata) if f.metadata else {}
            assert "label" in meta, f"{cls.__name__}.{f.name} missing 'label' in metadata"
            assert isinstance(meta["label"], str), f"{cls.__name__}.{f.name} label must be str"
            assert len(meta["label"]) > 0, f"{cls.__name__}.{f.name} label must not be empty"
            assert "help" in meta, f"{cls.__name__}.{f.name} missing 'help' in metadata"
            assert isinstance(meta["help"], str), f"{cls.__name__}.{f.name} help must be str"
            assert len(meta["help"]) > 0, f"{cls.__name__}.{f.name} help must not be empty"

    # Feature: agent-workspace-bindings, Property 5: Workspace migration preserves directory paths
    @given(
        raw_workspaces=st.dictionaries(
            keys=_safe_name_st,
            values=st.one_of(
                # Flat string format (legacy)
                st.text(min_size=1, max_size=30),
                # Structured dict format (new)
                st.builds(
                    lambda d: {"dir": d},
                    d=st.text(min_size=1, max_size=30),
                ),
            ),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_workspace_migration_preserves_directory_paths(
        self,
        raw_workspaces: dict,
    ) -> None:
        """For any workspace dict mixing flat strings and structured
        {"dir": str}, _migrate_workspaces produces WorkspaceConfig
        instances whose dir matches the original value.

        **Validates: Requirements 4.1, 4.2, 4.4**
        """
        result = _migrate_workspaces(raw_workspaces)

        if not raw_workspaces:
            # Empty input → default entry
            assert "default" in result
            assert result["default"].dir == "workspace"
        else:
            for name, value in raw_workspaces.items():
                assert name in result
                assert isinstance(result[name], WorkspaceConfig)
                if isinstance(value, str):
                    assert result[name].dir == value
                elif isinstance(value, dict):
                    assert result[name].dir == value.get("dir", "workspace")

    # Feature: agent-workspace-bindings, Property 10: Config serialization round-trip
    @given(config=_kiroclaw_config_st)
    @settings(deadline=None)
    def test_config_serialization_round_trip(
        self,
        config: KiroClawConfig,
    ) -> None:
        """For any valid KiroClawConfig with agents/workspaces/stores,
        to_dict() → load() produces an equivalent instance.

        **Validates: Requirements 9.4, 11.5**
        """
        d = config.to_dict()
        loaded = _load_from_dict(d)

        # Compare agents — migration may add a "default" agent if none exist
        if config.agents:
            # Existing agents are preserved; migration may add "default"
            for name in config.agents:
                assert name in loaded.agents
                assert loaded.agents[name].kiro_agent == config.agents[name].kiro_agent
                assert loaded.agents[name].workspace == config.agents[name].workspace
                assert loaded.agents[name].memory_store == config.agents[name].memory_store
        else:
            # Empty agents → migration creates "default" agent
            assert "default" in loaded.agents
            assert len(loaded.agents) >= 1

        # Compare default_agent — migration may fix invalid values
        if config.default_agent and config.default_agent in loaded.agents:
            assert loaded.default_agent == config.default_agent
        else:
            # Migration fixes invalid/empty default_agent
            assert loaded.default_agent in loaded.agents

        # Compare workspaces
        if config.workspaces:
            assert set(loaded.workspaces.keys()) == set(config.workspaces.keys())
            for name in config.workspaces:
                assert loaded.workspaces[name].dir == config.workspaces[name].dir
        else:
            # Empty workspaces → default entry synthesized by _migrate_workspaces
            assert "default" in loaded.workspaces
            assert loaded.workspaces["default"].dir == "workspace"

        # Compare memory_stores
        if config.memory_stores:
            assert set(loaded.memory_stores.keys()) == set(config.memory_stores.keys())
            for name in config.memory_stores:
                assert (
                    loaded.memory_stores[name].description == config.memory_stores[name].description
                )
                assert (
                    loaded.memory_stores[name].embedding_provider
                    == config.memory_stores[name].embedding_provider
                )
        else:
            # Empty memory_stores → default entry synthesized
            assert "default" in loaded.memory_stores

        # Compare default_memory_store
        assert loaded.default_memory_store == config.default_memory_store

        # Compare core fields still round-trip
        assert loaded.agent.approval_mode == config.agent.approval_mode
        assert loaded.agent.provider == config.agent.provider
        assert loaded.session.timeout_secs == config.session.timeout_secs
        assert loaded.memory.embedding_provider == config.memory.embedding_provider
        assert loaded.default_workspace == config.default_workspace
        assert loaded.auto_update == config.auto_update

    # Feature: agent-workspace-bindings, Property 11: Serialization format correctness
    @pytest.mark.skipif(platform.system() == "Darwin", reason="Hypothesis flaky on macOS CI")
    @given(config=_kiroclaw_config_st)
    @settings(deadline=None)
    def test_serialization_format_correctness(
        self,
        config: KiroClawConfig,
    ) -> None:
        """For any config, to_dict() output has agents as dict-of-dicts,
        workspaces values as dicts with dir key, memory_stores as
        dict-of-dicts.

        **Validates: Requirements 11.1, 11.2, 11.3, 11.4**
        """
        d = config.to_dict()

        # agents is a dict of dicts with expected keys
        assert isinstance(d["agents"], dict)
        for name, agent_dict in d["agents"].items():
            assert isinstance(agent_dict, dict)
            assert "kiro_agent" in agent_dict
            assert "workspace" in agent_dict
            assert "memory_store" in agent_dict

        # workspaces values are dicts with "dir" key
        assert isinstance(d["workspaces"], dict)
        for name, ws_dict in d["workspaces"].items():
            assert isinstance(ws_dict, dict)
            assert "dir" in ws_dict

        # memory_stores is a dict of dicts with expected keys
        assert isinstance(d["memory_stores"], dict)
        for name, ms_dict in d["memory_stores"].items():
            assert isinstance(ms_dict, dict)
            assert "description" in ms_dict
            assert "embedding_provider" in ms_dict

        # default_agent and default_memory_store are present
        assert "default_agent" in d
        assert isinstance(d["default_agent"], str)
        assert "default_memory_store" in d
        assert isinstance(d["default_memory_store"], str)

    # Feature: agent-workspace-bindings, Property 6: Memory store merge correctness
    @given(
        top_level=st.fixed_dictionaries(
            {},
            optional={
                "embedding_provider": st.sampled_from(["none", "ollama"]),
                "embedding_url": st.text(min_size=1, max_size=40),
                "allow_remote_embedding": st.booleans(),
                "embedding_dim": st.sampled_from([256, 512, 1024]),
                "embedding_timeout_secs": st.floats(min_value=1.0, max_value=30.0),
                "semantic_confidence_threshold": st.floats(min_value=0.0, max_value=1.0),
                "episodic_dedup_threshold": st.floats(min_value=0.0, max_value=1.0),
                "episodic_max_results": st.integers(min_value=1, max_value=50),
                "history_max_days": st.integers(min_value=1, max_value=365),
                "migrated": st.booleans(),
            },
        ),
        store_overrides=st.fixed_dictionaries(
            {},
            optional={
                "description": st.text(min_size=0, max_size=30),
                "embedding_provider": st.sampled_from(["", "none", "ollama"]),
                "embedding_url": st.one_of(st.just(""), st.text(min_size=1, max_size=40)),
                "embedding_dim": st.one_of(st.just(None), st.sampled_from([256, 512, 1024])),
            },
        ),
    )
    @settings(deadline=None)
    def test_memory_store_merge_correctness(
        self,
        top_level: dict,
        store_overrides: dict,
    ) -> None:
        """For any top-level memory dict and partial store override dict,
        resolve_memory_store_config produces a merged dict where
        store-level values override and unspecified fields inherit from
        top-level.

        **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
        """
        merged = resolve_memory_store_config(top_level, store_overrides)

        # Req 6.2: Unspecified fields inherit from top-level
        for key, value in top_level.items():
            if key not in store_overrides:
                assert merged[key] == value, (
                    f"Key '{key}' should inherit from top-level "
                    f"(expected {value!r}, got {merged.get(key)!r})"
                )

        # Req 6.3: Explicit non-empty, non-None store values override
        for key, value in store_overrides.items():
            if key == "description":
                # description is store-only metadata, must not appear in merged
                assert key not in merged or merged.get(key) == top_level.get(
                    key
                ), "'description' should be skipped during merge"
                continue
            if value != "" and value is not None:
                assert merged[key] == value, (
                    f"Key '{key}' should be overridden by store "
                    f"(expected {value!r}, got {merged.get(key)!r})"
                )

        # Req 6.4: Empty string and None values do not override
        for key, value in store_overrides.items():
            if key == "description":
                continue
            if value == "" or value is None:
                # Should inherit from top-level (or not be present if not in top-level)
                if key in top_level:
                    assert merged[key] == top_level[key], (
                        f"Key '{key}' with empty/None value should inherit from top-level "
                        f"(expected {top_level[key]!r}, got {merged.get(key)!r})"
                    )

        # Original top_level dict must not be mutated
        assert merged is not top_level

    # Feature: agent-workspace-bindings, Property 3: Resolver correct bindings
    @given(
        agent_name=_safe_name_st,
        ws_name=_safe_name_st,
        store_name=_safe_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=20),
        ws_dir=st.text(min_size=1, max_size=30),
        store_desc=st.text(min_size=0, max_size=20),
        store_provider=st.sampled_from(["", "none", "ollama"]),
    )
    @settings(deadline=None)
    def test_resolver_correct_bindings(
        self,
        agent_name: str,
        ws_name: str,
        store_name: str,
        kiro_agent_name: str,
        ws_dir: str,
        store_desc: str,
        store_provider: str,
    ) -> None:
        """For configs with valid agent→workspace→memory_store chains,
        resolve_agent_bindings returns the correct workspace dir and
        memory store name.

        **Validates: Requirements 7.1, 7.2, 7.5**
        """
        config = KiroClawConfig(
            agents={
                agent_name: KiroClawAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace=ws_name,
                    memory_store=store_name,
                ),
            },
            default_agent=agent_name,
            workspaces={ws_name: WorkspaceConfig(dir=ws_dir)},
            default_workspace=ws_name,
            memory_stores={
                store_name: MemoryStoreConfig(
                    description=store_desc,
                    embedding_provider=store_provider,
                )
            },
            default_memory_store=store_name,
        )

        # Resolve via explicit agent_name
        result = resolve_agent_bindings(config, agent_name=agent_name)
        assert isinstance(result, ResolvedBindings)
        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == store_name
        assert result.kiro_agent == kiro_agent_name

        # Resolve via default_agent (no explicit agent_name)
        result2 = resolve_agent_bindings(config)
        assert result2.workspace_dir == Path(ws_dir)
        assert result2.memory_store_name == store_name
        assert result2.kiro_agent == kiro_agent_name

    # Feature: agent-workspace-bindings, Property 4: Resolver fallback on missing references
    @given(
        agent_name=_safe_name_st,
        missing_ws=_safe_name_st,
        missing_store=_safe_name_st,
        fallback_ws_name=_safe_name_st,
        fallback_store_name=_safe_name_st,
        fallback_ws_dir=st.text(min_size=1, max_size=30),
    )
    @settings(deadline=None)
    def test_resolver_fallback_on_missing_references(
        self,
        agent_name: str,
        missing_ws: str,
        missing_store: str,
        fallback_ws_name: str,
        fallback_store_name: str,
        fallback_ws_dir: str,
    ) -> None:
        """When an agent references a non-existent workspace or store,
        the resolver falls back to default_workspace / default_memory_store.

        **Validates: Requirements 7.3, 7.4, 2.3**
        """
        # Ensure the agent references names that do NOT exist in the maps
        assume(missing_ws != fallback_ws_name)
        assume(missing_store != fallback_store_name)

        config = KiroClawConfig(
            agents={
                agent_name: KiroClawAgentConfig(
                    kiro_agent="some-agent",
                    workspace=missing_ws,
                    memory_store=missing_store,
                ),
            },
            default_agent=agent_name,
            workspaces={fallback_ws_name: WorkspaceConfig(dir=fallback_ws_dir)},
            default_workspace=fallback_ws_name,
            memory_stores={fallback_store_name: MemoryStoreConfig()},
            default_memory_store=fallback_store_name,
        )

        result = resolve_agent_bindings(config, agent_name=agent_name)

        # Should fall back to default_workspace dir
        assert result.workspace_dir == Path(fallback_ws_dir)
        # Should fall back to default_memory_store name
        assert result.memory_store_name == fallback_store_name

    # Feature: agent-workspace-bindings, Property 8: Kiro agent validation warnings
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.builds(
                KiroClawAgentConfig,
                kiro_agent=st.text(min_size=0, max_size=20),
                workspace=st.just("default"),
                memory_store=st.just("default"),
            ),
            min_size=0,
            max_size=5,
        ),
        installed=st.lists(
            st.text(min_size=1, max_size=20),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_kiro_agent_validation_warnings(
        self,
        agents_data: dict[str, KiroClawAgentConfig],
        installed: list[str],
    ) -> None:
        """For configs with kiro_agent values and mock installed agent
        lists, validate_kiro_agent_references logs warnings for
        unresolved references and never raises.

        **Validates: Requirements 8.1, 8.2, 8.3**
        """
        config = KiroClawConfig(agents=agents_data)
        installed_set = set(installed)

        # Capture warnings
        log_messages: list[str] = []

        def capture_warning(msg: object, *args: object) -> None:
            try:
                log_messages.append(str(msg) % args)
            except Exception:
                log_messages.append(str(msg))

        with unittest.mock.patch.object(logger, "warning", capture_warning):
            # Must never raise
            validate_kiro_agent_references(config, installed)

        # Check that warnings were logged for unresolved references
        for mc_name, mc_agent in agents_data.items():
            if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_set:
                # Should have a warning mentioning this agent
                matching = [m for m in log_messages if mc_name in m and mc_agent.kiro_agent in m]
                assert len(matching) > 0, (
                    f"Expected warning for agent '{mc_name}' referencing "
                    f"'{mc_agent.kiro_agent}', got: {log_messages}"
                )

        # Agents with empty kiro_agent or matching installed agents should NOT warn
        for mc_name, mc_agent in agents_data.items():
            if not mc_agent.kiro_agent or mc_agent.kiro_agent in installed_set:
                # Use precise prefix to avoid substring false positives
                prefix = f"KiroClaw agent '{mc_name}' references"
                matching = [m for m in log_messages if prefix in m]
                assert len(matching) == 0, (
                    f"Unexpected warning for agent '{mc_name}' with "
                    f"kiro_agent='{mc_agent.kiro_agent}': {log_messages}"
                )

    # Feature: agent-workspace-bindings, Property 7: Workspace path resolution
    @given(
        ws_name=_safe_name_st,
        path_kind=st.sampled_from(["absolute_slash", "absolute_tilde", "relative"]),
        rel_segment=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(deadline=None)
    def test_workspace_path_resolution(
        self,
        ws_name: str,
        path_kind: str,
        rel_segment: str,
    ) -> None:
        """For absolute paths (``/...``, ``~/...``) the resolved path is
        absolute; for relative paths the resolved path is under
        ``config_dir()``.

        **Validates: Requirements 3.4**
        """
        if path_kind == "absolute_slash":
            dir_value = f"/tmp/ws-{rel_segment}"
        elif path_kind == "absolute_tilde":
            dir_value = f"~/ws-{rel_segment}"
        else:
            dir_value = rel_segment

        # Build a raw config dict with the structured workspace format
        raw_config = {
            "default_workspace": ws_name,
            "workspaces": {ws_name: {"dir": dir_value}},
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump(raw_config, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch(
                "kiro_claw.config.loader.config_path",
                return_value=tmp,
            ):
                result = workspace_dir_for(ws_name)
        finally:
            tmp.unlink(missing_ok=True)

        if path_kind == "absolute_slash":
            assert result.is_absolute(), (
                f"Absolute path '{dir_value}' should resolve to absolute, " f"got '{result}'"
            )
            assert str(result) == dir_value
        elif path_kind == "absolute_tilde":
            assert result.is_absolute(), (
                f"Tilde path '{dir_value}' should resolve to absolute, " f"got '{result}'"
            )
            # expanduser resolves ~ to home dir
            assert str(result) == str(Path(dir_value).expanduser())
        else:
            # Relative path should be under config_dir()
            assert result.is_absolute(), (
                f"Relative path should be resolved to absolute via config_dir(), " f"got '{result}'"
            )
            assert str(result) == str(config_dir() / dir_value)

    # Feature: agent-workspace-bindings, Property 2: Agents parsing with duplicate kiro_agent values
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.fixed_dictionaries(
                {
                    "kiro_agent": st.sampled_from(["kiroclaw", "oncall-agent", "custom", ""]),
                    "workspace": _safe_name_st,
                    "memory_store": _safe_name_st,
                },
            ),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_agents_parsing_with_duplicate_kiro_agent_values(
        self,
        agents_data: dict[str, dict[str, str]],
    ) -> None:
        """For any agents dict with optional duplicate kiro_agent values,
        load() parses all entries without error.

        **Validates: Requirements 1.3, 1.7**
        """
        raw_config: dict = {"agents": agents_data}
        cfg = _load_from_dict(raw_config)

        # All agent entries must be parsed
        assert set(cfg.agents.keys()) == set(agents_data.keys())

        for name, raw_entry in agents_data.items():
            parsed = cfg.agents[name]
            assert isinstance(parsed, KiroClawAgentConfig)
            assert parsed.kiro_agent == raw_entry["kiro_agent"]
            assert parsed.workspace == raw_entry["workspace"]
            assert parsed.memory_store == raw_entry["memory_store"]

        # Verify duplicate kiro_agent values are accepted (no error)
        kiro_values = [e["kiro_agent"] for e in agents_data.values()]
        if len(set(kiro_values)) < len(kiro_values):
            # Duplicates exist — config still loaded fine
            assert len(cfg.agents) == len(agents_data)

    # Feature: agent-workspace-bindings, Property 9: Backward compatibility with legacy configs
    @given(
        legacy_default_agent=st.text(min_size=0, max_size=20),
        flat_workspaces=st.dictionaries(
            keys=_safe_name_st,
            values=st.text(min_size=1, max_size=30),
            min_size=0,
            max_size=3,
        ),
        embedding_provider=st.sampled_from(["none", "ollama"]),
    )
    @settings(deadline=None)
    def test_backward_compatibility_with_legacy_configs(
        self,
        legacy_default_agent: str,
        flat_workspaces: dict[str, str],
        embedding_provider: str,
    ) -> None:
        """For legacy configs (no agents, no top-level default_agent,
        flat workspaces), load() migrates to include a default agent
        using agent.default_agent as kiro agent name.

        **Validates: Requirements 9.1, 9.2, 9.3, 9.5**
        """
        raw_config: dict = {
            "agent": {"default_agent": legacy_default_agent},
            "memory": {"embedding_provider": embedding_provider},
        }
        if flat_workspaces:
            raw_config["workspaces"] = flat_workspaces

        cfg = _load_from_dict(raw_config)

        # After migration: default agent created from legacy config
        assert isinstance(cfg.agents, dict)
        assert len(cfg.agents) >= 1
        assert "default" in cfg.agents
        assert cfg.default_agent == "default"

        # Req 9.5: agent.default_agent is preserved as kiro agent name
        # in the migrated default agent
        expected_kiro = legacy_default_agent if legacy_default_agent else "kiroclaw"
        assert cfg.agents["default"].kiro_agent == expected_kiro

        # Req 9.2: Flat workspaces auto-migrated to structured format
        # (schema validation may strip invalid entries, so only check
        # that surviving workspaces are structured)
        for ws_name, ws_cfg in cfg.workspaces.items():
            assert isinstance(ws_cfg, WorkspaceConfig)

        # Always has at least one workspace (default synthesized if empty)
        assert len(cfg.workspaces) >= 1

        # Req 9.3: No memory_stores → default synthesized
        assert "default" in cfg.memory_stores
        assert isinstance(cfg.memory_stores["default"], MemoryStoreConfig)

        # Resolve bindings → uses migrated default agent
        result = resolve_agent_bindings(cfg)
        assert result.kiro_agent == expected_kiro


class TestResourceIndependence:
    """Property-based test for resource independence between config types."""

    # Feature: agent-workspace-bindings, Property 12: Resource independence
    @given(
        check_workspace=st.booleans(),
    )
    @settings(deadline=None)
    def test_resource_independence(
        self,
        check_workspace: bool,
    ) -> None:
        """WorkspaceConfig has no agent/memory fields;
        MemoryStoreConfig has no workspace/agent fields.

        **Validates: Requirements 3.3, 5.6**
        """
        import dataclasses

        agent_memory_field_names = {
            "kiro_agent",
            "agent",
            "agents",
            "memory_store",
            "memory_stores",
            "memory",
            "default_agent",
        }
        workspace_field_names = {
            "workspace",
            "workspaces",
            "default_workspace",
            "dir",
        }

        if check_workspace:
            # WorkspaceConfig must not have agent or memory fields
            ws_fields = {f.name for f in dataclasses.fields(WorkspaceConfig)}
            overlap = ws_fields & agent_memory_field_names
            assert not overlap, f"WorkspaceConfig has agent/memory fields: {overlap}"
        else:
            # MemoryStoreConfig must not have workspace or agent fields
            ms_fields = {f.name for f in dataclasses.fields(MemoryStoreConfig)}
            ws_agent_names = workspace_field_names | {
                "kiro_agent",
                "agent",
                "agents",
            }
            overlap = ms_fields & ws_agent_names
            assert not overlap, f"MemoryStoreConfig has workspace/agent fields: {overlap}"


class TestEdgeCases:
    """Unit tests for edge cases in agent-workspace-bindings.

    **Validates: Requirements 2.4, 4.3, 4.4, 5.4**
    """

    def test_empty_agents_empty_default_agent_falls_back(self) -> None:
        """Empty agents + empty default_agent triggers migration to create
        a default agent, then resolver uses that agent.

        **Validates: Requirement 2.4**
        """
        raw_config: dict = {
            "agents": {},
            "default_agent": "",
            "workspaces": {"default": {"dir": "my-workspace"}},
            "default_workspace": "default",
            "memory_stores": {"default": {"description": "test"}},
            "default_memory_store": "default",
        }
        cfg = _load_from_dict(raw_config)

        # Migration creates default agent
        assert "default" in cfg.agents
        assert cfg.default_agent == "default"

        result = resolve_agent_bindings(cfg)
        # Resolves via migrated default agent → workspace "default" → "my-workspace"
        assert result.workspace_dir == Path("my-workspace")
        # Resolves via migrated default agent → memory_store "default"
        assert result.memory_store_name == "default"
        # Migrated default agent uses "kiroclaw" as kiro_agent (no legacy value)
        assert result.kiro_agent == "kiroclaw"

    def test_missing_workspaces_creates_default_entry(self) -> None:
        """Missing workspaces section creates default entry.

        **Validates: Requirement 4.4**
        """
        raw_config: dict = {"agent": {"default_agent": "test"}}
        cfg = _load_from_dict(raw_config)

        assert "default" in cfg.workspaces
        assert isinstance(cfg.workspaces["default"], WorkspaceConfig)
        assert cfg.workspaces["default"].dir == "workspace"

    def test_missing_memory_stores_synthesizes_default(self) -> None:
        """Missing memory_stores section synthesizes default store.

        **Validates: Requirement 5.4**
        """
        raw_config: dict = {
            "memory": {"embedding_provider": "ollama"},
        }
        cfg = _load_from_dict(raw_config)

        assert "default" in cfg.memory_stores
        assert isinstance(cfg.memory_stores["default"], MemoryStoreConfig)

    def test_embedding_model_loaded_from_config(self) -> None:
        """embedding_model from config.json is used instead of default."""
        raw_config: dict = {
            "memory": {"embedding_model": "snowflake-arctic-embed2"},
        }
        cfg = _load_from_dict(raw_config)
        assert cfg.memory.embedding_model == "snowflake-arctic-embed2"

    def test_embedding_runtime_loaded_from_config(self) -> None:
        """embedding_runtime from config.json is used instead of default 'native'."""
        raw_config: dict = {
            "memory": {"embedding_runtime": "docker"},
        }
        cfg = _load_from_dict(raw_config)
        assert cfg.memory.embedding_runtime == "docker"

    def test_embedding_runtime_defaults_to_native(self) -> None:
        """embedding_runtime defaults to 'native' when not in config."""
        cfg = _load_from_dict({})
        assert cfg.memory.embedding_runtime == "native"

    def test_to_dict_always_writes_structured_workspace_format(self) -> None:
        """to_dict() always writes structured workspace format.

        **Validates: Requirement 4.3**
        """
        # Load from structured format (flat strings are rejected by schema validation)
        raw_config: dict = {
            "workspaces": {
                "default": {"dir": "workspace"},
                "oncall": {"dir": "workspace-oncall"},
            },
        }
        cfg = _load_from_dict(raw_config)

        # Serialize
        d = cfg.to_dict()

        # Workspaces must be structured dicts with "dir" key
        assert isinstance(d["workspaces"], dict)
        for ws_name, ws_val in d["workspaces"].items():
            assert isinstance(
                ws_val, dict
            ), f"Workspace '{ws_name}' should be a dict, got {type(ws_val)}"
            assert "dir" in ws_val, f"Workspace '{ws_name}' missing 'dir' key"

        assert d["workspaces"]["default"]["dir"] == "workspace"
        assert d["workspaces"]["oncall"]["dir"] == "workspace-oncall"


class TestPersistentLogLevel:
    """Tests for the persistent log_level config field."""

    def test_default_log_level_is_warning(self) -> None:
        """When no log_level is specified, default is WARNING."""
        cfg = _load_from_dict({})
        assert cfg.agent.log_level == "WARNING"

    def test_log_level_loaded_from_config(self) -> None:
        """log_level is read from agent section."""
        cfg = _load_from_dict({"agent": {"log_level": "DEBUG"}})
        assert cfg.agent.log_level == "DEBUG"

    def test_log_level_case_insensitive(self) -> None:
        """log_level is uppercased on load."""
        cfg = _load_from_dict({"agent": {"log_level": "info"}})
        assert cfg.agent.log_level == "INFO"

    def test_log_level_round_trips_through_to_dict(self) -> None:
        """log_level survives save/load round-trip."""
        cfg = _load_from_dict({"agent": {"log_level": "ERROR"}})
        d = cfg.to_dict()
        assert d["agent"]["log_level"] == "ERROR"


# ---------------------------------------------------------------------------
# Phase 3: Multi-Agent Orchestration Property Tests (Task 1.5)
# ---------------------------------------------------------------------------


class TestMultiAgentOrchestrationProperties:
    """Property-based tests for multi-agent-orchestration config migration and resolver."""

    # Feature: multi-agent-orchestration, Property 1: Config load always produces at least one agent with valid default
    @given(
        config_shape=st.sampled_from(
            [
                "empty_object",
                "no_agents_key",
                "empty_agents_dict",
                "missing_default_agent",
                "valid_agents",
            ]
        ),
        legacy_default_agent=st.text(min_size=0, max_size=15),
    )
    @settings(deadline=None)
    def test_config_load_always_produces_agent_with_valid_default(
        self,
        config_shape: str,
        legacy_default_agent: str,
    ) -> None:
        """For any valid JSON config (including empty objects, configs with no
        agents key, configs with empty agents dict, and configs with missing
        default_agent), loading via KiroClawConfig.load() shall produce a config
        where len(config.agents) >= 1 and config.default_agent names a key in
        config.agents.

        **Validates: Requirements 6.1, 6.2, 6.3, 6.6**
        """
        if config_shape == "empty_object":
            data: dict = {}
        elif config_shape == "no_agents_key":
            data = {"agent": {"default_agent": legacy_default_agent}}
        elif config_shape == "empty_agents_dict":
            data = {"agents": {}, "default_agent": ""}
        elif config_shape == "missing_default_agent":
            data = {
                "agents": {
                    "myagent": {
                        "kiro_agent": "kiroclaw",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
            }
        else:  # valid_agents
            data = {
                "agents": {
                    "coding": {
                        "kiro_agent": "kiroclaw",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
                "default_agent": "coding",
            }

        cfg = _load_from_dict(data)

        assert len(cfg.agents) >= 1, (
            f"Expected at least 1 agent, got {len(cfg.agents)} "
            f"for config_shape={config_shape!r}"
        )
        assert cfg.default_agent in cfg.agents, (
            f"default_agent={cfg.default_agent!r} not in agents={list(cfg.agents.keys())} "
            f"for config_shape={config_shape!r}"
        )

    # Feature: multi-agent-orchestration, Property 2: Legacy kiro_agent preserved in migrated default
    @given(
        legacy_kiro=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(deadline=None)
    def test_legacy_kiro_agent_preserved_in_migrated_default(
        self,
        legacy_kiro: str,
    ) -> None:
        """For any config JSON with no agents section and a non-empty
        agent.default_agent value, loading shall produce a "default" agent
        whose kiro_agent field equals the legacy value.

        **Validates: Requirements 6.5**
        """
        data: dict = {
            "agent": {"default_agent": legacy_kiro},
        }
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents, "Migration should create 'default' agent"
        assert cfg.agents["default"].kiro_agent == legacy_kiro, (
            f"Expected kiro_agent={legacy_kiro!r}, " f"got {cfg.agents['default'].kiro_agent!r}"
        )

    # Feature: multi-agent-orchestration, Property 3: Existing agents preserved on load
    @given(
        agents_data=st.dictionaries(
            keys=_safe_name_st,
            values=st.fixed_dictionaries(
                {
                    "kiro_agent": st.text(min_size=1, max_size=15),
                    "workspace": _safe_name_st,
                    "memory_store": _safe_name_st,
                },
            ),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(deadline=None)
    def test_existing_agents_preserved_on_load(
        self,
        agents_data: dict[str, dict[str, str]],
    ) -> None:
        """For any config JSON with a non-empty agents section, loading shall
        preserve all existing agent entries without creating additional agents.

        **Validates: Requirements 6.4**
        """
        first_name = next(iter(agents_data))
        data: dict = {
            "agents": agents_data,
            "default_agent": first_name,
        }
        cfg = _load_from_dict(data)

        # All original agents must be preserved
        for name, raw_entry in agents_data.items():
            assert name in cfg.agents, f"Agent '{name}' was lost during load"
            assert cfg.agents[name].kiro_agent == raw_entry["kiro_agent"]
            assert cfg.agents[name].workspace == raw_entry["workspace"]
            assert cfg.agents[name].memory_store == raw_entry["memory_store"]

        # No additional agents should be created
        assert set(cfg.agents.keys()) == set(agents_data.keys()), (
            f"Expected agents {set(agents_data.keys())}, " f"got {set(cfg.agents.keys())}"
        )

    # Feature: multi-agent-orchestration, Property 4: Backward compatibility — migrated default produces identical resolution
    @given(
        legacy_kiro=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
            min_size=0,
            max_size=15,
        ),
        ws_dir=st.text(
            alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-/"),
            min_size=1,
            max_size=20,
        ),
        store_desc=st.text(min_size=0, max_size=15),
    )
    @settings(deadline=None)
    def test_backward_compat_migrated_default_produces_identical_resolution(
        self,
        legacy_kiro: str,
        ws_dir: str,
        store_desc: str,
    ) -> None:
        """For any legacy config JSON (no agents section), the ResolvedBindings
        produced by resolve_agent_bindings() on the loaded config shall have the
        same values as the previous legacy fallback path would have produced.

        **Validates: Requirements 7.4**
        """
        data: dict = {
            "agent": {"default_agent": legacy_kiro},
            "workspaces": {"default": {"dir": ws_dir}},
            "default_workspace": "default",
            "memory_stores": {"default": {"description": store_desc}},
            "default_memory_store": "default",
        }
        cfg = _load_from_dict(data)

        # The legacy fallback would have used:
        # - kiro_agent = agent.default_agent or "kiroclaw"
        # - workspace = default_workspace → workspaces["default"].dir
        # - memory_store = default_memory_store
        expected_kiro = legacy_kiro if legacy_kiro else "kiroclaw"

        result = resolve_agent_bindings(cfg)

        assert (
            result.kiro_agent == expected_kiro
        ), f"Expected kiro_agent={expected_kiro!r}, got {result.kiro_agent!r}"
        assert result.workspace_dir == Path(
            ws_dir
        ), f"Expected workspace_dir={ws_dir!r}, got {result.workspace_dir!r}"
        assert (
            result.memory_store_name == "default"
        ), f"Expected memory_store_name='default', got {result.memory_store_name!r}"

    # Feature: multi-agent-orchestration, Property 5: Agent resolution produces correct workspace and memory store
    # NOTE: This property is already covered by TestAgentWorkspaceBindingsProperties.test_resolver_correct_bindings
    # (Property 3 from agent-workspace-bindings spec). Adding a focused variant that validates
    # the multi-agent-orchestration requirements specifically.
    @given(
        agent_name=_safe_name_st,
        ws_name=_safe_name_st,
        store_name=_safe_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=15),
        ws_dir=st.text(min_size=1, max_size=20),
    )
    @settings(deadline=None)
    def test_agent_resolution_correct_workspace_and_memory_store(
        self,
        agent_name: str,
        ws_name: str,
        store_name: str,
        kiro_agent_name: str,
        ws_dir: str,
    ) -> None:
        """For any KiroClawConfig with agents and a valid agent name, calling
        resolve_agent_bindings(config, agent_name) shall return correct
        workspace_dir and memory_store_name.

        **Validates: Requirements 1.1, 1.3, 2.1, 2.4**
        """
        config = KiroClawConfig(
            agents={
                agent_name: KiroClawAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace=ws_name,
                    memory_store=store_name,
                ),
            },
            default_agent=agent_name,
            workspaces={ws_name: WorkspaceConfig(dir=ws_dir)},
            default_workspace=ws_name,
            memory_stores={store_name: MemoryStoreConfig()},
            default_memory_store=store_name,
        )

        result = resolve_agent_bindings(config, agent_name=agent_name)

        assert result.workspace_dir == Path(ws_dir)
        assert result.memory_store_name == store_name
        assert result.kiro_agent == kiro_agent_name

    # Feature: multi-agent-orchestration, Property 6: Non-KiroClaw agent names resolve via default agent
    @given(
        default_name=_safe_name_st,
        unknown_name=_safe_name_st,
        kiro_agent_name=st.text(min_size=1, max_size=15),
        ws_dir=st.text(min_size=1, max_size=20),
        store_name=_safe_name_st,
    )
    @settings(deadline=None)
    def test_non_kiroclaw_agent_names_resolve_via_default(
        self,
        default_name: str,
        unknown_name: str,
        kiro_agent_name: str,
        ws_dir: str,
        store_name: str,
    ) -> None:
        """For any agent name NOT in config.agents, calling
        resolve_agent_bindings(config, agent_name) shall return the same
        ResolvedBindings as calling with config.default_agent.

        **Validates: Requirements 1.2, 2.2, 7.2**
        """
        assume(unknown_name != default_name)

        config = KiroClawConfig(
            agents={
                default_name: KiroClawAgentConfig(
                    kiro_agent=kiro_agent_name,
                    workspace="default",
                    memory_store=store_name,
                ),
            },
            default_agent=default_name,
            workspaces={"default": WorkspaceConfig(dir=ws_dir)},
            default_workspace="default",
            memory_stores={store_name: MemoryStoreConfig()},
            default_memory_store=store_name,
        )

        result_unknown = resolve_agent_bindings(config, agent_name=unknown_name)
        result_default = resolve_agent_bindings(config, agent_name=default_name)

        assert result_unknown.workspace_dir == result_default.workspace_dir, (
            f"Unknown agent workspace_dir={result_unknown.workspace_dir} "
            f"!= default={result_default.workspace_dir}"
        )
        assert result_unknown.memory_store_name == result_default.memory_store_name, (
            f"Unknown agent memory_store_name={result_unknown.memory_store_name} "
            f"!= default={result_default.memory_store_name}"
        )
        assert result_unknown.kiro_agent == result_default.kiro_agent, (
            f"Unknown agent kiro_agent={result_unknown.kiro_agent} "
            f"!= default={result_default.kiro_agent}"
        )
        assert result_unknown.effective_memory_config == result_default.effective_memory_config


# ---------------------------------------------------------------------------
# Phase 3: Multi-Agent Orchestration Unit Tests (Task 1.6)
# ---------------------------------------------------------------------------


class TestMultiAgentMigrationEdgeCases:
    """Unit tests for config migration edge cases.

    **Validates: Requirements 6.1, 6.2, 6.5, 6.7, 1.4, 3.4**
    """

    def test_empty_config_creates_default_agent_and_persists(self) -> None:
        """Empty config → default agent created and persisted to disk.

        **Validates: Requirement 6.1**
        """
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump({}, f)
            tmp = Path(f.name)

        try:
            with unittest.mock.patch(
                "kiro_claw.config.loader.config_path",
                return_value=tmp,
            ):
                cfg = KiroClawConfig.load()

                # In-memory: default agent exists
                assert "default" in cfg.agents
                assert cfg.default_agent == "default"
                assert cfg.agents["default"].kiro_agent == "kiroclaw"
                assert cfg.agents["default"].workspace == "default"
                assert cfg.agents["default"].memory_store == "default"

                # On-disk: persisted via save()
                on_disk = json.loads(tmp.read_text(encoding="utf-8"))
                assert "agents" in on_disk
                assert "default" in on_disk["agents"]
                assert on_disk["default_agent"] == "default"
                assert on_disk["agents"]["default"]["kiro_agent"] == "kiroclaw"
        finally:
            tmp.unlink(missing_ok=True)
            # Clean up backup file
            bak = tmp.with_suffix(".json.bak")
            bak.unlink(missing_ok=True)

    def test_empty_agents_dict_creates_default_agent(self) -> None:
        """Empty agents dict → default agent created.

        **Validates: Requirement 6.2**
        """
        data: dict = {"agents": {}, "default_agent": ""}
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents
        assert cfg.default_agent == "default"
        assert len(cfg.agents) == 1
        assert cfg.agents["default"].kiro_agent == "kiroclaw"

    def test_legacy_agent_default_agent_used_as_kiro_agent(self) -> None:
        """Legacy agent.default_agent value used as kiro_agent in migrated default.

        **Validates: Requirement 6.5**
        """
        data: dict = {
            "agent": {"default_agent": "oncall-agent"},
        }
        cfg = _load_from_dict(data)

        assert "default" in cfg.agents
        assert cfg.agents["default"].kiro_agent == "oncall-agent"

    def test_setup_writes_default_agent(self) -> None:
        """kiroclaw setup creates config with default agent via
        _ensure_default_agent_in_config.

        **Validates: Requirement 6.7**
        """
        import tempfile

        from kiro_claw.cli_chat import _ensure_default_agent_in_config

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_config = Path(tmpdir) / "config.json"
            # Start with empty config
            tmp_config.write_text("{}", encoding="utf-8")

            with unittest.mock.patch(
                "kiro_claw.config.loader.config_path",
                return_value=tmp_config,
            ), unittest.mock.patch(
                "kiro_claw.cli_chat.config_path",
                return_value=tmp_config,
            ):
                _ensure_default_agent_in_config()

                on_disk = json.loads(tmp_config.read_text(encoding="utf-8"))
                assert "agents" in on_disk
                assert "default" in on_disk["agents"]
                assert on_disk["default_agent"] == "default"
                assert on_disk["agents"]["default"]["kiro_agent"] == "kiroclaw"
                assert on_disk["agents"]["default"]["workspace"] == "default"
                assert on_disk["agents"]["default"]["memory_store"] == "default"

    def test_resolver_with_missing_workspace_falls_back(self) -> None:
        """Resolver with missing workspace falls back to default_workspace.

        **Validates: Requirement 1.4**
        """
        config = KiroClawConfig(
            agents={
                "test": KiroClawAgentConfig(
                    kiro_agent="kiroclaw",
                    workspace="nonexistent",
                    memory_store="default",
                ),
            },
            default_agent="test",
            workspaces={"default": WorkspaceConfig(dir="my-fallback-dir")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig()},
            default_memory_store="default",
        )

        result = resolve_agent_bindings(config, agent_name="test")

        # Falls back to default_workspace dir
        assert result.workspace_dir == Path("my-fallback-dir")

    def test_resolver_with_empty_agent_name_uses_default(self) -> None:
        """Resolver with empty agent name uses default_agent.

        **Validates: Requirement 3.4**
        """
        config = KiroClawConfig(
            agents={
                "mydefault": KiroClawAgentConfig(
                    kiro_agent="kiroclaw",
                    workspace="default",
                    memory_store="default",
                ),
            },
            default_agent="mydefault",
            workspaces={"default": WorkspaceConfig(dir="ws-dir")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig()},
            default_memory_store="default",
        )

        # Empty string agent_name → uses default_agent
        result = resolve_agent_bindings(config, agent_name="")
        assert result.kiro_agent == "kiroclaw"
        assert result.workspace_dir == Path("ws-dir")

        # None agent_name → uses default_agent
        result2 = resolve_agent_bindings(config, agent_name=None)
        assert result2.kiro_agent == "kiroclaw"
        assert result2.workspace_dir == Path("ws-dir")


class TestReactionsEmptyStringFiltering:
    """Empty-string reaction values must be filtered out, preserving defaults."""

    def test_empty_string_reaction_filtered(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"slack": {"reactions": {"done": "", "error": "boom"}}})
        )
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroClawConfig.load()
        # Empty string should be dropped
        assert "done" not in cfg.slack.reactions
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"


class TestReactionsNullSuppression:
    """``null`` (JSON) / ``None`` (Python) values must be preserved as suppression sentinels."""

    def test_null_reaction_preserved(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"slack": {"reactions": {"done": None, "error": "boom"}}})
        )
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroClawConfig.load()
        # null should be preserved (distinct from absent key)
        assert "done" in cfg.slack.reactions
        assert cfg.slack.reactions["done"] is None
        # Non-empty value preserved
        assert cfg.slack.reactions["error"] == "boom"

    def test_non_string_non_null_filtered(self, tmp_path: Path) -> None:
        """Values that are neither strings nor null (e.g. numbers, bools) are dropped."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {"slack": {"reactions": {"done": 42, "error": True, "tool": "ok"}}}
            )
        )
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroClawConfig.load()
        assert "done" not in cfg.slack.reactions
        assert "error" not in cfg.slack.reactions
        assert cfg.slack.reactions["tool"] == "ok"


class TestSttStreamingDefault:
    """Pin the fresh-install default for `stt.streaming` to False."""

    def test_stt_config_dataclass_default_is_false(self) -> None:
        assert SttConfig().streaming is False

    def test_missing_stt_key_loads_streaming_false(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}))
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroClawConfig.load()
        assert cfg.stt.streaming is False

    def test_partial_stt_block_without_streaming_key_loads_false(
        self, tmp_path: Path
    ) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"stt": {"provider": "transcribe", "language_code": "en-US"}})
        )
        with unittest.mock.patch(
            "kiro_claw.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroClawConfig.load()
        assert cfg.stt.streaming is False


# ---------------------------------------------------------------------------
# Phase 3: Soft-Stop Config Field Tests
# ---------------------------------------------------------------------------


class TestSoftStopBudget:
    """Tests for agent.soft_stop_budget_secs config field."""

    def test_soft_stop_budget_default(self) -> None:
        """Default AgentConfig has soft_stop_budget_secs == 10.0."""
        cfg = AgentConfig()
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_valid_range(self) -> None:
        """AgentConfig accepts soft_stop_budget_secs within [0.5, 60.0]."""
        cfg = AgentConfig(soft_stop_budget_secs=10.0)
        assert cfg.soft_stop_budget_secs == 10.0

    def test_soft_stop_budget_too_low(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs below 0.5 to 0.5 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_claw.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=0.1)
        assert cfg.soft_stop_budget_secs == 0.5
        assert "out of range" in caplog.text

    def test_soft_stop_budget_too_high(self, caplog) -> None:
        """AgentConfig clamps soft_stop_budget_secs above 60.0 to 60.0 with a warning."""
        with caplog.at_level(logging.WARNING, logger="kiro_claw.config.loader"):
            cfg = AgentConfig(soft_stop_budget_secs=120.0)
        assert cfg.soft_stop_budget_secs == 60.0
        assert "out of range" in caplog.text

    def test_soft_stop_budget_appears_in_schema(self) -> None:
        """Generated config baseline includes soft_stop_budget_secs."""
        from kiro_claw.config.schema import SCHEMA_REGISTRY

        paths = [e.path for e in SCHEMA_REGISTRY]
        assert "agent.soft_stop_budget_secs" in paths

        entry = next(e for e in SCHEMA_REGISTRY if e.path == "agent.soft_stop_budget_secs")
        assert entry.type == "number"
        assert entry.default_value == 10.0


class TestDashboardMcpProbeTimeout:
    """Tests for the dashboard.mcp_probe_timeout_secs config field."""

    def test_dashboard_mcp_probe_timeout_default(self) -> None:
        """DashboardConfig defaults mcp_probe_timeout_secs to 15."""
        cfg = DashboardConfig()
        assert cfg.mcp_probe_timeout_secs == 15

    def test_dashboard_mcp_probe_timeout_from_json(self) -> None:
        """Loading config with mcp_probe_timeout_secs reads the value."""
        content = json.dumps({"dashboard": {"mcp_probe_timeout_secs": 30}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.mcp_probe_timeout_secs == 30

    def test_dashboard_mcp_probe_timeout_invalid_falls_back(self) -> None:
        """Non-int mcp_probe_timeout_secs falls back to default 15."""
        content = json.dumps({"dashboard": {"mcp_probe_timeout_secs": "fast"}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.mcp_probe_timeout_secs == 15


class TestTrackingChannelsValidation:
    """Tests for slack.tracking_channels validation and coercion."""

    def test_dict_format_passes_through(self) -> None:
        """Proper dict format with channel_id is accepted as-is."""
        data = {"slack": {"tracking_channels": [{"channel_id": "C0B371VEW5S", "name": "ops"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coerced_to_dict(self) -> None:
        """Bare channel ID strings are auto-coerced to dict format."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S"]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 1
        assert cfg.slack.tracking_channels[0]["channel_id"] == "C0B371VEW5S"

    def test_bare_string_coercion_logs_warning(self) -> None:
        """Bare string coercion produces a warning log."""
        data = {"slack": {"tracking_channels": ["C0B371VEW5S", "C1234567890"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert len(cfg.slack.tracking_channels) == 2
        assert any("bare string" in msg for msg in logs)

    def test_invalid_entries_rejected(self) -> None:
        """Entries that are neither valid dicts nor channel-ID strings are dropped."""
        data = {"slack": {"tracking_channels": [123, None, {"name": "no-id"}]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 0

    def test_invalid_entries_log_warning(self) -> None:
        """Invalid entries produce a warning log."""
        data = {"slack": {"tracking_channels": [42, "not-a-channel-id"]}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert any("invalid entries" in msg for msg in logs)

    def test_mixed_format_all_valid_coerced(self) -> None:
        """Mix of dicts and bare strings both work."""
        data = {"slack": {"tracking_channels": [
            {"channel_id": "C111", "name": "one"},
            "C222",
        ]}}
        cfg = _load_from_dict(data)
        assert len(cfg.slack.tracking_channels) == 2
        ids = {c["channel_id"] for c in cfg.slack.tracking_channels}
        assert ids == {"C111", "C222"}

    def test_empty_list_no_warnings(self) -> None:
        """Empty tracking_channels produces no warnings."""
        data = {"slack": {"tracking_channels": []}}
        cfg, logs = _load_from_dict_with_logs(data)
        assert cfg.slack.tracking_channels == []
        assert not any("tracking_channels" in msg for msg in logs)


class TestAllowedEnterpriseIdsFiltering:
    """Tests for ``slack.allowed_enterprise_ids`` prefix filtering.

    The loader accepts both ``E``-prefix Slack enterprise IDs (org-level) and
    ``T``-prefix workspace IDs (Enterprise Grid child workspaces).  Other
    prefixes and non-string entries are dropped.
    """

    def test_e_prefix_enterprise_id_kept(self) -> None:
        """Standard E-prefix enterprise IDs (Slack org-level) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids

    def test_t_prefix_workspace_id_kept(self) -> None:
        """T-prefix workspace IDs (Enterprise Grid child workspaces) are preserved."""
        data = {"slack": {"allowed_enterprise_ids": ["T016NEJQWE9"]}}
        cfg = _load_from_dict(data)
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_mixed_e_and_t_prefix_kept(self) -> None:
        """Both E- and T-prefix IDs coexist in the allowlist."""
        data = {
            "slack": {"allowed_enterprise_ids": ["E015GUGD2V6", "T016NEJQWE9"]}
        }
        cfg = _load_from_dict(data)
        assert "E015GUGD2V6" in cfg.slack.allowed_enterprise_ids
        assert "T016NEJQWE9" in cfg.slack.allowed_enterprise_ids

    def test_invalid_prefix_dropped(self) -> None:
        """IDs with neither E nor T prefix are stripped."""
        data = {
            "slack": {"allowed_enterprise_ids": ["X999INVALID", "ABCDEF"]}
        }
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []

    def test_non_string_entries_dropped(self) -> None:
        """Non-string entries (int, None) are dropped without raising."""
        data = {"slack": {"allowed_enterprise_ids": [42, None, "E015GUGD2V6"]}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == ["E015GUGD2V6"]

    def test_empty_list_yields_empty_allowlist(self) -> None:
        """Empty list produces an empty allowlist."""
        data = {"slack": {"allowed_enterprise_ids": []}}
        cfg = _load_from_dict(data)
        assert cfg.slack.allowed_enterprise_ids == []


class TestWidgetDensityRoundTrip:
    """Tests for dashboard.widget_density persistence."""

    def test_widget_density_defaults_to_more(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.dashboard.widget_density == "more"

    def test_widget_density_loaded_from_config(self) -> None:
        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        assert cfg.dashboard.widget_density == "less"

    def test_widget_density_survives_save_load(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        cfg = _load_from_dict({"dashboard": {"widget_density": "less"}})
        cfg_file = tmp_path / "config.json"
        with patch("kiro_claw.config.loader.config_path", return_value=cfg_file):
            cfg.save()
            loaded = KiroClawConfig.load()
        assert loaded.dashboard.widget_density == "less"
