"""Tests for config loader."""

import json

from kiro_claw.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OFF,
    ChannelConfig,
    KiroClawConfig,
    config_dir,
)


class TestKiroClawConfig:
    def test_defaults(self):
        cfg = KiroClawConfig()
        assert cfg.agent.approval_mode == "auto"
        assert cfg.agent.streaming is True
        assert cfg.session.timeout_secs == 3600

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCLAW_HOME", str(tmp_path / "empty"))
        cfg = KiroClawConfig.load()
        assert cfg.agent.approval_mode == "auto"

    def test_load_from_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".kiroclaw" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(
            json.dumps(
                {
                    "agent": {"approval_mode": "interactive", "streaming": False},
                    "session": {"timeout_secs": 600},
                    "hooks": {"auto_approve_tools": ["ReadFile"]},
                }
            )
        )
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)

        cfg = KiroClawConfig.load()
        assert cfg.agent.approval_mode == "interactive"
        assert cfg.agent.streaming is False
        assert cfg.session.timeout_secs == 600
        assert cfg.hooks == {"auto_approve_tools": ["ReadFile"]}

    def test_load_invalid_json(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json")
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)

        cfg = KiroClawConfig.load()
        assert cfg.agent.approval_mode == "auto"  # falls back to defaults


class TestChannelConfig:
    def test_default_dm_activation_always(self):
        cfg = KiroClawConfig()
        ch = cfg.channel_config("D1234")
        assert ch.activation == ACTIVATION_ALWAYS

    def test_default_group_activation_mention(self):
        cfg = KiroClawConfig()
        ch = cfg.channel_config("C1234")
        assert ch.activation == ACTIVATION_MENTION

    def test_per_channel_override(self):
        cfg = KiroClawConfig(
            slack_channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS, agent="ops")}
        )
        ch = cfg.channel_config("C1234")
        assert ch.activation == ACTIVATION_ALWAYS
        assert ch.agent == "ops"

    def test_unknown_channel_uses_default(self):
        cfg = KiroClawConfig(slack_channels={"C1234": ChannelConfig(activation=ACTIVATION_OFF)})
        ch = cfg.channel_config("C9999")
        assert ch.activation == ACTIVATION_MENTION

    def test_dm_activation_override(self):
        cfg = KiroClawConfig(slack_dm_activation=ACTIVATION_MENTION)
        ch = cfg.channel_config("D5678")
        assert ch.activation == ACTIVATION_MENTION

    def test_channel_config_from_dict(self):
        ch = ChannelConfig.from_dict({"activation": "always", "agent": "reviewer"})
        assert ch.activation == ACTIVATION_ALWAYS
        assert ch.agent == "reviewer"

    def test_channel_config_from_dict_invalid_activation(self):
        ch = ChannelConfig.from_dict({"activation": "bogus"})
        assert ch.activation == ACTIVATION_MENTION

    def test_load_channels_from_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".kiroclaw" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(
            json.dumps(
                {
                    "slack": {
                        "channels": {
                            "C111": {"activation": "always", "agent": "ops"},
                            "C222": {"activation": "off"},
                        },
                        "dm_activation": "mention",
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)

        cfg = KiroClawConfig.load()
        assert "C111" in cfg.slack_channels
        assert cfg.slack_channels["C111"].activation == ACTIVATION_ALWAYS
        assert cfg.slack_channels["C111"].agent == "ops"
        assert cfg.slack_channels["C222"].activation == ACTIVATION_OFF
        assert cfg.slack_dm_activation == ACTIVATION_MENTION

    def test_invalid_dm_activation_falls_back_to_mention(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".kiroclaw" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({"slack": {"dm_activation": "bogus"}}))
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)

        cfg = KiroClawConfig.load()
        assert cfg.slack_dm_activation == ACTIVATION_MENTION


class TestConfigDir:
    def test_config_dir_is_home_based(self):
        d = config_dir()
        assert d.name == ".kiroclaw"


class TestTrustedBotIds:
    def test_default_empty(self):
        cfg = KiroClawConfig()
        assert cfg.slack.trusted_bot_ids == set()

    def test_load_from_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".kiroclaw" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({"slack": {"trusted_bot_ids": ["B07AAA", "B07BBB"]}}))
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)
        cfg = KiroClawConfig.load()
        assert cfg.slack.trusted_bot_ids == {"B07AAA", "B07BBB"}

    def test_round_trip(self):
        from kiro_claw.config.loader import SlackConfig

        cfg = KiroClawConfig(slack=SlackConfig(trusted_bot_ids={"B07BBB", "B07AAA"}))
        d = cfg.to_dict()
        assert d["slack"]["trusted_bot_ids"] == ["B07AAA", "B07BBB"]  # sorted

    def test_empty_not_serialized(self):
        cfg = KiroClawConfig()
        d = cfg.to_dict()
        assert "trusted_bot_ids" not in d.get("slack", {})


class TestTimezoneConfig:
    def test_default_empty(self):
        cfg = KiroClawConfig()
        assert cfg.timezone == ""

    def test_load_from_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"timezone": "America/Los_Angeles"}))
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)
        cfg = KiroClawConfig.load()
        assert cfg.timezone == "America/Los_Angeles"

    def test_load_missing_timezone(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": {}}))
        monkeypatch.setattr("kiro_claw.config.loader.config_path", lambda: cfg_file)
        cfg = KiroClawConfig.load()
        assert cfg.timezone == ""
