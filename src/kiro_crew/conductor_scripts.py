"""Bridge to the goal-conductor's bundled skill scripts.

The acceptance evaluator (``accept_eval.py``) and ledger entry codec
(``ledger_entry.py``) are standalone scripts that live in the skill dir.
This module exposes their entry-point functions for the MCP tool handlers in
``mcp_dashboard.py``, loading them once by file location at import time.

The scripts remain the canonical implementation and continue to work as CLI
scripts; this module simply lets the MCP layer call into them without
subprocess overhead, and adds the in-band gate that the auto-approved MCP door
needs but the ``execute_bash`` door got from its own approval prompt (see
``_gate_accept``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Tuple

from kiro_crew import github_runner, hooks, pinned_fs
from kiro_crew.skills import _within_any

_SKILL_DIR = Path(__file__).parent / "builtin_skills" / "goal-conductor" / "scripts"


def _load_script(name: str, filename: str) -> ModuleType:
    """Load a skill script by file path without writing bytecode."""
    path = _SKILL_DIR / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


# Lazy-loaded module references (populated on first call).
_accept_eval: ModuleType | None = None
_ledger_entry: ModuleType | None = None


def _get_accept_eval() -> ModuleType:
    global _accept_eval
    if _accept_eval is None:
        _accept_eval = _load_script("_conductor_accept_eval", "accept_eval.py")
    return _accept_eval


def _get_ledger_entry() -> ModuleType:
    global _ledger_entry
    if _ledger_entry is None:
        _ledger_entry = _load_script("_conductor_ledger_entry", "ledger_entry.py")
    return _ledger_entry


#: The ``repo`` segment charset, imported rather than respelled: this is the
#: constraint ``github_runner`` already applies before either segment of an
#: ``owner/name`` pair reaches a subprocess, and a second copy could drift.
_SEGMENT_RE = github_runner._SEGMENT_RE


def _pinned_exists(path: str) -> bool:
    """Answer "is it there" through a pinned parent, never by re-walking the name.

    ``validate_file_path`` resolves the path, and between that resolution and the
    probe an ancestor DIRECTORY can be swapped for a symlink -- by the model
    itself, since the roots this door admits are exactly the trees the agent
    writes. A by-name ``Path.exists()`` then follows the replacement and answers
    about a path the gate refused, which is worse than no gate because it reports
    success. ``pinned_fs.pin_parent`` walks the resolved parent one ``openat`` per
    component with ``O_NOFOLLOW``, so a component that became a link after
    resolution is refused, and ``stat_at`` asks the question through the
    descriptor that walk produced. Descriptor-pinned is the repo's one mechanism
    for this (``pinned_fs`` exists because guarding each call site by name did
    not converge over two review rounds); this is a thin consumer of it.

    ``lstat`` rather than a following stat is correct here precisely because the
    path handed in is already canonical: its final component cannot be a link, so
    not following one loses nothing and keeps the answer about the NAME the gate
    approved.

    The platform guard lives HERE and not in ``_gate_accept`` on purpose. Refusing
    at the gate would make the platform the FIRST thing checked, so on a host that
    cannot pin every other diagnostic disappears: a malformed ``exists``, a
    relative path and a path outside the roots would all come back "cannot pin"
    and the author would learn nothing about the actual defect in the spec. Placed
    at the probe, the refusal happens exactly where the missing capability is, and
    every check before it still reports what it found on every platform.
    """
    if not pinned_fs.supports_pinned_walk():
        # ``pinned_fs`` deliberately never substitutes a weaker mechanism -- it
        # reports that it cannot pin and lets the caller decide. On a door with no
        # approval prompt in front of it the only honest decision is to refuse: a
        # by-name probe here IS the ancestor-swap disclosure this exists to stop.
        raise pinned_fs.PinnedPathRefusal(
            "this platform cannot pin a directory walk, so a 'file' check cannot "
            "be made race-free here; use a 'pr_checks' condition or ask the user"
        )
    parent, name = os.path.split(path)
    dir_fd = pinned_fs.pin_parent(parent, what="acceptance check's target directory")
    try:
        return pinned_fs.stat_at(dir_fd, name) is not None
    finally:
        os.close(dir_fd)


def _gate_accept(accept: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, Tuple[str, str] | None]:
    """Apply the MCP path's in-band gate to one acceptance spec.

    Returns ``(accept_to_use, refusal)`` with exactly one side populated.

    The CLI script's security invariant assumes it is reached through an
    ``execute_bash`` prompt: that prompt IS the call-time check on the spec's
    model-authored fields, and the hooks deny floor, the sensitive-path matcher
    and the governance ceiling all read an ``execute_bash`` command string that
    does not exist on an MCP call. They could not read this one anyway: the path
    arrives NESTED at ``items[i].accept.path``, and the PreToolUse argument
    classifier only reads a top-level ``path``, so no policy upstream of this
    door ever sees it. ``conductor_accept_eval`` is auto-approved so patrol never
    stops for a prompt, which leaves these checks as the only call-time ones --
    run in band on the dispatch path, the shape the computer-use surface uses for
    the same reason.

    Both NARROW rather than consult: an in-process policy lookup would fail OPEN
    wherever the ceiling is not composed, which is exactly the host the check
    exists for.

    * ``file.path`` must be absolute AND resolve inside a tree the agent itself
      writes (``github_runner.agent_writable_roots`` -- the project checkout and
      the workspace root), then survive ``hooks.validate_file_path`` (the UNC
      trusted-root gate BEFORE resolution, then ``is_sensitive_path`` on the
      canonical target). A work item's acceptance condition is "did the child
      session produce this file", and a child session writes into exactly those
      roots, so nothing legitimate sits outside them -- while ``/etc/hosts``,
      ``~/.ssh/id_rsa`` and ``security_policy.json`` become unreachable by
      construction rather than by a policy that may be absent. NOTE the
      polarity: ``agent_writable_roots`` is a DENY list for provider binaries (a
      repo-planted ``gh`` shim) and an ALLOW list here; both follow from the one
      fact it states -- the model can write there.
    * ``pr_checks.repo`` is REQUIRED and constrained to ``owner/name``. Required
      because ``gh pr checks N`` with no ``--repo`` resolves the repository from
      the CWD, and on this door that is the MCP server process's directory, not
      the conductor's session -- so an omitted repo silently returns a verdict
      about PR N in whatever repository the gateway started in, and the conductor
      treats that verdict as final. Constrained because ``gh`` runs with the
      operator's ambient credentials.

    A rejected spec is a ``refused`` verdict, not an exception: ``refused`` is
    the script's own vocabulary for "surface this to the user, never retry
    around it", which is exactly what a blocked probe is.
    """
    kind = accept.get("kind")
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            # Shape errors stay the script's to report, so the message is
            # identical on both doors.
            return accept, None
        if not os.path.isabs(path):
            return None, (
                "refused",
                f"path must be absolute on this door, got {path!r}: a relative "
                "path resolves against the MCP server's directory, not your "
                "session's",
            )
        canonical = hooks.validate_file_path(path)
        if canonical is None:
            return None, (
                "refused",
                "path refused by the filesystem gate (sensitive path, or an "
                "untrusted UNC host); name a path inside the workspace",
            )
        roots = tuple(str(root) for root in github_runner.agent_writable_roots())
        if not roots or not _within_any(canonical, roots):
            return None, (
                "refused",
                "path is outside the project and workspace roots; a work item's "
                "output lives inside them, so name a path there",
            )
        gated = dict(accept)
        gated["path"] = canonical
        return gated, None
    if kind == "pr_checks":
        repo = accept.get("repo")
        if not repo:
            return None, (
                "refused",
                "pr_checks needs an explicit repo on this door: without one `gh` "
                "resolves the repository from the server process's directory, "
                "not your session's. Spell it out as owner/name",
            )
        if (
            not isinstance(repo, str)
            or repo.count("/") != 1
            or not all(seg not in (".", "..") and _SEGMENT_RE.match(seg) for seg in repo.split("/"))
        ):
            return None, (
                "refused",
                f"repo must be owner/name with a safe charset, got {repo!r}",
            )
    return accept, None


def _audited_spawn(argv: list[Any], timeout: float, cwd: Any = None) -> Any:
    """Spawn a script-built ``gh`` argv through the app's audited chokepoint.

    The script builds the argv and asserts its LOGICAL name; this replaces the
    bare ``subprocess.run`` the CLI door uses, because on an auto-approved door a
    PATH lookup for ``gh`` is a substitution the model's own workspace can win.
    ``resolve_gh`` returns a validated absolute path (rejecting a binary found
    inside a tree the agent writes), and ``run_gh`` adds what a bare spawn cannot:
    a minimal gh-scoped environment, so a substituted binary never sees unrelated
    gateway secrets, and a fail-closed SEL ``invoked`` event written BEFORE the
    spawn, so an unauditable call does not run.

    A setup failure (no usable ``gh``, or audit storage unavailable) is raised as
    ``OSError`` so the script's verdict mapping reports it as
    ``("error", "could not run: ...")`` -- it genuinely could not run, and that
    keeps one spelling of what a failed spawn MEANS.
    """
    if cwd is not None:
        # Nothing passes this today (the pr_checks handler builds its argv with
        # no cwd) and this door could not honour it: ``run_gh`` takes no cwd by
        # design, and silently dropping one would restore the wrong-directory
        # verdict the required ``repo`` above exists to prevent.
        raise OSError("this door cannot run a check in a chosen directory")
    try:
        resolved = github_runner.resolve_gh()
        return github_runner.run_gh(
            [resolved, *(str(a) for a in argv[1:])],
            timeout=timeout,
            audit_caller="core:goal-conductor",
        )
    except github_runner.SetupError as exc:
        raise OSError(str(exc)) from exc


def _gated_evaluate(item: Dict[str, Any]) -> Tuple[str, str]:
    """Evaluate one item after gating its spec. The MCP path's per-item handler."""
    mod = _get_accept_eval()
    accept = item.get("accept") or {}
    try:
        if not isinstance(accept, dict):
            # Not a dict at all: let the script produce its own message.
            return mod._evaluate(item, spawn=_audited_spawn, exists=_pinned_exists)
        gated, refusal = _gate_accept(accept)
        if refusal is not None:
            return refusal
        target = item if gated is accept else {**item, "accept": gated}
        return mod._evaluate(target, spawn=_audited_spawn, exists=_pinned_exists)
    except pinned_fs.PinnedPathRefusal as exc:
        # A component became a link between resolution and the probe. That is a
        # spec to re-express (or an attack), never an evaluator bug, so it gets
        # the refusal vocabulary rather than the per-item "evaluator bug" wrap.
        return ("refused", str(exc))


def evaluate_items(items: list[Any]) -> list[Dict[str, Any]]:
    """Evaluate a batch of work items, gated for the MCP door.

    Delegates the loop itself to ``accept_eval.evaluate_items`` so the per-item
    contract (object guard, positional id fallback, one bad spec never hiding
    the rest) has exactly ONE spelling, and injects the gated per-item handler.
    """
    mod = _get_accept_eval()
    return mod.evaluate_items(items, evaluate=_gated_evaluate)


def ledger_mode(mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run a ledger codec operation.

    ``mode`` is one of: encode, decode, validate, rotate.
    Delegates to the corresponding function in ``ledger_entry._MODES``.
    """
    mod = _get_ledger_entry()
    handler = mod._MODES.get(mode)
    if handler is None:
        return {"ok": False, "error": {"code": "unknown_mode", "detail": f"unknown mode {mode!r}"}}
    return handler(payload)
