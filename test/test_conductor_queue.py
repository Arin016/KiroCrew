"""The conductor's durable inbox + dispatch record (``scripts/queue.py``).

Three properties are load-bearing, and each has a class here:

- **A parked message survives.** A claim does not delete it, a turn that dies
  after claiming does not take it with it, and a full inbox refuses rather than
  dropping the oldest — every entry is a message the user typed.
- **A retried dispatch converges instead of duplicating.** ``dispatch_begin``
  returns the SAME id for an item already begun, and says so, which is the whole
  point: a fresh id per attempt is what opens a second session for one item.
- **It touches no identity.** The script is a plain local state machine; if it
  ever grew a read of a session key or a gateway credential it would be gating on
  an assertion, since a script has no verifiable identity. Pinned as a source
  ratchet rather than trusted to review.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from skill_script_helpers import load_skill_script

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-conductor"
    / "scripts"
    / "queue.py"
)


def _mod():
    return load_skill_script("_conductor_queue_under_test", SCRIPT)


def _run(mode: str, payload: dict, home: Path) -> dict:
    """Drive the script the way the conductor does: argv mode + JSON on stdin."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(home), "KIROCREW_HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode in (0, 2), proc.stderr
    return json.loads(proc.stdout)


class TestInvocationContract:
    """Same shape as the sibling scripts: one JSON in, one JSON out."""

    def test_an_unknown_mode_is_exit_2_on_stdout(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "drain"],
            input="{}",
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        # stdout, not stderr: the conductor reads one stream.
        assert "usage: queue.py" in json.loads(proc.stdout)["error"]

    def test_a_json_array_on_stdin_is_exit_2_not_a_crash(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "status"],
            input="[1, 2]",
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["error"] == "stdin must be a JSON object"

    def test_every_mode_reports_where_the_state_landed(self, tmp_path: Path) -> None:
        """``state_path`` is how a conductor tells 'empty' from 'somewhere else'."""
        out = _run("status", {"goal": "g1"}, tmp_path)
        assert out["ok"] is True
        assert out["state_path"].endswith("conductor/g1/queue.json")
        assert out["exists"] is False


class TestGoalIdIsValidatedNotSanitized:
    """A goal id names a directory, and sanitizing merges two goals into one."""

    @pytest.mark.parametrize(
        "goal",
        ["../escape", "a/b", "", ".hidden", "x" * 65, "has space", "sym+bol"],
    )
    def test_a_goal_that_is_not_one_safe_segment_is_refused(self, goal: str) -> None:
        mod = _mod()
        got = mod.mode_status({"goal": goal})
        assert got["ok"] is False
        assert got["error"]["code"] == "bad_goal"

    def test_traversal_never_reaches_a_path(self, tmp_path: Path) -> None:
        out = _run("enqueue", {"goal": "../../etc", "text": "hi"}, tmp_path)
        assert out["error"]["code"] == "bad_goal"
        assert not (tmp_path.parent / "etc").exists()


class TestAParkedMessageSurvives:
    def test_enqueue_then_claim_returns_the_text(self, tmp_path: Path) -> None:
        _run("enqueue", {"goal": "g", "text": "drop item 2"}, tmp_path)
        out = _run("claim", {"goal": "g"}, tmp_path)
        assert [c["text"] for c in out["claimed"]] == ["drop item 2"]

    def test_a_claim_does_not_delete_so_a_dead_turn_loses_nothing(self, tmp_path: Path) -> None:
        """The failure this script exists to prevent, asserted directly."""
        _run("enqueue", {"goal": "g", "text": "steer left"}, tmp_path)
        first = _run("claim", {"goal": "g"}, tmp_path)
        assert len(first["claimed"]) == 1
        # The turn that claimed it dies here — no `done` call is ever made.
        # A later claim with the staleness window elapsed re-serves the message.
        again = _run("claim", {"goal": "g", "stale_secs": 0}, tmp_path)
        assert [c["text"] for c in again["claimed"]] == ["steer left"]
        assert again["claimed"][0]["reclaimed"] == 1

    def test_a_fresh_claim_does_not_re_serve_a_live_claim(self, tmp_path: Path) -> None:
        """Re-serving instantly would double-apply every steering message."""
        _run("enqueue", {"goal": "g", "text": "x"}, tmp_path)
        _run("claim", {"goal": "g"}, tmp_path)
        assert _run("claim", {"goal": "g", "stale_secs": 900}, tmp_path)["claimed"] == []

    def test_done_drops_it_and_a_retried_done_is_safe(self, tmp_path: Path) -> None:
        got = _run("enqueue", {"goal": "g", "text": "applied"}, tmp_path)
        _run("claim", {"goal": "g"}, tmp_path)
        first = _run("done", {"goal": "g", "ids": [got["id"]]}, tmp_path)
        assert first["removed"] == 1
        # Idempotent: a conductor that lost the response must be able to repeat it
        # without having to guess.
        second = _run("done", {"goal": "g", "ids": [got["id"]]}, tmp_path)
        assert second["ok"] is True and second["removed"] == 0

    def test_release_puts_it_back_without_applying_it(self, tmp_path: Path) -> None:
        _run("enqueue", {"goal": "g", "text": "not yet"}, tmp_path)
        _run("claim", {"goal": "g"}, tmp_path)
        ids = [e["id"] for e in _run("status", {"goal": "g"}, tmp_path)["inbox"]]
        rel = _run("release", {"goal": "g", "ids": ids}, tmp_path)
        assert rel["released"] == 1 and rel["pending"] == 1

    def test_a_full_inbox_refuses_rather_than_dropping_the_oldest(self, tmp_path: Path) -> None:
        """Silently discarding a user's message is the failure, not the backlog."""
        mod = _mod()
        # Seeded as a fixture rather than through MAX_INBOX subprocess calls: the
        # property under test is the refusal at the cap, not the accumulation.
        state = tmp_path / "conductor" / "g" / "queue.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps(
                {
                    "version": 1,
                    "goal": "g",
                    "inbox": [
                        {"id": f"{i:012d}", "text": f"m{i}", "enqueued_at": 0, "state": "pending"}
                        for i in range(mod.MAX_INBOX)
                    ],
                    "dispatch": {},
                }
            ),
            encoding="utf-8",
        )
        full = _run("enqueue", {"goal": "g", "text": "one too many"}, tmp_path)
        assert full["ok"] is False and full["error"]["code"] == "inbox_full"
        # The oldest is still there — nothing was evicted to make room.
        inbox = _run("status", {"goal": "g"}, tmp_path)["inbox"]
        assert len(inbox) == mod.MAX_INBOX and inbox[0]["text"] == "m0"

    def test_an_oversize_message_refuses_rather_than_truncating(self, tmp_path: Path) -> None:
        """Half a steering instruction can invert its meaning."""
        mod = _mod()
        out = _run("enqueue", {"goal": "g", "text": "x" * (mod.MAX_TEXT + 1)}, tmp_path)
        assert out["error"]["code"] == "text_too_long"

    def test_a_corrupt_record_is_moved_aside_not_fatal(self, tmp_path: Path) -> None:
        _run("enqueue", {"goal": "g", "text": "before"}, tmp_path)
        state = tmp_path / "conductor" / "g" / "queue.json"
        state.write_text("{not json", encoding="utf-8")
        out = _run("enqueue", {"goal": "g", "text": "after"}, tmp_path)
        assert out["ok"] is True
        # Still on disk to look at, rather than deleted.
        assert list((tmp_path / "conductor" / "g").glob("queue.corrupt-*"))


class TestARetriedDispatchConverges:
    def test_begin_assigns_an_id_and_reports_no_replay(self, tmp_path: Path) -> None:
        out = _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        assert out["state"] == "begun" and out["replay"] is False and out["attempts"] == 1
        assert out["dispatch_id"]

    def test_a_second_begin_returns_the_same_id_and_flags_the_replay(self, tmp_path: Path) -> None:
        """A fresh id per attempt is what opens a second session for one item."""
        first = _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        second = _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        assert second["dispatch_id"] == first["dispatch_id"]
        assert second["replay"] is True and second["attempts"] == 2
        # And it names the consequence, because a session with no seed looks
        # exactly like a session that is merely quiet.
        assert "no seed" in second["warning"]

    def test_a_sent_item_replays_without_the_unseeded_warning(self, tmp_path: Path) -> None:
        _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        _run("dispatch_sent", {"goal": "g", "item": "item-1", "session": "dashboard:s1"}, tmp_path)
        again = _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        assert again["replay"] is True and again["state"] == "sent"
        assert "warning" not in again
        # The session it converged on travels with it, so a replay can read it.
        assert again["session"] == "dashboard:s1"

    def test_status_lists_the_items_that_never_recorded_a_seed(self, tmp_path: Path) -> None:
        _run("dispatch_begin", {"goal": "g", "item": "item-1"}, tmp_path)
        _run("dispatch_begin", {"goal": "g", "item": "item-2"}, tmp_path)
        _run("dispatch_sent", {"goal": "g", "item": "item-2", "session": "dashboard:s2"}, tmp_path)
        assert _run("status", {"goal": "g"}, tmp_path)["unsent_items"] == ["item-1"]

    def test_a_send_with_no_begin_is_recorded_and_flagged(self, tmp_path: Path) -> None:
        """The send already happened; refusing would make the record less true."""
        out = _run(
            "dispatch_sent", {"goal": "g", "item": "item-9", "session": "dashboard:s9"}, tmp_path
        )
        assert out["ok"] is True and "replay guard was skipped" in out["warning"]


class TestItTouchesNoIdentity:
    """The property that makes this safe as a script rather than an MCP tool."""

    def test_the_source_reads_no_identity_or_credential(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        # A script is a child of execute_bash, where the gateway injects no
        # verifiable identity — so anything here that read one would be reading an
        # assertion. Named individually so a new one fails loudly.
        for forbidden in (
            "KIROCREW_SESSION_KEY",
            "KIROCREW_HOST_PID",
            "_resolve_session_key",
            "X-Internal-Secret",
            "read_local_secret",
            "sel_hmac",
            "verify_session_pid",
        ):
            assert forbidden not in code, f"queue.py must not reach for {forbidden}"

    def test_it_makes_no_network_or_subprocess_call(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("subprocess", "urllib", "socket", "http.client", "requests"):
            assert forbidden not in code, f"queue.py must not use {forbidden}"


class TestUnreadableIsNotAbsent:
    """The defect this class exists for destroyed the parked messages.

    An earlier revision returned a BLANK record for any ``OSError``, and every
    mode then wrote that blank back — so a state file that merely could not be
    read this time was overwritten, losing exactly what the script is for.
    """

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unreadable_state_file_refuses_and_is_left_untouched(self, tmp_path: Path) -> None:
        _run("enqueue", {"goal": "g", "text": "must survive"}, tmp_path)
        state = tmp_path / "conductor" / "g" / "queue.json"
        before = state.read_bytes()
        state.chmod(0o000)
        try:
            out = _run("enqueue", {"goal": "g", "text": "second"}, tmp_path)
            assert out["ok"] is False
            assert out["error"]["code"] == "state_unreadable"
        finally:
            state.chmod(0o600)
        # The bytes are still the ones holding the first message.
        assert state.read_bytes() == before
        assert _run("status", {"goal": "g"}, tmp_path)["inbox"][0]["text"] == "must survive"

    def test_absent_still_reads_as_blank(self, tmp_path: Path) -> None:
        """Only UNREADABLE refuses; a goal with no file yet is normal."""
        out = _run("status", {"goal": "fresh"}, tmp_path)
        assert out["ok"] is True and out["inbox"] == [] and out["exists"] is False


class TestTheLockAlwaysConvergesOnAnAnswer:
    """A lock that cannot be judged must answer, not spin.

    The earlier revision's ``except OSError: continue`` around the staleness stat
    skipped both the deadline check and the sleep, so a stat that keeps failing
    burns a core forever instead of returning ``locked``.
    """

    def test_a_live_lock_times_out_instead_of_waiting_forever(self, tmp_path: Path) -> None:
        mod = _mod()
        mod.LOCK_WAIT_SECS = 0.2
        target = tmp_path / "queue.json"
        (tmp_path / "queue.lock").write_text("999999", encoding="utf-8")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with mod._Lock(target):
                pass
        assert time.monotonic() - started < 5.0

    def test_a_stat_that_keeps_failing_still_terminates(self, tmp_path: Path) -> None:
        """The busy-loop regression, forced: staleness can never be judged."""
        mod = _mod()
        mod.LOCK_WAIT_SECS = 0.2
        target = tmp_path / "queue.json"
        (tmp_path / "queue.lock").write_text("1", encoding="utf-8")
        real_stat = Path.stat

        def blind_stat(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            if self.name == "queue.lock":
                raise OSError("cannot stat")
            return real_stat(self, *a, **k)

        started = time.monotonic()
        with mock.patch.object(Path, "stat", blind_stat):
            with pytest.raises(TimeoutError):
                with mod._Lock(target):
                    pass
        elapsed = time.monotonic() - started
        # Converged on the deadline rather than spinning: the old code never
        # reached the deadline check on this path at all.
        assert elapsed < 5.0, f"took {elapsed:.1f}s — the deadline was not honoured"
        # And it did NOT steal a lock whose staleness it could not judge.
        assert (tmp_path / "queue.lock").exists()

    def test_a_stale_lock_is_stolen_so_a_crash_cannot_wedge_a_goal(self, tmp_path: Path) -> None:
        mod = _mod()
        target = tmp_path / "queue.json"
        lock = tmp_path / "queue.lock"
        lock.write_text("1", encoding="utf-8")
        old = time.time() - (mod.LOCK_STALE_SECS + 30)
        os.utime(lock, (old, old))
        with mod._Lock(target):
            pass  # acquired by stealing; no exception is the assertion
