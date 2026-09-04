#!/usr/bin/env python3
"""Durable inbox + dispatch record - the conductor's crash-surviving bookkeeping.

Two things the conductor previously held only in a PROMPT, and therefore only in
context, which is exactly what compaction and a lost turn take away:

1. **A mid-flight user message.** The user can message the conductor at any time,
   and the skill's rule is to apply a goal change at the ROUND BOUNDARY. Between
   arrival and that boundary the message lived nowhere but the conversation, so a
   compaction in between silently dropped a steering instruction.
2. **Whether an item was already dispatched.** Dispatch is two calls -
   ``session_create`` then ``session_send`` - and the skill ordered them with
   prose ("send the seed BEFORE recording the ledger row"). A turn lost between
   the two leaves a session with no seed, and the next patrol cycle cannot tell
   that from a session that is merely quiet.

This script owns both as files on disk, so a fresh turn can read what the last
one did.

WHAT THIS DOES NOT GIVE YOU, said plainly: dispatch here is
detectable-and-convergent, NOT atomic. ``dispatch_begin`` hands back the same id
for an item already begun, so a second attempt is VISIBLE and converges on one
session instead of opening a second - but ``session_create`` and ``session_send``
are MCP tool calls the model makes, and this script cannot make them. Only moving
dispatch into gateway-side code makes a duplicate impossible; that is deliberately
out of scope here.

No identity, by construction. This is the conductor's own bookkeeping, not an
authorization boundary: nothing here reads a session key, a gateway credential, or
the SEL trust root, and nothing here decides what any caller may reach. That is
what makes it safe as a plain script - a script runs as a child of
``execute_bash``, where the gateway injects no verifiable identity, so any file
here that gated access would be gating on an assertion.

Usage:
    python3 dispatch_queue.py {enqueue|claim|done|release|dispatch_begin|dispatch_sent|status} < in.json

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
    stdout: {"ok": true, "claimed": [{"id","text","enqueued_at"}], "pending": 0}
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

#: How long a lock may be held before a holder we can PROVE is gone loses it. A
#: crashed holder must not wedge a goal forever, and every critical section here
#: is a read-modify-write of one small file. Age alone is deliberately NOT enough
#: to steal: a writer merely slower than this is still live, and robbing it is how
#: two processes end up publishing over each other. See :meth:`_Lock._may_steal`.
LOCK_STALE_SECS = 60.0

#: The backstop for a lock whose holder cannot be judged dead - an unparseable
#: pid, or any platform without a non-destructive liveness probe (Windows: see
#: :func:`_holder_is_alive`). Far longer than ``LOCK_STALE_SECS`` because it is
#: the window in which a live-but-slow writer would be robbed, and short enough
#: that a crash on such a platform still frees the goal within one sitting.
LOCK_ABANDON_SECS = 900.0

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


def _quarantine(path: Path) -> None:
    """Move an unusable record aside, keeping the bytes on disk to look at.

    Best-effort by design: failing to rename must not make the mode fail, because
    the alternative is a goal whose queue can never be used again. The rename is
    what makes discarding a record RECOVERABLE, so every discard path goes through
    here -- an unparseable file and a structurally invalid one are the same class
    of problem and get the same treatment.
    """
    try:
        path.replace(path.with_suffix(f".corrupt-{int(_now())}"))
    except OSError:
        pass


def _read(path: Path, goal: str) -> Dict[str, Any]:
    """Load the record. Absent -> blank; unusable -> quarantine; unreadable -> raise.

    A truncated or hand-edited file must not make every later mode fail -- the
    conductor would have no way back to a working queue -- so a file whose BYTES
    read fine but are not a record this script can use is moved aside
    (``.corrupt-<ts>``, still there to look at) and treated as absent. A file we
    cannot read at all is a different thing and is not overwritten.

    "Not a record this script can use" covers BOTH unparseable bytes and valid
    JSON of the wrong shape, and the second is the one worth stating: a file whose
    ``inbox`` is an object or whose ``dispatch`` is a list still holds the
    conductor's parked messages and dispatch ids in readable form. Returning a
    blank for it without the sidecar meant the very next mutation wrote the blank
    back and those records were gone with no copy anywhere -- the same data loss
    the unparseable path already quarantined against, reached through a different
    door.

    The shape check goes one level DEEP, and that is not belt-and-braces. Checking
    only the containers accepts ``{"inbox": [null]}``: the container is a list, so
    every mode then reaches for ``entry.get(...)`` on a non-mapping and dies with an
    uncaught ``AttributeError``. That is the exact failure this function exists to
    prevent -- a hand-edited or truncated file making every later mode fail with a
    traceback instead of the structured refusal the conductor can act on -- so a
    malformed ENTRY is quarantined on the same path as a malformed container.
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
        _quarantine(path)
        return _blank(goal)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("inbox"), list)
        or not isinstance(data.get("dispatch"), dict)
        or not all(isinstance(entry, dict) for entry in data["inbox"])
        or not all(isinstance(rec, dict) for rec in data["dispatch"].values())
    ):
        _quarantine(path)
        return _blank(goal)
    data["goal"] = goal
    return data


def _write_all(fd: int, payload: bytes, what: str) -> None:
    """Write every byte, or raise without having written a usable file.

    ``os.write`` is not obliged to consume the whole buffer and does NOT raise when
    it does not - on a full device it returns a short count. Every caller here
    needs all-or-nothing, so the loop lives in one place: a partial state record
    published over a good one destroys the queue, and a partial lock TOKEN is worse
    than no lock at all, because it can never be matched again and would make every
    later fenced write refuse.
    """
    written = 0
    while written < len(payload):
        n = os.write(fd, payload[written:])
        if n <= 0:
            # Not an OS exception, just no forward progress - which on a full
            # device is exactly what a short write looks like.
            raise OSError(
                f"short write to {what}: {written} of {len(payload)} bytes (out of space?)"
            )
        written += n


def _fsync_dir(directory: Path) -> None:
    """Commit the RENAME, not just the bytes it points at.

    ``os.fsync`` on the temp file makes its CONTENTS durable; the directory entry
    that ``os.replace`` rewrote is separate metadata and can still be lost. Without
    this, a power cut just after a successful ``enqueue`` can come back up with the
    message gone even though the call returned ok -- and a message the user typed
    that we acknowledged is precisely what this script exists not to lose.

    Best-effort by design. A directory is not openable on Windows, and some
    filesystems refuse to fsync one; neither is a reason to fail a write whose data
    is already committed, so the durability is upgraded where it can be and the
    write stands where it cannot.
    """
    if sys.platform == "win32":
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write(path: Path, data: Dict[str, Any], fence: "_Lock | None" = None) -> None:
    """Atomic replace + fsync, so a crash mid-write cannot leave half a record.

    Three things have to hold before the ``os.replace`` publishes, and each of them
    is a way this has already been got wrong:

    - **Every byte is written**, via :func:`_write_all`. Writing once and replacing
      anyway published truncated JSON over a complete record, and the truncation
      then read back as corrupt, so the queue was gone.
    - **The bytes are on the device.** ``os.fsync`` before the rename, so a crash
      cannot leave the rename durable and the contents not. :func:`_fsync_dir`
      after it, so the rename itself is durable too.
    - **We still hold the lock.** ``fence`` is the lock guarding this critical
      section; if it was stolen while we were slow, we are about to overwrite
      somebody else's newer state with our stale view. Refusing is the only
      correct move - see :class:`_Lock`.

    Nothing is published unless all three hold, so a failure here leaves the
    previous record exactly as it was and drops the temp file.

    The fence is checked TWICE, and the second one is the load-bearing one. An
    entry check only proves the lock was ours before the slow part; ``os.fsync``
    waits on the device, which is exactly the window in which a stalled writer gets
    its lock stolen. Checking only on entry meant a writer could be declared dead,
    have its lock taken, watch the thief publish, and then ``os.replace`` its own
    pre-steal view over the newer state -- the lost update the fence exists to stop,
    reached by being slow in between the check and the publish. The entry check
    stays because failing before writing anything is cheaper than failing after.
    """
    if fence is not None and not fence.still_held():
        raise _LockLost(f"{fence.path} was taken while this write was in progress")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, payload, str(tmp))
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    if fence is not None and not fence.still_held():
        try:
            tmp.unlink()
        except OSError:
            pass
        raise _LockLost(f"{fence.path} was taken while this write was being flushed")
    os.replace(str(tmp), str(path))
    _fsync_dir(path.parent)


class _LockLost(Exception):
    """We held the lock, and by the time we went to publish we no longer did.

    Raised instead of completing the write. The alternative - replacing the file
    anyway - is a lost update: the process that stole the lock has already written
    a NEWER record, and our view predates it.
    """


def _holder_is_alive(pid: int) -> bool | None:
    """Is the recorded lock holder still running? ``None`` means cannot tell.

    Three-valued on purpose, because "cannot tell" must not read as "dead". On
    POSIX ``os.kill(pid, 0)`` is the standard non-destructive probe. On Windows
    there is no such probe in the standard library -- ``os.kill`` there ignores the
    signal and TERMINATES the target -- so this returns ``None`` rather than doing
    something destructive to answer a bookkeeping question. ``EPERM`` means the pid
    exists and belongs to somebody else, which is alive for our purposes.
    """
    if not hasattr(os, "kill") or sys.platform == "win32":
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class _Lock:
    """Cross-process lock via ``O_EXCL``, stolen only from a holder that is gone.

    ``fcntl.flock`` would be less code and is not portable to Windows, which this
    project supports. A lock file is portable; the interesting part is when it may
    be TAKEN from someone, and that has two halves:

    - **Stealing is liveness-gated, not age-gated.** Age alone says the holder is
      slow, which is not the same as dead: a writer stalled past
      ``LOCK_STALE_SECS`` -- a paged-out interpreter, a contended filesystem -- was
      being robbed while still running, and both processes then published, so the
      slower one's ``os.replace`` silently dropped the faster one's update. So a
      steal now needs the holder proved gone (:func:`_holder_is_alive`), with
      ``LOCK_ABANDON_SECS`` as the backstop for a holder that cannot be judged at
      all. A crash still frees the goal; a slow writer keeps its lock.
    - **Ownership is fenced, so a wrong steal cannot corrupt.** The lock file
      carries a token unique to this acquisition, not just a pid, and
      :meth:`still_held` re-reads it. ``_write`` checks that immediately before
      publishing and refuses if the token changed, and ``__exit__`` only unlinks a
      lock file that is still ours -- deleting the thief's lock would hand the file
      to a third writer. Gating the steal makes the race rare; fencing is what
      makes losing it non-destructive, which is why both are here.
    """

    def __init__(self, target: Path) -> None:
        self.path = target.with_suffix(".lock")
        self.held = False
        # pid for the liveness probe, random half so that a recycled pid - or a
        # second acquisition by this same process - is still a DIFFERENT owner.
        self.token = f"{os.getpid()} {os.urandom(8).hex()}"

    def _may_steal(self, age: float) -> bool:
        """Only take the lock from a holder proved gone, or past the backstop."""
        if age > LOCK_ABANDON_SECS:
            return True
        if age <= LOCK_STALE_SECS:
            return False
        try:
            recorded = self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        try:
            pid = int(recorded.split()[0])
        except (ValueError, IndexError):
            # An unparseable holder cannot be probed, so it waits for the
            # backstop rather than being treated as dead.
            return False
        return _holder_is_alive(pid) is False

    def still_held(self) -> bool:
        """Is the lock file still the one we created? Cheap enough to call per write."""
        if not self.held:
            return False
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").strip() == self.token
        except OSError:
            # Gone or unreadable: either way it is not demonstrably ours.
            return False

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = _now() + LOCK_WAIT_SECS
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    # All-or-nothing like the state write, and for a sharper
                    # reason: a half-written token can never match again, so the
                    # lock would be held by nobody and every fenced write under it
                    # would refuse. Better to fail acquiring.
                    _write_all(fd, self.token.encode("utf-8"), str(self.path))
                except BaseException:
                    os.close(fd)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    raise
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
                if self._may_steal(age):
                    try:
                        self.path.unlink()
                    except OSError:
                        # The steal was decided but could not be carried out --
                        # a lock we may not remove (a goal dir owned by another
                        # uid, a read-only mount). Fall through to the deadline
                        # instead of retrying: `continue` here re-decides the
                        # same steal on the same statable-and-stale file every
                        # pass, with no sleep and no deadline check, which is
                        # the hot spin the FileNotFoundError branch above
                        # already refuses. An un-removable lock must converge
                        # on `locked`, not pin a core forever.
                        pass
                    else:
                        continue
                if _now() >= deadline:
                    raise TimeoutError(
                        f"{self.path} still held after {LOCK_WAIT_SECS:.0f}s " f"(age {age:.0f}s)"
                    )
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        # Only ever unlink a lock that is still ours: if it was stolen, the file
        # now belongs to another writer and removing it would let a third in.
        if self.still_held():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False


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
    with _Lock(path) as lock:
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
        _write(path, data, fence=lock)
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
    with _Lock(path) as lock:
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
        _write(path, data, fence=lock)
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
    with _Lock(path) as lock:
        data = _read(path, goal)
        before = len(data["inbox"])
        data["inbox"] = [e for e in data["inbox"] if e.get("id") not in wanted]
        removed = before - len(data["inbox"])
        _write(path, data, fence=lock)
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
    with _Lock(path) as lock:
        data = _read(path, goal)
        released = 0
        for e in data["inbox"]:
            if e.get("id") in wanted and e.get("state") == "claimed":
                e["state"] = "pending"
                e.pop("claimed_at", None)
                released += 1
        _write(path, data, fence=lock)
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
    with _Lock(path) as lock:
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
        _write(path, data, fence=lock)
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
    with _Lock(path) as lock:
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
        _write(path, data, fence=lock)
    out: Dict[str, Any] = {"ok": True, "state": "sent", "dispatch_id": rec["dispatch_id"]}
    if rec.get("began_out_of_band"):
        out["warning"] = "no dispatch_begin preceded this send; the replay guard was skipped"
    return _with_state(goal, data, out)


def mode_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Report the record. Takes the lock even though it writes nothing.

    ``status`` looks like a pure read, but :func:`_read` is not one: on a malformed
    file it MOVES it aside. Unlocked, that made the read-only mode capable of losing
    a write -- status reads old corruption, a concurrent ``enqueue`` takes the lock
    and publishes a valid record over it, and status then renames THAT file to the
    sidecar, so a message the user typed vanishes from the queue at the hands of a
    mode that only meant to look. Holding the lock across the read and the existence
    check is what makes the quarantine decision apply to the file it was made about.
    """
    goal, err = _goal_of(payload)
    if err:
        return err
    path = _state_path(goal)
    with _Lock(path):
        data = _read(path, goal)
        exists = path.exists()
    unsent = sorted(
        item for item, rec in data["dispatch"].items() if rec.get("state") not in _TERMINAL_DISPATCH
    )
    out = {
        "ok": True,
        "inbox": data["inbox"],
        "dispatch": data["dispatch"],
        "unsent_items": unsent,
        "exists": exists,
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
        print(
            json.dumps(
                {"error": f"usage: dispatch_queue.py {{{'|'.join(sorted(_MODES))}}} < input.json"}
            )
        )
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
    except _LockLost as exc:
        # Nothing was written. Retryable in the same way `locked` is, and named
        # separately so the conductor can tell "could not start" from "started,
        # then found the ground had moved".
        result = _error(
            "lock_lost",
            f"the goal's lock was taken while this call was mid-flight ({exc}); "
            "nothing was written, so the other writer's state stands. Re-run to "
            "apply this operation on top of it.",
        )
    except OSError as exc:
        # A write that could not complete - a full disk is the case that matters.
        # The previous record is intact because `_write` refuses to publish a
        # partial one, and saying so is the whole point of reporting it here.
        result = _error(
            "state_write_failed",
            f"the goal's state could not be written ({exc}); the previous record "
            "is intact and unmodified.",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
