"""Per-run auto-approve authorization for an armed AutoNudge loop.

An unattended loop that loses auto-approval mid-run accomplishes nothing: every
cycle dispatches a tool, waits out the approval window with nobody present to
answer, and is declined. The fix is not to keep a global grant alive longer --
it is to give the RUN its own grant, sized by the operator at the moment they
arm it, and to hand it back the moment the run ends.

Three properties make this narrower than the alternatives already shipping:

* **Scoped, not session-wide.** The grant is a ``SafetyOverride`` scope key, so
  it never flips global YOLO. Its blast radius is the one session that will
  consume it, and only while its loop is armed. (Same mechanism the task runner
  uses for ``taskrunner:{id}:autoapprove``.)
* **Declared, not predicted.** The window comes from the operator picking one of
  :data:`AUTHORIZED_WINDOWS`. Nothing here estimates how long the run will take:
  a global turn-duration histogram mixes every agent and is empty on a cold
  start, so an estimate derived from it is not conservative or aggressive, it is
  meaningless. The operator arming an overnight run already knows the answer.
* **Never extended.** There is no renew path. The deadline is fixed when the
  operator authorizes, and the only dynamic behaviour -- release on loop end --
  moves it strictly earlier. An agent cannot lengthen its own authority, which
  is the property the security review cares about.

The grant is deliberately NOT created here at arm time. ``authorize_run`` must be
reached from an authenticated, owner-gated operator action; an agent-supplied
window would be a self-grant wearing a human's clothes.
"""

from __future__ import annotations

import logging

from kiro_crew.safety_override import safety_override
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# The windows an operator may pick, in seconds. Kept as a closed enum rather
# than a free integer so the authorizing endpoint cannot be talked into an
# arbitrary duration, and so the longest one stays well under the
# SafetyOverride 24h ceiling instead of leaning on it.
AUTHORIZED_WINDOWS: tuple[int, ...] = (2 * 3600, 8 * 3600, 12 * 3600)

_SCOPE_PREFIX = "autonudge"
_SCOPE_SUFFIX = "autoapprove"


def run_grant_scope(slot_key: str) -> str:
    """The scope key holding *slot_key*'s per-run grant.

    Keyed by the loop's binding key rather than by loop id on purpose: the
    approval path resolves this from the session it is already holding, so
    deciding one tool call costs a dict lookup and never a walk of the loop
    registry. The release paths hold the loop (hence its ``slot_key``), so both
    sides derive the same key without either needing the other's state.
    """
    return f"{_SCOPE_PREFIX}:{slot_key}:{_SCOPE_SUFFIX}"


def authorize_run(slot_key: str, window_secs: int, *, source: str) -> bool:
    """Grant *slot_key*'s run auto-approval for *window_secs*.

    Returns ``False`` for a window outside :data:`AUTHORIZED_WINDOWS` (audited as
    a denial) or when the underlying fail-closed activation refuses. Callers MUST
    already have established that an owner asked for this.
    """
    slot_key = (slot_key or "").strip()
    if not slot_key:
        return False
    if window_secs not in AUTHORIZED_WINDOWS:
        # Deny-by-default on the duration itself: an out-of-enum window is the
        # shape a caller trying to mint its own ceiling would take, so it is
        # worth an audit event rather than a silent clamp.
        sel().log_api_access(
            caller="autonudge",
            operation="autonudge.run_authorize",
            outcome="denied",
            source=source,
            resources=f"slot:{slot_key}, reason:window_not_offered, asked:{window_secs}s",
        )
        return False

    # "Never extended" has to hold against REPETITION, not just against a renew
    # call: activate_scoped overwrites the scope's expiry, so authorizing twice
    # would push the deadline past the window the operator declared. Refusing
    # while a grant is live is what makes the property true -- an operator who
    # wants a different window revokes first, which is the direction that can
    # only reduce authority.
    scope = run_grant_scope(slot_key)
    if safety_override().is_scope_active(scope):
        sel().log_api_access(
            caller="autonudge",
            operation="autonudge.run_authorize",
            outcome="denied",
            source=source,
            resources=f"slot:{slot_key}, reason:already_authorized",
        )
        return False

    result = safety_override().activate_scoped(
        scope, source=source, ttl=window_secs
    )
    # Read the field directly rather than through a defaulted ``getattr``: a
    # misspelled attribute name would otherwise degrade to "never granted",
    # which is silent, always-off, and passes any test whose double supplies the
    # wrong name. An AttributeError here is the failure being visible.
    granted = bool(result.active)
    sel().log_api_access(
        caller="autonudge",
        operation="autonudge.run_authorize",
        outcome="success" if granted else "error",
        source=source,
        resources=f"slot:{slot_key}, window:{window_secs}s",
    )
    return granted


def release_run_grant(slot_key: str, *, reason: str) -> None:
    """Hand back *slot_key*'s per-run grant.

    Called from every path that ends a loop. Idempotent, and deliberately
    swallowing: a loop MUST still finish stopping if the grant is already gone or
    the SEL write fails, and the grant is in-memory, so the worst case of a
    failure here is bounded by the deadline the operator already chose.
    """
    slot_key = (slot_key or "").strip()
    if not slot_key:
        return
    scope = run_grant_scope(slot_key)
    try:
        was_active = safety_override().is_scope_active(scope)
        safety_override().deactivate_scope(scope)
    except Exception:
        logger.error("Failed to release run grant for %s", slot_key, exc_info=True)
        return
    if not was_active:
        # Nothing was granted (the common case: most loops are never
        # authorized). Staying silent keeps the SEL free of an event per
        # ordinary loop stop, which would bury the ones that matter.
        return
    try:
        sel().log_api_access(
            caller="autonudge",
            operation="autonudge.run_release",
            outcome="success",
            source="autonudge",
            resources=f"slot:{slot_key}, reason:{reason}",
        )
    except Exception:
        logger.error("Failed to audit run-grant release for %s", slot_key, exc_info=True)
