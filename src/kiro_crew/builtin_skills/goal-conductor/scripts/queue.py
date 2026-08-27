#!/usr/bin/env python3
"""Durable inbox + dispatch record - the conductor's crash-surviving bookkeeping.

Two things the conductor previously held only in a PROMPT, and therefore only in
context, which is exactly what compaction and a lost turn take away:

1. **A mid-flight user message.** The user can message the conductor at any time,
   and the skill's rule is to apply a goal change at the ROUND BOUNDARY. Between
   arrival and that boundary the message lived nowhere but the conversation, so a
   compaction in between silently dropped a steering instruction.
2. **Whether an item was already dispatched.** Dispatch is two calls -
   ``session_ctl(op="create")`` then ``session_ctl(op="send")`` - and the skill
   ordered them with prose ("send the seed BEFORE recording the ledger row"). A
   turn lost between the two leaves a session with no seed, and the next patrol
   cycle cannot tell that from a session that is merely quiet.

This script owns both as files on disk, so a fresh turn can read what the last
one did.

WHAT THIS DOES NOT GIVE YOU, said plainly: dispatch here is
detectable-and-convergent, NOT atomic. ``dispatch_begin`` hands back the same id
for an item already begun, so a second attempt is VISIBLE and converges on one
session instead of opening a second - but the ``create``/``send`` calls are MCP
tool calls the model makes, and this script cannot make them. Only moving dispatch
into gateway-side code makes a duplicate impossible; that is deliberately out of
scope (see
``docs/request-for-change/rfc-conductor-op-tool-and-script-boundary.md``).

No identity, by construction. This is the conductor's own bookkeeping, not an
authorization boundary: nothing here reads a session key, a gateway credential, or
the SEL trust root, and nothing here decides what any caller may reach. That is
what makes it safe as a plain script - a script runs as a child of
``execute_bash``, where the gateway injects no verifiable identity, so any file
here that gated access would be gating on an assertion.

Usage:
    python3 queue.py {enqueue|claim|done|release|dispatch_begin|dispatch_sent|status} < in.json

Every mode reads one JSON document on stdin and writes one on stdout. Domain
problems (an unknown goal id shape, a full inbox, an oversize message) are
structured ``{"ok": false, "error": {...}}`` results, never crashes - the
conductor must be able to read WHY and decide. Exit code 0 means the operation
ran; 2 means stdin was not the JSON the mode needs, or the mode name is unknown.

Every response carries ``state_path``. That is not decoration: the state root is
resolved from the environment (see :func:`_data_home`), so a conductor that
records the path in its ledger can tell "the queue is empty" from "the queue is
somewhere else".

Modes:

``enqueue``  park a mid-flight message until the next round boundary.
    stdin:  {"goal": "<id>", "text": "..."}
    stdout: {"ok": true, "id": "...", "pending": 2, "state_path": "..."}

``claim``  take every pending message (call this AT the round boundary).
    stdin:  {"goal": "<id>", "stale_secs": 900}
    stdout: {"ok": true, "claimed": [{"id","text","enqueued_at"}], "pending_left": 0}
    A claim marks each entry ``claimed`` with a timestamp rather than deleting it,
    so a turn that dies after claiming does not take the messages with it: a later
    claim re-serves anything claimed longer ago than ``stale_secs``. Deleting on
    read would be simpler and would lose exactly the message this script exists to
    keep.

``done``  drop messages you have applied.
    stdin:  {"goal": "<id>", "ids": ["..."]}
    stdout: {"ok": true, "removed": 1, "pending": 0}

``release``  push claimed messages back to pending without applying them.
    stdin:  {"goal": "<id>", "ids": ["..."]}

``dispatch_begin``  pre-assign (or recover) a work item's dispatch id.
    stdin:  {"goal": "<id>", "item": "item-1"}
    stdout: {"ok": true, "dispatch_id": "...", "state": "begun", "attempts": 1,
             "replay": false}
    ``replay: true`` with ``state: "begun"`` is the signal that matters: a prior
    attempt began this item and never recorded a seed, so the session it created
    (if it created one) has no seed. Read it before creating a second one.

``dispatch_sent``  record that the seed landed.
    stdin:  {"goal": "<id>", "item": "item-1", "session": "dashboard:slot-3"}

``status``  the whole record, for a patrol cycle.
    stdin:  {"goal": "<id>"}
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

#: A goal id names a DIRECTORY, so it is validated as a single safe segment
#: rather than sanitized into one. Sanitizing maps two different goals onto one
#: name (``a/b`` and ``a_b``), which silently merges two goals' queues - the
#: failure would look like the conductor reading someone else's messages.
_GOAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Inbox cap. Full REFUSES rather than dropping the oldest: every entry here is a
#: message the user typed, and silently discarding one is the failure this script
#: was built to prevent. The conductor's answer to a full inbox is to run a round
#: and drain it, which is a real answer.
MAX_INBOX = 200

#: Per-message cap. Also a refusal rather than a truncation - half a steering
#: instruction can invert its meaning ("do not ship" -> "do").
MAX_TEXT = 20_000

#: Dispatch records per goal. A goal with more live items than this is not a
#: capacity problem to paper over; the skill caps a round at two or three items.
MAX_ITEMS = 200

#: How long a lock may be held before another process may steal it. A crashed
#: holder must not wedge a goal forever, and every critical section here is a
#: read-modify-write of one small file.
LOCK_STALE_SECS = 60.0
LOCK_WAIT_SECS = 5.0

_TERMINAL_DISPATCH = ("sent",)


def _error(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    out.update(extra)
    return out


def _data_home() -> Path:
    """Where the state lives.

    Preferred: the package's own resolver, so this agrees with the gateway by
    construction. It is a function-local import because this script also runs
    where ``kiro_crew`` is not importable - the skill tree is synced out of the
    package, and a module-scope import would make the script unusable there.

    Fallback: ``KIROCREW_HOME`` when set, else ``~/.kiro/crew``. That fallback
    deliberately does NOT re-implement the resolver's override-validity predicate
    (which rejects an override naming a system directory): a second copy of that
    rule is a second thing to drift, and the consequence of disagreeing here is
    not an access decision - it is a queue file under a different root. Which is
    why every response reports ``state_path``: a conductor that records it can
    see the disagreement instead of reading an empty queue as "no messages".
    """
    try:
        from kiro_crew.config.paths import data_home  # noqa: PLC0415

        return Path(data_home())
    except Exception:
        override = os.environ.get("KIROCREW_HOME", "").strip()
        if override:
            return Path(override).expanduser()
        return Path.home() / ".kiro" / "crew"


def _state_path(goal: str) -> Path:
    return _data_home() / "conductor" / goal / "queue.json"


def _now() -> float:
    return time.time()


def _blank(goal: str) -> Dict[str, Any]:
    return {"version": 1, "goal": goal, "inbox": [], "dispatch": {}}


class _Unreadable(Exception):
    """The record exists but could not be read THIS TIME.

    Distinct from absent on purpose, and the distinction is load-bearing: an
    earlier revision returned a blank record for any ``OSError``, and every mode
    then WROTE that blank over the file -- destroying the parked messages this
    script exists to keep, on a transient read error. Absent may read as blank;
    unreadable must refuse and leave the bytes alone.
    """


def _read(path: Path, goal: str) -> Dict[str, Any]:
    """Load the record. Absent -> blank; unparseable -> quarantine; unreadable -> raise.

    A truncated or hand-edited file must not make every later mode fail -- the
    conductor would have no way back to a working queue -- so a file whose BYTES
    read fine but are not the expected JSON is moved aside (``.corrupt-<ts>``,
    still there to look at) and treated as absent. A file we cannot read at all is
    a different thing and is not overwritten.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _blank(goal)
    except OSError as exc:
        raise _Unreadable(str(exc)) from exc
    try:
        data = json.loads(raw)
    except Exception:
        try:
            path.replace(path.with_suffix(f".corrupt-{int(_now())}"))
        except OSError:
            pass
        return _blank(goal)
    if not isinstance(data, dict) or not isinstance(data.get("inbox"), list):
        return _blank(goal)
    if not isinstance(data.get("dispatch"), dict):
        data["dispatch"] = {}
    data["goal"] = goal
    return data


def _write(path: Path, data: Dict[str, Any]) -> None:
    """Atomic replace + fsync, so a crash mid-write cannot leave half a record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


class _Lock:
    """Cross-process lock via ``O_EXCL``, with a documented steal after staleness.

    ``fcntl.flock`` would be less code and is not portable to Windows, which this
    project supports. A lock file plus a staleness steal is portable and, for a
    critical section that is one small read-modify-write, sufficient.
    """

    def __init__(self, target: Path) -> None:
        self.path = target.with_suffix(".lock")
        self.held = False

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = _now() + LOCK_WAIT_SECS
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = _now() - self.path.stat().st_mtime
                except FileNotFoundError:
                    # The holder released it between our open and this stat, so
                    # the next open is the retry. Still budget-checked below
                    # rather than `continue`d: an unconditional continue here is
                    # a hot spin with no exit, which is worse than answering
                    # `locked`.
                    age = 0.0
                except OSError:
                    # Cannot even judge staleness. Do NOT steal on an unknown
                    # error -- stealing a lock we cannot reason about is how two
                    # writers land on one file -- and do not spin: fall through
                    # to the deadline.
                    age = 0.0
                if age > LOCK_STALE_SECS:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if _now() >= deadline:
                    raise TimeoutError(
                        f"{self.path} still held after {LOCK_WAIT_SECS:.0f}s " f"(age {age:.0f}s)"
                    )
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def _goal_of(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any] | None]:
    goal = payload.get("goal")
    if not isinstance(goal, str) or not _GOAL_RE.match(goal):
        return "", _error(
            "bad_goal",
            "goal must be 1-64 chars of letters, digits, '.', '_' or '-', starting "
            "alphanumeric. It names a directory, so it is validated rather than "
            "sanitized: sanitizing would map two goals onto one queue.",
        )
    return goal, None


def _ids_of(payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any] | None]:
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        return [], _error("bad_ids", "ids must be a non-empty list of strings")
    if not ids:
        return [], _error("bad_ids", "ids must be a non-empty list of strings")
    return ids, None


def _with_state(goal: str, data: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    out["state_path"] = str(_state_path(goal))
    out["pending"] = sum(1 for e in data["inbox"] if e.get("state") == "pending")
    return out


def mode_enqueue(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("bad_text", "text must be a non-empty string")
    if len(text) > MAX_TEXT:
        return _error(
            "text_too_long",
            f"text is {len(text)} chars; the cap is {MAX_TEXT}. Refused rather than "
            "truncated: half a steering instruction can invert its meaning.",
        )
    path = _state_path(goal)
    with _Lock(path):
        data = _read(path, goal)
        if len(data["inbox"]) >= MAX_INBOX:
            return _with_state(
                goal,
                data,
                _error(
                    "inbox_full",
                    f"the inbox holds {len(data['inbox'])} entries and caps at "
                    f"{MAX_INBOX}. Refused rather than dropping the oldest: every "
                    "entry is a message the user typed. Run a round and drain it.",
                ),
            )
        entry = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "enqueued_at": _now(),
            "state": "pending",
        }
        data["inbox"].append(entry)
        _write(path, data)
    return _with_state(goal, data, {"ok": True, "id": entry["id"]})


def mode_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    stale = payload.get("stale_secs", 900)
    if not isinstance(stale, (int, float)) or isinstance(stale, bool) or stale < 0:
        return _error("bad_stale_secs", "stale_secs must be a non-negative number")
    path = _state_path(goal)
    now = _now()
    with _Lock(path):
        data = _read(path, goal)
        claimed: List[Dict[str, Any]] = []
        for e in data["inbox"]:
            if e.get("state") == "pending":
                pass
            elif e.get("state") == "claimed" and now - float(e.get("claimed_at") or 0) > stale:
                # Re-served, not lost: the turn that claimed it died before
                # calling done, and the message is still the user's.
                e["reclaimed"] = int(e.get("reclaimed", 0)) + 1
            else:
                continue
            e["state"] = "claimed"
            e["claimed_at"] = now
            claimed.append(
                {
                    "id": e["id"],
                    "text": e["text"],
                    "enqueued_at": e.get("enqueued_at"),
                    "reclaimed": int(e.get("reclaimed", 0)),
                }
            )
        _write(path, data)
    return _with_state(goal, data, {"ok": True, "claimed": claimed})


def mode_done(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    ids, err = _ids_of(payload)
    if err:
        return err
    path = _state_path(goal)
    wanted = set(ids)
    with _Lock(path):
        data = _read(path, goal)
        before = len(data["inbox"])
        data["inbox"] = [e for e in data["inbox"] if e.get("id") not in wanted]
        removed = before - len(data["inbox"])
        _write(path, data)
    # An id that was not there is NOT an error: a retried done must be safe, or a
    # conductor that lost the response would have to guess whether to call again.
    return _with_state(goal, data, {"ok": True, "removed": removed})


def mode_release(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    ids, err = _ids_of(payload)
    if err:
        return err
    path = _state_path(goal)
    wanted = set(ids)
    with _Lock(path):
        data = _read(path, goal)
        released = 0
        for e in data["inbox"]:
            if e.get("id") in wanted and e.get("state") == "claimed":
                e["state"] = "pending"
                e.pop("claimed_at", None)
                released += 1
        _write(path, data)
    return _with_state(goal, data, {"ok": True, "released": released})


def _item_of(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any] | None]:
    item = payload.get("item")
    if not isinstance(item, str) or not item.strip() or len(item) > 128:
        return "", _error("bad_item", "item must be a non-empty string of at most 128 chars")
    return item, None


def mode_dispatch_begin(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    item, err = _item_of(payload)
    if err:
        return err
    path = _state_path(goal)
    with _Lock(path):
        data = _read(path, goal)
        rec = data["dispatch"].get(item)
        if rec is None:
            if len(data["dispatch"]) >= MAX_ITEMS:
                return _with_state(
                    goal,
                    data,
                    _error(
                        "too_many_items",
                        f"{len(data['dispatch'])} dispatch records and the cap is "
                        f"{MAX_ITEMS}. A round is two or three items; this is a "
                        "goal that was never closed out, not a cap to raise.",
                    ),
                )
            rec = {
                "dispatch_id": uuid.uuid4().hex[:12],
                "state": "begun",
                "attempts": 1,
                "begun_at": _now(),
            }
            data["dispatch"][item] = rec
            replay = False
        else:
            # The SAME id, deliberately. Handing out a fresh one per attempt is
            # what opens a second session for one item; returning the first makes
            # the retry converge and makes the replay visible.
            rec["attempts"] = int(rec.get("attempts", 1)) + 1
            rec["last_attempt_at"] = _now()
            replay = True
        _write(path, data)
    out = {
        "ok": True,
        "dispatch_id": rec["dispatch_id"],
        "state": rec["state"],
        "attempts": rec["attempts"],
        "replay": replay,
    }
    if replay and rec["state"] not in _TERMINAL_DISPATCH:
        out["warning"] = (
            "this item was begun and never recorded a seed, so a session may exist "
            "with no seed. Read it before creating another one."
        )
    if rec.get("session"):
        out["session"] = rec["session"]
    return _with_state(goal, data, out)


def mode_dispatch_sent(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    item, err = _item_of(payload)
    if err:
        return err
    session = payload.get("session")
    if not isinstance(session, str) or not session.strip():
        return _error("bad_session", "session must be a non-empty string")
    path = _state_path(goal)
    with _Lock(path):
        data = _read(path, goal)
        rec = data["dispatch"].get(item)
        if rec is None:
            # Recording a send for an item never begun means the caller skipped
            # dispatch_begin, which is the whole guard. Accepted and FLAGGED
            # rather than refused: the send already happened, and refusing would
            # leave the durable record less true than the world.
            rec = {
                "dispatch_id": uuid.uuid4().hex[:12],
                "attempts": 1,
                "begun_at": _now(),
                "began_out_of_band": True,
            }
            data["dispatch"][item] = rec
        rec["state"] = "sent"
        rec["session"] = session
        rec["sent_at"] = _now()
        _write(path, data)
    out: Dict[str, Any] = {"ok": True, "state": "sent", "dispatch_id": rec["dispatch_id"]}
    if rec.get("began_out_of_band"):
        out["warning"] = "no dispatch_begin preceded this send; the replay guard was skipped"
    return _with_state(goal, data, out)


def mode_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal, err = _goal_of(payload)
    if err:
        return err
    path = _state_path(goal)
    data = _read(path, goal)
    unsent = sorted(
        item for item, rec in data["dispatch"].items() if rec.get("state") not in _TERMINAL_DISPATCH
    )
    out = {
        "ok": True,
        "inbox": data["inbox"],
        "dispatch": data["dispatch"],
        "unsent_items": unsent,
        "exists": path.exists(),
    }
    return _with_state(goal, data, out)


_MODES = {
    "enqueue": mode_enqueue,
    "claim": mode_claim,
    "done": mode_done,
    "release": mode_release,
    "dispatch_begin": mode_dispatch_begin,
    "dispatch_sent": mode_dispatch_sent,
    "status": mode_status,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _MODES:
        # stdout, matching ledger_entry.py and accept_eval.py: the conductor reads
        # one stream, and a wrong mode name is the likeliest invocation error.
        print(json.dumps({"error": f"usage: queue.py {{{'|'.join(sorted(_MODES))}}} < input.json"}))
        return 2
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"error": "stdin must be a JSON object"}))
        return 2
    if not isinstance(payload, dict):
        # Explicit, not an assert: asserts vanish under `python -O`, and a JSON
        # array would then crash inside a mode handler instead of returning the
        # documented structured exit-2.
        print(json.dumps({"error": "stdin must be a JSON object"}))
        return 2
    try:
        result = _MODES[sys.argv[1]](payload)
    except TimeoutError as exc:
        # A held lock is a real, retryable condition, not a bug: say so in the
        # same structured shape every other refusal uses.
        result = _error("locked", f"another writer holds the goal's lock: {exc}")
    except _Unreadable as exc:
        # Deliberately NOT recovered into a blank record: the state file is still
        # on disk with the parked messages in it, and answering "empty" would
        # invite the caller to write over them.
        result = _error(
            "state_unreadable",
            f"the goal's state file exists but could not be read ({exc}); it was "
            "left untouched. Fix the read error rather than re-running, or the "
            "messages parked in it are what gets overwritten.",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
