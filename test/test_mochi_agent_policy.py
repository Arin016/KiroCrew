"""Tests for Mochi's MCP reach policy and the framework merge that applies it.

Every case here pins a behaviour whose failure mode is SILENT: a grant that
never reaches the agent config, or a deny that reads like a deny and behaves
like an allow.
"""

from __future__ import annotations

import json

from kiro_crew.apps.bridges import _apply_agent_mcp_policy
from kiro_crew.apps.builtins.mochi.agent_policy import (
    BG_AGENT,
    CHAT_AGENT,
    build_policy,
    write_policy,
)


class TestBuildPolicy:
    def test_audience_maps_chat_and_bg_to_real_agent_names(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        pol = build_policy(
            {
                "extraMcpServers": [
                    {"name": "a", "agents": ["chat"]},
                    {"name": "b", "agents": ["bg"]},
                    {"name": "c", "agents": ["chat", "bg"]},
                ]
            }
        )
        assert sorted(pol["agents"][CHAT_AGENT]["servers"]) == ["a", "c"]
        assert sorted(pol["agents"][BG_AGENT]["servers"]) == ["b", "c"]

    def test_string_entry_defaults_to_chat_only(self, monkeypatch):
        """Legacy settings stored bare strings; they must not silently grant bg."""
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        pol = build_policy({"extraMcpServers": ["legacy"]})
        assert "legacy" in pol["agents"][CHAT_AGENT]["servers"]
        assert "legacy" not in pol["agents"][BG_AGENT]["servers"]

    def test_ungranted_ambient_server_is_neutralized_with_its_real_tools(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"ambient": ["t1", "t2"]},
        )
        pol = build_policy({"extraMcpServers": []})
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {"ambient": ["t1", "t2"]}

    def test_unknown_tools_are_recorded_not_emitted_as_a_hollow_deny(self, monkeypatch):
        """An empty disabledTools list declares the server and disables nothing.

        Emitting it would read as a deny and behave as an allow — the exact
        fail-open this policy exists to prevent.
        """
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"unprobed": []},
        )
        pol = build_policy({"extraMcpServers": []})
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {}
        assert pol["agents"][CHAT_AGENT]["pendingNeutralize"] == ["unprobed"]

    def test_granted_server_is_never_also_neutralized(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"shared": ["t1"]},
        )
        pol = build_policy({"extraMcpServers": [{"name": "shared", "agents": ["chat"]}]})
        assert "shared" in pol["agents"][CHAT_AGENT]["servers"]
        assert "shared" not in pol["agents"][CHAT_AGENT]["neutralize"]
        # ...but the bg agent, which was NOT granted it, still gets it denied.
        assert "shared" in pol["agents"][BG_AGENT]["neutralize"]

    def test_own_server_is_never_neutralized(self, monkeypatch):
        """The app's own MCP server is the pet's reason to exist."""
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers",
            lambda: {"mochi:mochi": ["perform_pet_action"], "other": ["x"]},
        )
        pol = build_policy({"extraMcpServers": []})
        for agent in (CHAT_AGENT, BG_AGENT):
            assert "mochi:mochi" not in pol["agents"][agent]["neutralize"]
            assert "other" in pol["agents"][agent]["neutralize"]

    def test_discovery_failure_does_not_raise(self, monkeypatch):
        def boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", boom)
        pol = build_policy({"extraMcpServers": []})
        assert pol["agents"][CHAT_AGENT]["neutralize"] == {}

    def test_write_policy_lands_where_the_framework_reads_it(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            "kiro_crew.apps.builtins.mochi.agent_policy._ambient_servers", lambda: {}
        )
        write_policy(tmp_path, {"extraMcpServers": ["x"]})
        written = tmp_path / bridges.AGENT_MCP_POLICY_FILE
        assert written.is_file()
        assert "x" in json.loads(written.read_text())["agents"][CHAT_AGENT]["servers"]


class TestApplyAgentMcpPolicy:
    def _policy(self, **per_agent):
        return {"agents": {CHAT_AGENT: per_agent}}

    def test_grant_adds_server_and_tool_reference(self, monkeypatch):
        # A grant must produce a COMPLETE server spec: the policy carries only
        # POLICY, so the launch command comes from the ambient MCP config. A
        # command-less entry never launches, so the tool is simply absent and
        # the agent reports "not available" with nothing logged anywhere.
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"srv": {"command": "srv-cmd", "args": []}}
        )
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read"], "allowedTools": ["fs_read"]},
            CHAT_AGENT,
            self._policy(servers={"srv": {"autoApprove": ["a"], "disabledTools": []}}),
        )
        assert out["mcpServers"]["srv"]["autoApprove"] == ["a"]
        assert out["mcpServers"]["srv"]["command"] == "srv-cmd"
        assert "@srv" in out["tools"]
        # allowedTools too: a server only in `tools` still prompts per call,
        # which for an unattended agent resolves to "rejected".
        assert "@srv" in out["allowedTools"]

    def test_grant_without_any_launch_spec_is_skipped(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read"]},
            CHAT_AGENT,
            self._policy(servers={"ghost": {"autoApprove": [], "disabledTools": []}}),
        )
        assert "ghost" not in (out.get("mcpServers") or {})
        assert "@ghost" not in out["tools"]

    def test_neutralize_declares_the_server_and_removes_its_tool_reference(self, monkeypatch):
        # The entry must carry the FULL spec (copied from the global mcp.json):
        # kiro-cli's strict agent loader rejects the whole file over a
        # command-less mcpServers entry, unregistering the agent instead of
        # denying the server.
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"amb": {"command": "amb-cmd", "args": []}}
        )
        out = _apply_agent_mcp_policy(
            {"name": CHAT_AGENT, "tools": ["fs_read", "@amb"]},
            CHAT_AGENT,
            self._policy(neutralize={"amb": ["t1", "t2"]}),
        )
        assert out["mcpServers"]["amb"]["disabledTools"] == ["t1", "t2"]
        assert out["mcpServers"]["amb"]["command"] == "amb-cmd"
        assert "@amb" not in out["tools"]

    def test_policy_for_another_agent_is_ignored(self):
        original = {"name": BG_AGENT, "tools": ["fs_read"]}
        out = _apply_agent_mcp_policy(dict(original), BG_AGENT, self._policy(servers={"srv": {}}))
        assert out.get("mcpServers", {}) == {} or "srv" not in out["mcpServers"]
        assert out["tools"] == ["fs_read"]

    def test_empty_policy_leaves_the_config_untouched(self):
        cfg = {"name": CHAT_AGENT, "tools": ["fs_read"], "mcpServers": {"k": {}}}
        assert _apply_agent_mcp_policy(dict(cfg), CHAT_AGENT, {}) == cfg


class TestQueuedPetActionIsExecutable:
    """A pet action queued by the MCP server must be executable by the poller.

    The MCP server runs as a SEPARATE process (``kirocrew app mcp mochi``) and can
    only hand work over through the queue file, so the file's contract is the only
    thing keeping the two halves in step. It previously wrote the payload without
    ``execute_after`` / ``id`` / ``urgent``, and every one of those omissions fails
    SILENTLY: get_executable_tasks() treats a missing execute_after as not-due, so
    the action sat in the queue and the pet simply never moved.
    """

    def _queue(self, monkeypatch, tmp_path, args):
        from kiro_crew.apps.builtins.mochi import mcp_server as ms

        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        ms._tool_perform_pet_action(args)
        from kiro_crew.apps.builtins.mochi import queue_file as qf

        return qf, ms, qf.read_queue(str(tmp_path / ms._QUEUE_FILE))

    def test_move_is_due_immediately(self, monkeypatch, tmp_path):
        qf, ms, queue = self._queue(
            monkeypatch, tmp_path, {"action": "move", "waypoints": [{"x": 1, "y": 2}]}
        )
        due = qf.get_executable_tasks(queue, ms._now_ms())
        assert len(due) == 1, "queued move is not due — the pet would never move"
        assert due[0]["type"] == "move"

    def test_task_carries_an_id_so_it_can_be_marked_done(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(monkeypatch, tmp_path, {"action": "mood", "mood": "happy"})
        assert queue["tasks"][0].get("id"), "no id — the poller re-executes it every second"

    def test_task_is_urgent_so_the_stale_skip_cannot_drop_it(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(monkeypatch, tmp_path, {"action": "move", "x": 5, "y": 6})
        assert queue["tasks"][0].get("urgent") is True

    def test_payload_survives_into_the_task(self, monkeypatch, tmp_path):
        _, _, queue = self._queue(
            monkeypatch, tmp_path, {"action": "move", "behavior": "hide_left", "interrupt": False}
        )
        task = queue["tasks"][0]
        assert task["behavior"] == "hide_left" and task["interrupt"] is False

    def test_query_does_not_queue_anything(self, monkeypatch, tmp_path):
        from kiro_crew.apps.builtins.mochi import mcp_server as ms

        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        out = ms._tool_perform_pet_action({"action": "query"})
        assert "displays" in out
        assert not (tmp_path / ms._QUEUE_FILE).exists()
