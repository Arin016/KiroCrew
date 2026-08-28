"""Handler-level tests for the conductor's MCP tools.

Every test here drives ``mcp_dashboard._call_tool_inner`` -- the real dispatch
entry point -- so the tool's contract is asserted where a caller actually meets
it: schema validation, bridge delegation, the in-band spec gate, and JSON
serialization. Reproducing the handler's body in a test instead would assert a
copy and leave the shipped block with zero consumers.

The gate tests are the load-bearing ones. ``conductor_accept_eval`` is
auto-approved on the conductor's grant list, so unlike the ``execute_bash``
door it reaches the machine with no approval prompt and no hook gate; the
sensitive-path and ``owner/name`` checks in ``conductor_scripts._gate_accept``
are the only call-time check left, and these pin them.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew import conductor_scripts, pinned_fs
from kiro_crew.mcp_dashboard import _call_tool_inner

#: A `file` verdict is a real pass/fail only where the probe can pin its walk. On a
#: host that cannot (Windows has no `O_DIRECTORY`/`dir_fd`), the kind is refused by
#: design, so a test asserting a probe OUTCOME has to say which platform contract it
#: is making. Tests that assert a GATE diagnostic — absolute path, sensitive path,
#: writable roots, `exists` type — are platform-neutral by construction (the guard
#: sits at the probe, after all of them) and carry no marker.
pinned_only = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="a file-kind verdict requires O_DIRECTORY, O_NOFOLLOW and dir_fd (POSIX)",
)


def _verdicts(payload: str) -> dict[str, dict[str, str]]:
    """Index a handler response's results by item id."""
    return {r["id"]: r for r in json.loads(payload)["results"]}


@pytest.fixture
def writable_root(tmp_path, monkeypatch):
    """Make a CHILD of ``tmp_path`` the agent's only writable root.

    The `file` gate admits a path only inside a tree the agent itself writes,
    which on a real host is the project checkout and the workspace root. Pointing
    that at a directory here is what lets a test say "inside a writable root"
    without writing into either.

    A child rather than ``tmp_path`` itself, so that a test needing a path
    OUTSIDE the root has somewhere to put it that is still inside the directory
    this test owns. Returning ``tmp_path`` made ``writable_root.parent`` the
    shared pytest base, and a file written there is residue every later test in
    the session inherits.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(conductor_scripts.github_runner, "agent_writable_roots", lambda: (root,))
    return root


class TestAcceptEvalHandler:
    """``conductor_accept_eval``: batch evaluation through the real handler."""

    @pinned_only
    def test_file_kind_pass_and_fail_in_one_batch(self, writable_root):
        target = writable_root / "artifact.txt"
        target.write_text("present", encoding="utf-8")
        out = _call_tool_inner(
            "conductor_accept_eval",
            {
                "items": [
                    {"id": "there", "accept": {"kind": "file", "path": str(target)}},
                    {
                        "id": "gone",
                        "accept": {"kind": "file", "path": str(writable_root / "missing.txt")},
                    },
                ]
            },
        )
        results = _verdicts(out)
        assert results["there"]["verdict"] == "pass"
        assert results["gone"]["verdict"] == "fail"

    def test_human_approval_is_pending_and_unknown_kind_is_error(self):
        out = _call_tool_inner(
            "conductor_accept_eval",
            {
                "items": [
                    {"id": "ask", "accept": {"kind": "human_approval"}},
                    {"id": "huh", "accept": {"kind": "nonexistent_kind"}},
                ]
            },
        )
        results = _verdicts(out)
        assert results["ask"]["verdict"] == "pending"
        assert results["huh"]["verdict"] == "error"

    def test_cmd_kind_stays_refused(self):
        """The removed ``cmd`` kind must not come back through this door."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "shell", "accept": {"kind": "cmd", "cmd": "rm -rf /"}}]},
        )
        assert _verdicts(out)["shell"]["verdict"] == "refused"

    def test_one_bad_item_does_not_hide_its_siblings(self):
        """The per-item contract, asserted on the handler's own loop."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {
                "items": [
                    {"accept": "not an object"},
                    {"id": "good", "accept": {"kind": "human_approval"}},
                ]
            },
        )
        results = json.loads(out)["results"]
        assert results[0]["id"] == "#0"
        assert results[0]["verdict"] == "error"
        assert results[1]["verdict"] == "pending"

    def test_missing_items_is_a_validation_error(self):
        with pytest.raises(Exception):
            _call_tool_inner("conductor_accept_eval", {})


class TestAcceptEvalGate:
    """The in-band gate this auto-approved door adds over the CLI path."""

    def test_a_relative_path_is_refused_before_anything_resolves_it(self):
        """A relative path would resolve against the SERVER's directory."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "rel", "accept": {"kind": "file", "path": "build/out.txt"}}]},
        )
        result = _verdicts(out)["rel"]
        assert result["verdict"] == "refused"
        assert "must be absolute" in result["evidence"]

    def test_sensitive_path_is_refused_not_probed(self, writable_root):
        """A path the hooks gate calls sensitive never reaches ``exists()``."""
        with patch.object(conductor_scripts.hooks, "validate_file_path", return_value=None):
            out = _call_tool_inner(
                "conductor_accept_eval",
                {
                    "items": [
                        {
                            "id": "probe",
                            "accept": {"kind": "file", "path": str(writable_root / "id_rsa")},
                        }
                    ]
                },
            )
        result = _verdicts(out)["probe"]
        assert result["verdict"] == "refused"
        assert "filesystem gate" in result["evidence"]

    def test_a_path_outside_the_writable_roots_is_refused(self, writable_root, tmp_path):
        """``/etc/hosts`` is unreachable by construction, not by policy lookup.

        The nested ``items[i].accept.path`` is invisible to the PreToolUse
        argument classifier, so no upstream policy ever sees it; the reachable
        set is narrowed here instead, which cannot fail open.
        """
        # Inside the directory this test owns, but outside the writable root the
        # gate was told about — so nothing is written where a later test can see it.
        outside = writable_root.parent / "outside.txt"
        assert outside.parent == tmp_path, "the probe target must stay inside tmp_path"
        outside.write_text("x", encoding="utf-8")
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "esc", "accept": {"kind": "file", "path": str(outside)}}]},
        )
        result = _verdicts(out)["esc"]
        assert result["verdict"] == "refused"
        assert "outside the project and workspace roots" in result["evidence"]

    def test_every_file_spec_is_refused_when_no_root_can_be_named(self, monkeypatch, tmp_path):
        """Fail CLOSED: an unnameable root is a refusal, never a wide-open probe."""
        target = tmp_path / "x.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(conductor_scripts.github_runner, "agent_writable_roots", lambda: ())
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "f", "accept": {"kind": "file", "path": str(target)}}]},
        )
        assert _verdicts(out)["f"]["verdict"] == "refused"

    def test_gate_is_consulted_for_every_file_spec(self, writable_root):
        target = writable_root / "ok.txt"
        target.write_text("x", encoding="utf-8")
        with patch.object(
            conductor_scripts.hooks,
            "validate_file_path",
            wraps=conductor_scripts.hooks.validate_file_path,
        ) as gate:
            _call_tool_inner(
                "conductor_accept_eval",
                {"items": [{"id": "f", "accept": {"kind": "file", "path": str(target)}}]},
            )
        gate.assert_called_once_with(str(target))

    @pinned_only
    def test_verdict_reports_the_canonical_path_that_was_stated(self, writable_root):
        """The gate's canonical path is what gets stat-ed, and what is reported."""
        real = writable_root / "real.txt"
        real.write_text("x", encoding="utf-8")
        link = writable_root / "link.txt"
        link.symlink_to(real)
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "f", "accept": {"kind": "file", "path": str(link)}}]},
        )
        result = _verdicts(out)["f"]
        assert result["verdict"] == "pass"
        assert str(real) in result["evidence"]

    @pytest.mark.parametrize(
        "repo",
        ["../../etc", "owner/repo/extra", "owner", "own er/repo", "./x", "owner/.."],
    )
    def test_unsafe_repo_is_refused_before_gh_runs(self, repo):
        """``gh`` carries ambient credentials, so the slug is charset-bounded."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "pr", "accept": {"kind": "pr_checks", "pr": 1, "repo": repo}}]},
        )
        result = _verdicts(out)["pr"]
        assert result["verdict"] == "refused"
        assert "owner/name" in result["evidence"]

    @pytest.mark.parametrize(
        "accept", [{"kind": "pr_checks", "pr": 1}, {"kind": "pr_checks", "pr": 1, "repo": ""}]
    )
    def test_a_missing_repo_is_refused_rather_than_resolved_from_the_cwd(self, accept):
        """No ``--repo`` means ``gh`` picks the repo from the server's directory."""
        out = _call_tool_inner("conductor_accept_eval", {"items": [{"id": "pr", "accept": accept}]})
        result = _verdicts(out)["pr"]
        assert result["verdict"] == "refused"
        assert "explicit repo" in result["evidence"]

    def test_a_well_formed_repo_reaches_the_evaluator_through_the_audited_spawn(self):
        with patch.object(
            conductor_scripts._get_accept_eval(), "_run", return_value=("pass", "exit 0")
        ) as run:
            out = _call_tool_inner(
                "conductor_accept_eval",
                {
                    "items": [
                        {
                            "id": "pr",
                            "accept": {"kind": "pr_checks", "pr": 7, "repo": "owner/name.js"},
                        }
                    ]
                },
            )
        assert _verdicts(out)["pr"]["verdict"] == "pass"
        assert run.call_args[0][0] == ["gh", "pr", "checks", "7", "--repo", "owner/name.js"]
        # The door hands the script its own audited spawn, never the direct one.
        assert run.call_args.kwargs["spawn"] is conductor_scripts._audited_spawn

    def test_non_integer_pr_is_still_the_script_s_error(self):
        """Shape errors stay with the script so both doors report identically."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "pr", "accept": {"kind": "pr_checks", "pr": True, "repo": "o/r"}}]},
        )
        assert _verdicts(out)["pr"]["verdict"] == "error"

    @pytest.mark.parametrize("path", [None, "", 7, ["/tmp/x"]])
    def test_a_non_string_path_is_the_script_s_error_not_a_refusal(self, path):
        """The gate passes a malformed path through so one message serves both doors."""
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "f", "accept": {"kind": "file", "path": path}}]},
        )
        result = _verdicts(out)["f"]
        assert result["verdict"] == "error"
        assert "needs a path" in result["evidence"]

    def test_a_non_object_accept_is_left_to_the_script(self):
        out = _call_tool_inner(
            "conductor_accept_eval", {"items": [{"id": "f", "accept": "not an object"}]}
        )
        assert _verdicts(out)["f"]["verdict"] == "error"


class TestExistenceProbeIsPinned:
    """The probe must not re-walk a name the gate already resolved."""

    def test_the_door_injects_the_pinned_prober(self, writable_root):
        target = writable_root / "f.txt"
        target.write_text("x", encoding="utf-8")
        with patch.object(
            conductor_scripts._get_accept_eval(), "_evaluate", return_value=("pass", "ok")
        ) as ev:
            _call_tool_inner(
                "conductor_accept_eval",
                {"items": [{"id": "f", "accept": {"kind": "file", "path": str(target)}}]},
            )
        assert ev.call_args.kwargs["exists"] is conductor_scripts._pinned_exists
        assert ev.call_args.kwargs["spawn"] is conductor_scripts._audited_spawn

    @pinned_only
    def test_an_ancestor_swapped_after_resolution_is_refused_not_followed(
        self, writable_root, monkeypatch
    ):
        """The check-to-use race, reproduced: resolve, then swap a parent dir.

        The roots this door admits are the trees the agent itself writes, so the
        swap is something the model can actually perform.
        """
        real_parent = writable_root / "sub"
        real_parent.mkdir()
        approved = real_parent / "f.txt"  # never created: a by-name probe says False
        elsewhere = writable_root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "f.txt").write_text("x", encoding="utf-8")

        # The gate resolved `approved` BEFORE the swap; now the parent is a link.
        real_parent.rmdir()
        real_parent.symlink_to(elsewhere)
        monkeypatch.setattr(
            conductor_scripts.hooks, "validate_file_path", lambda raw: str(approved)
        )

        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "race", "accept": {"kind": "file", "path": str(approved)}}]},
        )
        result = _verdicts(out)["race"]
        assert result["verdict"] == "refused"
        assert "symbolic link" in result["evidence"]

    def test_a_platform_that_cannot_pin_refuses_the_file_kind(self, writable_root, monkeypatch):
        """pinned_fs never degrades silently, so this door fails closed."""
        target = writable_root / "f.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(conductor_scripts.pinned_fs, "supports_pinned_walk", lambda: False)
        out = _call_tool_inner(
            "conductor_accept_eval",
            {"items": [{"id": "f", "accept": {"kind": "file", "path": str(target)}}]},
        )
        result = _verdicts(out)["f"]
        assert result["verdict"] == "refused"
        assert "cannot pin" in result["evidence"]

    @pinned_only
    def test_the_pinned_probe_agrees_with_the_direct_one_on_a_clean_tree(self, tmp_path):
        """Same answer when nothing is racing: the guard adds no false negatives."""
        there = tmp_path / "there.txt"
        there.write_text("x", encoding="utf-8")
        gone = tmp_path / "gone.txt"
        script = conductor_scripts._get_accept_eval()
        assert conductor_scripts._pinned_exists(str(there)) is True
        assert script._exists_direct(str(there)) is True
        assert conductor_scripts._pinned_exists(str(gone)) is False
        assert script._exists_direct(str(gone)) is False


class TestExistsMustBeABoolean:
    """``bool("false")`` is True, so coercion would invert the verdict."""

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, "", None, [], {}])
    def test_a_non_boolean_exists_is_an_error_not_an_inverted_verdict(self, writable_root, bad):
        target = writable_root / "f.txt"
        target.write_text("x", encoding="utf-8")
        out = _call_tool_inner(
            "conductor_accept_eval",
            {
                "items": [
                    {"id": "f", "accept": {"kind": "file", "path": str(target), "exists": bad}}
                ]
            },
        )
        result = _verdicts(out)["f"]
        assert result["verdict"] == "error"
        assert "must be true or false" in result["evidence"]

    def test_the_cli_door_rejects_it_too(self):
        """One spelling: the type check lives in the script, not in the gate."""
        script = conductor_scripts._get_accept_eval()
        verdict, evidence = script._evaluate(
            {"accept": {"kind": "file", "path": "/nonexistent", "exists": "false"}}
        )
        assert verdict == "error"
        assert "must be true or false" in evidence

    @pinned_only
    @pytest.mark.parametrize("want,expected", [(True, "pass"), (False, "fail")])
    def test_a_real_boolean_still_decides_the_verdict(self, writable_root, want, expected):
        target = writable_root / "f.txt"
        target.write_text("x", encoding="utf-8")
        out = _call_tool_inner(
            "conductor_accept_eval",
            {
                "items": [
                    {"id": "f", "accept": {"kind": "file", "path": str(target), "exists": want}}
                ]
            },
        )
        assert _verdicts(out)["f"]["verdict"] == expected


class TestAuditedSpawn:
    """``gh`` on this door must be a trusted binary, spawned through the chokepoint."""

    def test_it_resolves_gh_and_runs_it_through_run_gh(self):
        """A bare ``gh`` would be a PATH lookup the model's own workspace can win."""
        sentinel = object()
        with (
            patch.object(
                conductor_scripts.github_runner, "resolve_gh", return_value="/trusted/bin/gh"
            ) as resolve,
            patch.object(
                conductor_scripts.github_runner, "run_gh", return_value=sentinel
            ) as run_gh,
        ):
            assert conductor_scripts._audited_spawn(["gh", "pr", "checks", "7"], 300.0) is sentinel
        resolve.assert_called_once_with()
        # argv[0] is REPLACED by the resolved absolute path; the rest is verbatim.
        assert run_gh.call_args[0][0] == ["/trusted/bin/gh", "pr", "checks", "7"]
        assert run_gh.call_args.kwargs["timeout"] == 300.0
        assert run_gh.call_args.kwargs["audit_caller"] == "core:goal-conductor"

    def test_a_setup_failure_becomes_the_script_s_could_not_run_error(self):
        """No usable gh, or unavailable audit storage, is an error verdict."""
        with patch.object(
            conductor_scripts.github_runner,
            "resolve_gh",
            side_effect=conductor_scripts.github_runner.SetupError("no usable gh"),
        ):
            with pytest.raises(OSError, match="no usable gh"):
                conductor_scripts._audited_spawn(["gh", "pr", "checks", "1"], 300.0)

    def test_it_refuses_a_chosen_directory_rather_than_ignoring_it(self):
        """run_gh has no cwd; dropping one silently restores the wrong-repo verdict."""
        with pytest.raises(OSError, match="chosen directory"):
            conductor_scripts._audited_spawn(["gh", "pr", "checks", "1"], 300.0, cwd="/somewhere")

    def test_the_segment_charset_is_imported_not_respelled(self):
        assert conductor_scripts._SEGMENT_RE is conductor_scripts.github_runner._SEGMENT_RE


class TestBatchLoopHasOneSpelling:
    """The handler must not carry its own copy of the per-item loop."""

    def test_handler_delegates_the_loop_to_the_script(self):
        with patch.object(
            conductor_scripts, "evaluate_items", return_value=[{"id": "x", "verdict": "pass"}]
        ) as bridge:
            out = _call_tool_inner("conductor_accept_eval", {"items": [{"id": "x"}]})
        bridge.assert_called_once_with([{"id": "x"}])
        assert json.loads(out) == {"results": [{"id": "x", "verdict": "pass"}]}

    def test_bridge_delegates_the_loop_to_accept_eval(self):
        script = conductor_scripts._get_accept_eval()
        with patch.object(script, "evaluate_items", return_value=[]) as loop:
            conductor_scripts.evaluate_items([{"id": "x"}])
        assert loop.call_args.kwargs["evaluate"] is conductor_scripts._gated_evaluate

    def test_script_loop_and_handler_agree_on_a_batch(self):
        """One spelling means one behaviour, so assert them equal.

        The batch mixes a spec the gate refuses with one it passes through: the
        script's own loop is called with the same gated handler, so the two must
        agree item for item.
        """
        script = conductor_scripts._get_accept_eval()
        items = [
            {"accept": {"kind": "human_approval"}},
            {"id": "bad-repo", "accept": {"kind": "pr_checks", "pr": 1, "repo": "../etc"}},
        ]
        assert json.loads(_call_tool_inner("conductor_accept_eval", {"items": items})) == {
            "results": script.evaluate_items(items, evaluate=conductor_scripts._gated_evaluate)
        }

    def test_schema_rejects_a_non_object_item_before_the_loop(self):
        """``item_type=dict`` means the loop's isinstance guard is defence in depth."""
        with pytest.raises(Exception):
            _call_tool_inner("conductor_accept_eval", {"items": [7]})


class TestLedgerEntryHandler:
    """``conductor_ledger_entry``: codec dispatch through the real handler."""

    def test_encode_then_decode_round_trips(self):
        entry = {
            "accept": {"kind": "file", "path": "/tmp/out.txt", "exists": True},
            "session": "dashboard:slot-y",
            "round": 3,
            "status": "pass",
        }
        encoded = json.loads(
            _call_tool_inner("conductor_ledger_entry", {"mode": "encode", "payload": entry})
        )
        assert encoded["ok"] is True
        assert "\n" not in encoded["value"]
        decoded = json.loads(
            _call_tool_inner(
                "conductor_ledger_entry",
                {"mode": "decode", "payload": {"value": encoded["value"]}},
            )
        )
        assert decoded["ok"] is True
        assert decoded["entry"] == entry

    def test_validate_and_rotate_reach_their_modes(self):
        encoded = json.loads(
            _call_tool_inner(
                "conductor_ledger_entry",
                {
                    "mode": "encode",
                    "payload": {
                        "accept": {"kind": "pr_checks", "pr": 1},
                        "session": "s-1",
                        "round": 1,
                        "status": "pass",
                    },
                },
            )
        )
        artifacts = {"item-done": encoded["value"]}
        validated = json.loads(
            _call_tool_inner(
                "conductor_ledger_entry", {"mode": "validate", "payload": {"artifacts": artifacts}}
            )
        )
        assert validated["ok"] is True
        assert validated["violations"] == []
        rotated = json.loads(
            _call_tool_inner(
                "conductor_ledger_entry", {"mode": "rotate", "payload": {"artifacts": artifacts}}
            )
        )
        assert rotated["ok"] is True
        assert "item-done" in rotated["collapsed"]

    def test_domain_error_is_structured_not_raised(self):
        out = json.loads(
            _call_tool_inner(
                "conductor_ledger_entry",
                {"mode": "encode", "payload": {"session": "s-1", "round": 1, "status": "running"}},
            )
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "missing_field"

    def test_unknown_mode_is_rejected_by_the_schema(self):
        with pytest.raises(Exception):
            _call_tool_inner("conductor_ledger_entry", {"mode": "frobnicate", "payload": {}})

    def test_bridge_still_refuses_an_unknown_mode_on_its_own(self):
        """Defence in depth: the schema is the first gate, not the only one."""
        result = conductor_scripts.ledger_mode("frobnicate", {})
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_mode"
        assert "frobnicate" in result["error"]["detail"]

    def test_unexpected_bridge_exception_becomes_internal_error(self):
        """The handler's own try/except, exercised through the handler."""
        with patch.object(
            conductor_scripts, "ledger_mode", side_effect=RuntimeError("bridge broke")
        ):
            out = json.loads(
                _call_tool_inner(
                    "conductor_ledger_entry", {"mode": "validate", "payload": {"artifacts": {}}}
                )
            )
        assert out["ok"] is False
        assert out["error"]["code"] == "internal_error"
        assert "bridge broke" in out["error"]["detail"]
