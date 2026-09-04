"""The conductor's durable inbox + dispatch record (``scripts/dispatch_queue.py``).

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

import io
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
    / "dispatch_queue.py"
)


#: Mode bits are the skip axis, not the whole module: ``dispatch_queue.py`` is portable by
#: design (``_Lock`` uses ``O_EXCL`` rather than ``fcntl.flock`` precisely so it
#: runs on Windows), so the suite has to run there or the portability claim goes
#: untested. Only the case that needs ``0o000`` to MEAN unreadable is skipped --
#: on Windows a mode-0 file stays readable by its owner, so the case cannot
#: express what it asserts.
_POSIX_MODE_BITS_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs 0o000 to make a file unreadable; on Windows the owner still reads it",
)

#: The same reasoning for the liveness-gated steal: ``os.kill(pid, 0)`` is the
#: probe, and Windows has no non-destructive equivalent (``os.kill`` there
#: TERMINATES), so ``_holder_is_alive`` answers "cannot tell" and the only steal
#: path left is the abandon backstop. The backstop IS covered on every platform;
#: only the two cases that turn on a real dead-vs-alive answer are skipped.
_POSIX_LIVENESS_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no non-destructive liveness probe on Windows; the backstop path covers it",
)


def _mod():
    return load_skill_script("_conductor_queue_under_test", SCRIPT)


def _reaped_pid() -> int:
    """A pid that is definitely gone: spawn something trivial and reap it.

    A hard-coded "surely unused" number is a coin flip, and a zombie still answers
    ``os.kill(pid, 0)`` — so the child is waited on, which reaps it and makes the
    probe report it dead.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _stalling_write():
    """An ``os.write`` that stalls on the STATE record and passes everything else.

    The ENOSPC shape: a real short write returns a small count without raising. A
    mock that keeps returning 1 would let the loop finish a byte at a time and
    prove nothing, so this one makes one byte of progress and then none.

    Scoped by payload rather than by call count because the lock token goes through
    ``os.write`` too — stalling that instead would fail acquisition and never reach
    the write under test.
    """
    real_write = os.write
    stalled = {"yes": False}

    def stalling(fd, data):  # noqa: ANN001, ANN202
        if b'"inbox"' not in data:
            return real_write(fd, data)
        if not stalled["yes"] and len(data) > 1:
            stalled["yes"] = True
            return real_write(fd, data[:1])
        return 0

    return stalling


def _run(mode: str, payload: dict, home: Path) -> dict:
    """Drive the script the way the conductor does: argv mode + JSON on stdin.

    The parent environment is INHERITED and only the home overridden. A stripped
    env with a hand-written POSIX ``PATH`` would not start an interpreter on
    Windows, and the isolation that matters here is the state root, which
    ``KIROCREW_HOME`` already pins (``dispatch_queue.py`` resolves it through the package's
    own ``data_home``, whose override branch honours it).

    ``cwd`` is not tidiness: a child inheriting pytest's CWD starts in the
    checkout, so any relative path it ever writes lands in repository state rather
    than under ``tmp_path``. ``encoding`` pins the decode instead of taking the
    Windows ANSI code page.
    """
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "KIROCREW_HOME": str(home),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(home),
        env=env,
        check=False,
    )
    assert proc.returncode in (0, 2), proc.stderr
    return json.loads(proc.stdout)


class TestInvocationContract:
    """Same shape as the sibling scripts: one JSON in, one JSON out."""

    def test_an_unknown_mode_is_exit_2_on_stdout(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "drain"],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            check=False,
        )
        assert proc.returncode == 2
        # stdout, not stderr: the conductor reads one stream.
        assert "usage: dispatch_queue.py" in json.loads(proc.stdout)["error"]

    def test_a_json_array_on_stdin_is_exit_2_not_a_crash(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "status"],
            input="[1, 2]",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            check=False,
        )
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["error"] == "stdin must be a JSON object"

    def test_every_mode_reports_where_the_state_landed(self, tmp_path: Path) -> None:
        """``state_path`` is how a conductor tells 'empty' from 'somewhere else'."""
        out = _run("status", {"goal": "g1"}, tmp_path)
        assert out["ok"] is True
        # Compared as path PARTS, not a "/"-separated suffix: the script builds
        # the path with pathlib, so on Windows the string carries backslashes and
        # an endswith would never match.
        assert Path(out["state_path"]).parts[-3:] == ("conductor", "g1", "queue.json")
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

    @pytest.mark.parametrize(
        "body",
        [
            # Parses as JSON, is not a record this script can use, and in each
            # case still holds the conductor's own bookkeeping in readable form.
            '{"version": 1, "inbox": {"id": "x"}, "dispatch": {}}',
            '{"version": 1, "inbox": [], "dispatch": [["item-1", "abc"]]}',
            '["not", "an", "object"]',
            # One level DEEPER: the containers are right and the ENTRIES are not.
            # A container-only check passes these through, and then every mode
            # reaches for `.get` on a non-mapping.
            '{"version": 1, "inbox": [null], "dispatch": {}}',
            '{"version": 1, "inbox": ["just a string"], "dispatch": {}}',
            '{"version": 1, "inbox": [], "dispatch": {"item-1": "abc"}}',
        ],
    )
    def test_structurally_invalid_state_is_quarantined_not_silently_dropped(
        self, tmp_path: Path, body: str
    ) -> None:
        """Valid JSON of the wrong SHAPE used to be discarded with no sidecar.

        It read as blank and the next mutation wrote the blank back, so the
        dispatch ids and parked messages in it were gone with no copy anywhere —
        the same loss the unparseable path already guarded, reached through a
        different door. The sidecar is what makes it recoverable.
        """
        _run("enqueue", {"goal": "g", "text": "before"}, tmp_path)
        state = tmp_path / "conductor" / "g" / "queue.json"
        state.write_text(body, encoding="utf-8")
        out = _run("enqueue", {"goal": "g", "text": "after"}, tmp_path)
        assert out["ok"] is True
        sidecars = list((tmp_path / "conductor" / "g").glob("queue.corrupt-*"))
        assert sidecars, "a discarded record must leave its bytes on disk"
        assert sidecars[0].read_text(encoding="utf-8") == body

    def test_a_malformed_entry_is_a_structured_refusal_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        """The point of quarantining is that the CONDUCTOR gets an answer.

        A record whose containers are the right types but whose entries are not
        (``{"inbox": [null]}``) used to pass the shape check, and the mode then died
        with an uncaught ``AttributeError`` on ``entry.get`` — a traceback on stderr
        and no JSON on stdout, which is the one thing the conductor cannot handle.
        Asserted through the real subprocess, because that is where the difference
        between an exception and a structured answer actually shows.
        """
        _run("enqueue", {"goal": "g", "text": "before"}, tmp_path)
        state = tmp_path / "conductor" / "g" / "queue.json"
        state.write_text('{"version": 1, "inbox": [null], "dispatch": {}}', encoding="utf-8")
        # `_run` itself asserts the exit code is 0 or 2 and that stdout is JSON, so
        # a traceback fails here rather than being interpreted.
        out = _run("status", {"goal": "g"}, tmp_path)
        assert out["ok"] is True
        assert out["inbox"] == []
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
            assert forbidden not in code, f"dispatch_queue.py must not reach for {forbidden}"

    def test_it_makes_no_network_or_subprocess_call(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("subprocess", "urllib", "socket", "http.client", "requests"):
            assert forbidden not in code, f"dispatch_queue.py must not use {forbidden}"


class TestUnreadableIsNotAbsent:
    """The defect this class exists for destroyed the parked messages.

    An earlier revision returned a BLANK record for any ``OSError``, and every
    mode then wrote that blank back — so a state file that merely could not be
    read this time was overwritten, losing exactly what the script is for.
    """

    @_POSIX_MODE_BITS_ONLY
    @pytest.mark.skipif(
        # ``os.geteuid`` does not exist on Windows, and this decorator is
        # evaluated at COLLECTION time -- reading it directly raises
        # AttributeError before the skipif above can spare it, which errors every
        # shard rather than skipping one case.
        getattr(os, "geteuid", lambda: 1)() == 0,
        reason="root ignores the mode bits",
    )
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

    @_POSIX_LIVENESS_ONLY
    def test_a_stale_lock_that_cannot_be_unlinked_times_out_instead_of_spinning(
        self, tmp_path: Path
    ) -> None:
        """Deciding to steal is not the same as being able to.

        A stale lock in a directory this process may not write to (a goal dir
        created under another uid, a read-only mount) still stats fine and still
        looks steal-worthy, so the steal is re-decided on every pass. With an
        unconditional ``continue`` that is a tight CPU-pinning loop that never
        reaches the deadline — the same hot spin the staleness-stat branch
        already refuses. It has to converge on ``locked``.
        """
        mod = _mod()
        target = tmp_path / "queue.json"
        lock = tmp_path / "queue.lock"
        lock.write_text(f"{_reaped_pid()} deadbeef", encoding="utf-8")
        old = time.time() - (mod.LOCK_STALE_SECS + 30)
        os.utime(lock, (old, old))
        real_unlink = Path.unlink
        attempts = 0

        def refuse_unlink(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            nonlocal attempts
            if self.name == "queue.lock":
                attempts += 1
                raise PermissionError("read-only parent")
            return real_unlink(self, *a, **k)

        started = time.monotonic()
        with mock.patch.object(Path, "unlink", refuse_unlink):
            with pytest.raises(TimeoutError):
                with mod._Lock(target):
                    pass
        elapsed = time.monotonic() - started
        # Converging AT the budget is correct; the bug never converges at all, so
        # the bound only has to separate "terminated" from "pinned a core".
        assert (
            elapsed < 4 * mod.LOCK_WAIT_SECS
        ), f"took {elapsed:.1f}s — the deadline was not honoured"
        # The spin's real signature is unbounded retries inside the wait budget:
        # with the sleep honoured this is ~LOCK_WAIT_SECS/0.05 at the very most.
        assert attempts < 500, f"{attempts} unlink attempts — this is the hot spin"
        assert lock.exists(), "a lock it could not remove must be left alone"

    @_POSIX_LIVENESS_ONLY
    def test_a_stale_lock_from_a_dead_holder_is_stolen(self, tmp_path: Path) -> None:
        """A crash must not wedge a goal — but the holder has to actually be gone."""
        mod = _mod()
        target = tmp_path / "queue.json"
        lock = tmp_path / "queue.lock"
        lock.write_text(f"{_reaped_pid()} deadbeef", encoding="utf-8")
        old = time.time() - (mod.LOCK_STALE_SECS + 30)
        os.utime(lock, (old, old))
        with mod._Lock(target):
            pass  # acquired by stealing; no exception is the assertion

    @_POSIX_LIVENESS_ONLY
    def test_a_stale_lock_whose_holder_still_runs_is_not_stolen(self, tmp_path: Path) -> None:
        """The lost-update defect: age alone meant a slow-but-live writer was robbed.

        Both processes then published and the slower ``os.replace`` silently
        dropped the faster one's update. Being slower than ``LOCK_STALE_SECS`` — a
        paged-out interpreter, a contended filesystem — is not being dead.
        """
        mod = _mod()
        mod.LOCK_WAIT_SECS = 0.2
        target = tmp_path / "queue.json"
        lock = tmp_path / "queue.lock"
        # This very process is the holder, so liveness is not in question.
        lock.write_text(f"{os.getpid()} stillhere", encoding="utf-8")
        old = time.time() - (mod.LOCK_STALE_SECS + 30)
        os.utime(lock, (old, old))
        with pytest.raises(TimeoutError):
            with mod._Lock(target):
                pass
        assert lock.exists(), "a live holder's lock must survive"

    def test_a_holder_that_cannot_be_judged_is_stolen_only_past_the_backstop(
        self, tmp_path: Path
    ) -> None:
        """Unparseable holder: not treated as dead, but not wedged forever either.

        Portable on purpose — this is the only steal path Windows has, since it
        offers no non-destructive liveness probe.
        """
        mod = _mod()
        mod.LOCK_WAIT_SECS = 0.2
        target = tmp_path / "queue.json"
        lock = tmp_path / "queue.lock"
        lock.write_text("not-a-pid", encoding="utf-8")

        stale = time.time() - (mod.LOCK_STALE_SECS + 30)
        os.utime(lock, (stale, stale))
        with pytest.raises(TimeoutError):
            with mod._Lock(target):
                pass

        abandoned = time.time() - (mod.LOCK_ABANDON_SECS + 30)
        os.utime(lock, (abandoned, abandoned))
        with mod._Lock(target):
            pass  # past the backstop it is stolen; no exception is the assertion

    def test_a_stolen_lock_makes_the_write_refuse_instead_of_clobbering(
        self, tmp_path: Path
    ) -> None:
        """Fencing is what makes losing the race non-destructive rather than silent.

        Gating the steal makes a wrong steal rare; it cannot make it impossible.
        So the publish itself re-checks ownership: the thief has already written a
        NEWER record, and replacing it with our pre-steal view is the lost update.
        """
        mod = _mod()
        target = tmp_path / "queue.json"
        target.write_text('{"version": 1, "inbox": [], "dispatch": {}}', encoding="utf-8")
        newer = '{"version": 1, "inbox": [], "dispatch": {"item-1": {}}}'

        with mod._Lock(target) as lock:
            assert lock.still_held()
            # Another writer takes the lock and publishes while we hold our view.
            (tmp_path / "queue.lock").write_text("99999 thief", encoding="utf-8")
            target.write_text(newer, encoding="utf-8")
            assert lock.still_held() is False
            with pytest.raises(mod._LockLost):
                mod._write(target, {"version": 1, "inbox": [], "dispatch": {}}, fence=lock)

        # The thief's record stands, and its lock was not deleted on our way out.
        assert target.read_text(encoding="utf-8") == newer
        assert (tmp_path / "queue.lock").read_text(encoding="utf-8") == "99999 thief"

    def test_the_fence_is_rechecked_after_the_flush_not_only_on_entry(self, tmp_path: Path) -> None:
        """An entry-only check proves the lock was ours before the slow part.

        ``os.fsync`` waits on the device, and that is exactly the window in which a
        writer looks stalled and gets its lock stolen. Checking only on entry let a
        writer pass the check, lose the lock during the flush, watch the thief
        publish, and then ``os.replace`` its own pre-steal view over the newer
        record — the lost update the fence exists to stop, reached by being slow
        between the check and the publish. So the steal happens DURING the fsync
        here, which is the only placement that distinguishes one check from two.
        """
        mod = _mod()
        target = tmp_path / "queue.json"
        newer = '{"version": 1, "inbox": [{"id": "thiefs"}], "dispatch": {}}'
        real_fsync = os.fsync

        with mod._Lock(target) as lock:
            target.write_text(newer, encoding="utf-8")

            def steal_then_fsync(fd):  # noqa: ANN001, ANN202
                # The lock is taken while we are inside the flush, so the entry
                # check has already passed.
                (tmp_path / "queue.lock").write_text("99999 thief", encoding="utf-8")
                return real_fsync(fd)

            with mock.patch.object(os, "fsync", steal_then_fsync):
                with pytest.raises(mod._LockLost):
                    mod._write(target, {"version": 1, "inbox": [], "dispatch": {}}, fence=lock)

        assert target.read_text(encoding="utf-8") == newer, "the newer record was clobbered"
        # And the fully-written temp file is dropped rather than left to be mistaken
        # for state or picked up by a later replace.
        assert not list(tmp_path.glob("queue.tmp-*"))

    @pytest.mark.skipif(
        sys.platform == "win32", reason="a directory is not openable on Windows; sync is skipped"
    )
    def test_the_parent_directory_is_synced_so_the_rename_survives_power_loss(
        self, tmp_path: Path
    ) -> None:
        """Flushing the file commits its CONTENTS; the rename is separate metadata.

        Without a directory sync a power cut just after a successful ``enqueue`` can
        come back with the message gone even though the call returned ok — and an
        acknowledged message the user typed is the thing this script exists not to
        lose. Asserted by watching for an fsync on a DIRECTORY fd, because the file
        fsync was already there and would make a plain call-count assertion pass
        against the bug.
        """
        mod = _mod()
        target = tmp_path / "queue.json"
        real_fsync = os.fsync
        synced_dirs = []

        def recording_fsync(fd):  # noqa: ANN001, ANN202
            if os.fstat(fd).st_mode & 0o170000 == 0o040000:  # S_IFDIR
                synced_dirs.append(fd)
            return real_fsync(fd)

        with mock.patch.object(os, "fsync", recording_fsync):
            mod._write(target, {"version": 1, "inbox": [], "dispatch": {}})

        assert synced_dirs, "the rename was never committed — only the bytes it points at"

    @pytest.mark.parametrize("failure", [OSError("cannot open a directory"), None])
    def test_a_directory_that_cannot_be_synced_does_not_fail_the_write(
        self, tmp_path: Path, failure: Exception | None
    ) -> None:
        """Best-effort is the point: by then the DATA is already committed.

        Some filesystems refuse to fsync a directory, and Windows cannot open one at
        all — which is why this upgrade is attempted rather than required. Turning
        either into a failed write would refuse an operation whose bytes are durable,
        which is worse than the weaker durability it was reaching for. Parametrized
        over both refusal points: the directory that will not open, and the one that
        opens and will not sync.
        """
        mod = _mod()
        target = tmp_path / "queue.json"
        real_open, real_fsync = os.open, os.fsync

        def refusing_open(path, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
            if Path(path).is_dir():
                raise OSError("cannot open a directory here")
            return real_open(path, *a, **k)

        def refusing_fsync(fd):  # noqa: ANN001, ANN202
            if os.fstat(fd).st_mode & 0o170000 == 0o040000:  # S_IFDIR
                raise OSError("cannot sync a directory here")
            return real_fsync(fd)

        patch = (
            mock.patch.object(os, "open", refusing_open)
            if failure is not None
            else mock.patch.object(os, "fsync", refusing_fsync)
        )
        with patch:
            mod._write(target, {"version": 1, "inbox": [{"id": "must-land"}], "dispatch": {}})

        assert json.loads(target.read_text(encoding="utf-8"))["inbox"] == [{"id": "must-land"}]


class TestStatusNeverDestroysWhatItOnlyMeantToRead:
    """``status`` writes nothing, and used to be able to lose a write anyway.

    ``_read`` is not a pure read: on a malformed record it MOVES the file aside. Run
    without the lock, that made the read-only mode capable of destroying a
    concurrent writer's valid state — status reads old corruption, ``enqueue`` takes
    the lock and publishes a good record over it, and status then renames THAT file
    to the sidecar.
    """

    def test_status_takes_the_lock_before_it_reads(self, tmp_path: Path) -> None:
        """Held by somebody else, ``status`` reports `locked` rather than reading.

        That refusal IS the fix: a status that can be made to wait is a status whose
        quarantine decision applies to the file it was made about. Driven through the
        subprocess so the structured refusal, not an exception, is what is asserted.
        """
        goal_dir = tmp_path / "conductor" / "g"
        goal_dir.mkdir(parents=True)
        (goal_dir / "queue.json").write_text(
            '{"version": 1, "inbox": [], "dispatch": {}}', encoding="utf-8"
        )
        # A live holder: this process, so the liveness probe says "still running"
        # and the lock is not stolen out from under the assertion.
        (goal_dir / "queue.lock").write_text(f"{os.getpid()} someone-else", encoding="utf-8")

        out = _run("status", {"goal": "g"}, tmp_path)
        assert out["ok"] is False
        assert out["error"]["code"] == "locked"

    def test_the_quarantine_decision_is_made_under_the_lock(self, tmp_path: Path) -> None:
        """The destructive step is the rename, so THAT is what has to be excluded.

        Asserted as the invariant rather than by racing a real writer: at the moment
        ``_quarantine`` renames the file, the goal's lock must be held by us. That is
        precisely what makes the interleaving impossible — a lock-respecting
        ``enqueue`` cannot publish between the read and the rename, so it cannot be
        the file that gets moved aside. Unlocked, no lock file exists at that point
        at all, which is the shape of the bug.
        """
        mod = _mod()
        mod._data_home = lambda: tmp_path
        goal_dir = tmp_path / "conductor" / "g"
        goal_dir.mkdir(parents=True)
        state = goal_dir / "queue.json"
        state.write_text('{"version": 1, "inbox": [null], "dispatch": {}}', encoding="utf-8")
        lock = goal_dir / "queue.lock"
        real_quarantine = mod._quarantine
        held_at_rename = []

        def recording_quarantine(path):  # noqa: ANN001, ANN202
            held_at_rename.append(
                lock.exists()
                and lock.read_text(encoding="utf-8").strip().startswith(str(os.getpid()))
            )
            return real_quarantine(path)

        with mock.patch.object(mod, "_quarantine", recording_quarantine):
            got = mod.mode_status({"goal": "g"})

        assert got["ok"] is True
        assert held_at_rename == [True], "status quarantined without holding the goal's lock"
        # And the lock is not left behind for the next caller to time out on.
        assert not lock.exists()


class TestAPartialWriteIsNeverPublished:
    """A short write used to be accepted and then replaced over a good record.

    ``os.write`` may consume fewer bytes than it is given and does not raise when
    it does — on a full device that is exactly what happens. Publishing anyway put
    truncated JSON where the queue was, and the truncation then read back as
    corrupt, so every parked message was gone.
    """

    def test_a_short_write_refuses_and_leaves_the_previous_record(self, tmp_path: Path) -> None:
        mod = _mod()
        target = tmp_path / "queue.json"
        good = '{"version": 1, "inbox": [{"id": "keepme"}], "dispatch": {}}'
        target.write_text(good, encoding="utf-8")

        with mock.patch.object(os, "write", _stalling_write()):
            with pytest.raises(OSError):
                mod._write(target, {"version": 1, "inbox": [], "dispatch": {}})

        assert target.read_text(encoding="utf-8") == good
        # And no half-written temp file left beside it to be mistaken for state.
        assert not list(tmp_path.glob("queue.tmp-*"))

    def test_a_full_disk_is_reported_as_a_refusal_not_a_crash(self, tmp_path: Path) -> None:
        """The conductor has to be able to read WHY, and that the record survived.

        Driven through ``main`` rather than the mode function: the structured
        refusal is what the conductor actually reads on stdout, and a mode raising
        into a traceback would be indistinguishable from a bug.
        """
        _run("enqueue", {"goal": "g", "text": "must survive"}, tmp_path)
        mod = _mod()
        mod._data_home = lambda: tmp_path  # the subprocess env does not reach in-process

        out = io.StringIO()
        with (
            mock.patch.object(mod.sys, "argv", ["dispatch_queue.py", "enqueue"]),
            mock.patch.object(
                mod.sys, "stdin", io.StringIO(json.dumps({"goal": "g", "text": "second"}))
            ),
            mock.patch.object(mod.sys, "stdout", out),
            mock.patch.object(os, "write", _stalling_write()),
        ):
            assert mod.main() == 0

        got = json.loads(out.getvalue())
        assert got["ok"] is False
        assert got["error"]["code"] == "state_write_failed"
        assert _run("status", {"goal": "g"}, tmp_path)["inbox"][0]["text"] == "must survive"
