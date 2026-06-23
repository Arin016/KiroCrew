"""Phase 5 — profile store + active-scope resolution (per-surface / app / task).

Level 2 of the governance model.  A *profile* is a narrow-only ceiling bound to
a surface (``cron``/``slack``/``dashboard``/``subagent``/…), an app slug, or a
task id.  At each tool call the active profile is resolved from the session key
and agent, then intersected with the policy ceiling by ``governance.resolve``.

Kept apart from the pure-data ``governance`` module (which has no I/O) so the
filesystem read + mtime hot-reload + fallback policy live in one place — the
same split the config package uses (schema/eval vs loader).

Resolution principles (from the design + the grounding analysis):

* **Single owner per surface.** The active profile is keyed on the *session
  key* taxonomy (``sel._infer_source`` is the canonical classifier — reused, not
  re-implemented) and the agent name; never on a human-supplied value.
* **Deny-by-default on unproven identity.** A surface whose identity cannot be
  established resolves to the most-restrictive built-in profile
  (``deny_all_profile``), mirroring the dashboard ``api_session_tool_policy``
  precedent — never a permissive fall-through.
* **Invalid profile → deny-all, not the ceiling** (Validation rule 5): a
  schema-invalid profile file must not silently widen to the policy ceiling.
* **Hot-reload via mtime fingerprint** (reusing the config loader's cheap
  ``st_mtime_ns + st_size`` signature idea) so an operator edit is picked up
  without a restart, while the policy ceiling stays boot-frozen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from kiro_claw.platform.context import PlatformCompositionError
from kiro_claw.platform.governance import (
    Bind,
    Profile,
    compose_profiles,
    deny_all_profile,
    parse_profile,
)

logger = logging.getLogger(__name__)

_PROFILES_DIR = Path.home() / ".kiroclaw" / "profiles"

# Surfaces that run UNATTENDED with no interactive operator in the loop.  When
# such a surface has no explicitly-bound profile AND its identity is unproven,
# it resolves to deny-all rather than the (permissive) no-profile path — these
# are the high-blast-radius surfaces the profile layer exists to contain.
_UNATTENDED_SURFACES = frozenset({"cron", "subagent", "background", "heartbeat", "taskrunner"})


def _profiles_dir() -> Path:
    """The profiles directory (indirection so tests can monkeypatch the module)."""
    return _PROFILES_DIR


def _infer_surface(session_key: str) -> str:
    """Classify a session key to its surface.

    Delegates to ``sel._infer_source`` — the single canonical classifier — so
    governance never grows a 4th, drifting copy of the taxonomy parser.
    """
    from kiro_claw.sel import _infer_source

    return _infer_source(session_key)


def _salvage_bind(data: object) -> Optional[Bind]:
    """Extract a VALID bind from raw profile JSON, ignoring all other errors.

    Used on the invalid-profile fallback path so a profile whose controls are
    malformed but whose ``bind`` is well-formed still maps its bound surface to
    deny-all (fail-closed), instead of being dropped from the bind index and
    failing open to the policy ceiling.  Returns None when no valid bind is
    present (then the deny-all profile is simply unbound, as before).
    """
    if not isinstance(data, dict):
        return None
    raw_bind = data.get("bind")
    if not isinstance(raw_bind, dict):
        return None
    btype = str(raw_bind.get("type", "")).strip()
    if btype not in ("surface", "app", "task"):
        return None
    return Bind(type=btype, id=str(raw_bind.get("id", "")))


def _dir_fingerprint(directory: Path) -> Tuple:
    """Cheap signature of the profiles dir — busts the cache on any edit.

    Mirrors ``config.loader._config_fingerprint``: ``st_mtime_ns + st_size`` per
    file plus the set of names, so a create / edit / truncate / delete all change
    the fingerprint.  A missing directory yields a stable sentinel.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return (("<absent>", str(directory)),)
    sig: list = []
    for p in entries:
        if p.suffix != ".json":
            continue
        try:
            st = p.stat()
            sig.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((p.name, None))
    return tuple(sig)


class ProfileStore:
    """Loads + caches profiles from ``~/.kiroclaw/profiles`` with mtime hot-reload.

    A schema-invalid profile is recorded as a deny-all sentinel (never the
    ceiling) so a broken file fails closed.  ``extends`` is resolved by
    ``compose_profiles`` (monotonic narrowing); a cyclic/missing parent falls
    back to deny-all.
    """

    def __init__(self) -> None:
        self._fingerprint: Optional[Tuple] = None
        self._by_name: Dict[str, Profile] = {}
        # surface/app/task index → profile name, built from each profile's bind.
        self._by_bind: Dict[Tuple[str, str], str] = {}

    def _ensure_fresh(self) -> None:
        directory = _profiles_dir()
        fp = _dir_fingerprint(directory)
        if fp == self._fingerprint:
            return
        self._reload(directory)
        self._fingerprint = fp

    def _reload(self, directory: Path) -> None:
        by_name: Dict[str, Profile] = {}
        by_bind: Dict[Tuple[str, str], str] = {}
        try:
            files = [p for p in sorted(directory.iterdir()) if p.suffix == ".json"]
        except OSError:
            files = []
        # Pass 1: parse each file independently; an invalid one becomes deny-all.
        for path in files:
            stem = path.stem
            data: object = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise PlatformCompositionError("profile is not a JSON object")
                by_name[stem] = parse_profile(data)
            except Exception:
                logger.warning(
                    "profile %s is invalid; falling back to deny-all (fail-closed)",
                    path.name,
                    exc_info=True,
                )
                # Preserve a salvageable bind so the BOUND surface still resolves
                # to deny-all (not policy-only).  Without this, an invalid profile
                # with a valid bind would be dropped from the bind index and its
                # surface would fail OPEN to the policy ceiling — defeating
                # Validation rule 5 on the binding path.
                fallback = deny_all_profile(stem)
                salvaged = _salvage_bind(data)
                if salvaged is not None:
                    fallback = replace(fallback, bind=salvaged)
                by_name[stem] = fallback
        # Pass 2: resolve ``extends`` (monotonic narrowing) now that all are parsed.
        for name, profile in list(by_name.items()):
            if profile.extends:
                parent = by_name.get(profile.extends)
                if parent is None or parent.extends:  # missing or non-trivial chain
                    logger.warning(
                        "profile %r extends %r which is missing/chained; deny-all",
                        name,
                        profile.extends,
                    )
                    by_name[name] = deny_all_profile(name)
                else:
                    by_name[name] = compose_profiles(parent, profile)
        # Build the bind index.  Last writer wins on a duplicate bind, logged.
        for name, profile in by_name.items():
            if profile.bind is not None:
                key = (profile.bind.type, profile.bind.id)
                if key in by_bind and by_bind[key] != name:
                    logger.warning(
                        "profiles %r and %r both bind %s; using %r",
                        by_bind[key],
                        name,
                        key,
                        name,
                    )
                by_bind[key] = name
        self._by_name = by_name
        self._by_bind = by_bind

    def get(self, name: str) -> Optional[Profile]:
        self._ensure_fresh()
        return self._by_name.get(name)

    def for_bind(self, bind: Bind) -> Optional[Profile]:
        self._ensure_fresh()
        name = self._by_bind.get((bind.type, bind.id))
        return self._by_name.get(name) if name else None

    def all_profiles(self) -> "list[Profile]":
        """Every loaded profile (for the boot-time floor assertion)."""
        self._ensure_fresh()
        return list(self._by_name.values())


# Process-global store (cheap; hot-reloads itself on access).
_STORE = ProfileStore()


def reset_store() -> None:
    """Test helper — drop the cached profiles so the next access reloads."""
    global _STORE
    _STORE = ProfileStore()


def get_store_profile(name: str) -> Optional[Profile]:
    """Return a profile by file stem (read-only; used by ``policy``/``profile`` CLI)."""
    return _STORE.get(name)


def assert_profiles_within_ceiling(ceiling: "object") -> None:
    """Boot-time floor gate: every loaded profile must be ≥ as strict as the ceiling.

    Implements Validation rules 3 & 7 and the Combined-order "app/profile ≥
    ceiling for every control? no → ABORT fail-closed" step: a profile whose
    ordinal (approval_mode / sandbox.min_level) is LOOSER than the policy mark
    raises ``PlatformCompositionError`` and aborts boot, rather than being
    silently re-tightened only at runtime.  No-op when no ceiling is present
    (standalone, ungoverned).  Called once at boot from ``bootstrap_context``.
    """
    if ceiling is None:
        return
    from kiro_claw.platform.governance import GovernanceCeiling, assert_governance_floor

    if not isinstance(ceiling, GovernanceCeiling):
        return
    for profile in _STORE.all_profiles():
        assert_governance_floor(ceiling, profile)  # raises PlatformCompositionError on weakening


def governance_permits(
    scope: str,
    item: str,
    *,
    session_key: str = "",
    agent: str = "",
    app: str = "",
) -> "object":
    """One-call chokepoint helper: is *item* permitted in *scope* right now?

    Resolves the boot-frozen ceiling (from the active context) ∩ the active
    profile for the calling surface, and returns the ``Decision``.  This is the
    single entry point every wired chokepoint calls so they share one decision
    source and audit path — no chokepoint re-implements resolution.  Wired
    chokepoints today: the PreToolUse host gate (tools/mcp/commands), cron command
    authoring (``commands``), sub-agent spawn (``capabilities.spawn``), outbound
    messaging (``capabilities.messaging``), and the sandbox ordinal floor
    (``sandbox.min_level`` via ``governance_floor_ordinal``).  The
    ``filesystem``/``folders``, ``channels``, ``network.egress``, and
    ``approval_mode`` scopes are modeled + resolvable but NOT yet wired to a
    dedicated chokepoint (see ``docs/system-specs/modules/governance.md`` →
    "Modeled-but-not-yet-enforced scopes"); a future change adds those gates.

    Fail-closed discipline matches the gate: a ``PlatformCompositionError``
    propagates; any other unexpected error returns a permissive Decision (the
    chokepoint's own always-on checks still run) rather than wedging the surface.
    """
    from kiro_claw.platform.context import (
        PlatformCompositionError,
        current_context,
    )
    from kiro_claw.platform.governance import Decision, resolve

    try:
        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(session_key, agent=agent, app=app)
        if ceiling is None and profile is None:
            return Decision(True, "ungoverned", rule="default")
        return resolve(ceiling, profile, scope, item)
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("governance_permits failed; permissive", exc_info=True)
        from kiro_claw.platform.governance import Decision as _D

        return _D(True, "governance error; no opinion", rule="default")


def governance_floor_ordinal(
    scope: str,
    *,
    session_key: str = "",
    agent: str = "",
    app: str = "",
) -> Optional[str]:
    """Return the effective ordinal floor value for *scope*, or ``None``.

    Used by the sandbox / approval chokepoints to clamp a requested tier up to
    at least the governed strictness (e.g. ``sandbox.min_level``).  ``None`` means
    no governance opinion (caller keeps its own default).
    """
    from kiro_claw.platform.context import (
        PlatformCompositionError,
        current_context,
    )
    from kiro_claw.platform.governance import resolve_ordinal

    try:
        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(session_key, agent=agent, app=app)
        eff = resolve_ordinal(ceiling, profile, scope)
        return eff.value if eff is not None else None
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("governance_floor_ordinal failed; no opinion", exc_info=True)
        return None


def resolve_active_scope(
    session_key: str,
    *,
    agent: str = "",
    app: str = "",
    task: str = "",
) -> Optional[Profile]:
    """Resolve the active profile for a tool call, or ``None`` for policy-only.

    Precedence of bindings (most specific first):

    1. ``app`` bind — when an app is the active context, its per-app profile
       bounds the blast radius (the design's headline per-app use case).
    2. ``task`` bind — a specific spawned task's profile.
    3. ``surface`` bind — the surface inferred from the session key.

    Returns ``None`` when no profile is bound AND the surface is attended/proven
    (policy ceiling alone governs).  Returns ``deny_all_profile()`` when an
    unattended surface has no bound profile and no proven identity — fail-closed,
    never a permissive fall-through.
    """
    if app:
        prof = _STORE.for_bind(Bind(type="app", id=app))
        if prof is not None:
            return prof
    if task:
        prof = _STORE.for_bind(Bind(type="task", id=task))
        if prof is not None:
            return prof

    # An agent name may carry its own task-scoped profile (e.g. a tightly-scoped
    # "researcher" agent), checked before the broad surface binding so a spawned
    # agent's own ceiling wins over its surface's default.
    if agent:
        prof = _STORE.for_bind(Bind(type="task", id=agent))
        if prof is not None:
            return prof

    surface = _infer_surface(session_key)
    prof = _STORE.for_bind(Bind(type="surface", id=surface))
    if prof is not None:
        return prof

    # No bound profile.  Unattended + unproven identity → deny-all (fail-closed).
    # NOTE on the empty-key case: an empty/whitespace session key is the
    # documented OPT-OUT default of governance_permits/on_tool_call ("every
    # existing caller is unaffected"), and the taxonomy classifier maps it to the
    # attended "slack" surface — so it intentionally resolves to None (policy
    # ceiling alone governs), NOT deny-all.  Making it deny-all here would break
    # the gate's ungoverned no-op contract on every standalone host.  The
    # unattended sentinels (_bg/_hb) and unattended surfaces ARE contained.
    identity_proven = bool(session_key) and session_key not in ("", "_bg", "_hb")
    if surface in _UNATTENDED_SURFACES and not identity_proven:
        return deny_all_profile(f"_deny_all:{surface}")

    # Attended/proven surface with no profile → policy ceiling alone governs.
    return None
