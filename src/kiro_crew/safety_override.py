"""Time-limited safety override — replaces permanent YOLO mode.

Provides a ``SafetyOverride`` class with two kinds of grant:

- **Ad-hoc** — YOLO toggled mid-session from Slack, the dashboard picker or the
  API. Bounded by ONE duration shared by every surface (``agent.yolo_duration``,
  default 6 h, hard ceiling 24 h) and automatically expires. A 5-minute grace
  window after expiry allows renew() to reactivate without a full
  re-activation flow.
- **Declared** — ``agent.dangerously_skip_permissions: true`` in operator-owned
  config (the camelCase and legacy ``yolo`` spellings are also read). A standing
  instruction, so it does NOT expire: it is re-established and re-audited on
  every startup (state is in-memory), cleared the moment the operator picks
  another approval mode, and deniable by the enterprise governance ceiling via
  the ``yolo_duration`` scope's ``permanent`` member — which downgrades it to the
  ad-hoc duration.

Per-surface TTLs (30 min Slack / 6 h dashboard / 24 h config) were removed: the
same operator re-enabling the same grant got a different lifetime depending on
where they clicked, which was unpredictable without buying any security.

All state changes are logged to the Security Event Log (SEL).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from kiro_crew.sel import sel as _get_sel

logger = logging.getLogger(__name__)


def sel():  # noqa: ANN201 — thin wrapper kept for test patchability
    """Return the SEL singleton.

    Defined at module level so tests can patch ``kiro_crew.safety_override.sel``.
    """
    return _get_sel()


# ─── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class ActivationResult:
    """Returned by SafetyOverride.activate()."""

    active: bool
    ttl: int
    source: str
    activated_at_iso: str


@dataclass
class RenewResult:
    """Returned by SafetyOverride.renew()."""

    renewed: bool
    ttl: int  # 0 if not renewed
    source: str
    reason: str = ""  # populated on denial


@dataclass
class OverrideStatus:
    """Snapshot returned by SafetyOverride.status()."""

    active: bool
    source: str
    remaining_secs: int
    activation_count: int
    activated_at_iso: Optional[str]  # None when inactive
    expires_at_iso: Optional[str]  # None when inactive
    last_renewed_at_iso: Optional[str]  # None if never renewed
    last_renewed_by: str
    # True when the live grant was DECLARED in config and has no expiry at all.
    # ``remaining_secs`` is -1 and ``expires_at_iso`` is None in that case.
    permanent: bool = False


# ─── Core class ──────────────────────────────────────────────────────────────


class SafetyOverride:
    """Time-limited safety override with SEL audit trail.

    All public methods are thread-safe.
    """

    # ── Constants ────────────────────────────────────────────────────────────

    _MAX_TTL: int = 86400  # 24 h hard ceiling for an AD-HOC grant
    # ONE duration for every ad-hoc surface. Enabling YOLO from Slack and from
    # the dashboard picker is the same decision made from different places, so
    # they expire the same way. Per-surface TTLs (30 min Slack / 6 h dashboard)
    # made the behavior unpredictable without buying security: the same operator
    # re-enabled the same grant either way. Overridable via
    # ``agent.yolo_duration``, clamped to ``_MAX_TTL``.
    _ADHOC_TTL_DEFAULT: int = 21600  # 6 h
    _RENEW_GRACE_SECS: int = 300  # 5-min grace window after expiry
    # How far AUTOMATIC lease extensions may carry ONE ad-hoc grant, measured from
    # ``_activated_at`` — a deadline, not a budget of extension seconds.
    #
    # PROPORTIONAL to the TTL the operator chose, capped by ``_MAX_TTL``:
    #
    #     ceiling = _activated_at + min(_MAX_TTL, 4 x activation_ttl)
    #
    # A flat ``_MAX_TTL`` was the previous shape and it erased the operator's own
    # judgement: someone who deliberately set a 1h grant was softened to 24h the
    # moment any loop was armed, ending up in exactly the same place as someone
    # who set 6h. The cautious setting bought nothing. Scaling keeps a tight grant
    # proportionally tight (1h -> 4h) while the default 6h still reaches the 24h
    # cap, so the common case is unchanged.
    #
    # A cumulative-extension budget was the shape before that, and was wrong for a
    # different reason: 24h of activation plus 24h of leases authorizes 48h on one
    # human decision, which is what ``_MAX_TTL`` exists to forbid. Anchoring on the
    # activation instant is what makes the bound absolute rather than additive.
    #
    # This exists for one pathological shape, not for normal use. The agent itself
    # calls ``monitor_start`` and can raise ``max_cycles`` through
    # ``monitor_update``, so "keep renewing while a loop is armed" would otherwise
    # let the authorized party set its own authorization's lifetime. Human
    # renewals (``renew``) are a fresh decision by someone who can see what they
    # are approving and are not bound by it.
    _LEASE_CEILING_MULTIPLE: int = 4

    # The one source carrying STANDING authority: a grant the operator DECLARED
    # in config (``dangerouslySkipPermissions``), as opposed to one toggled ad hoc
    # mid-session. A declared grant does not expire — see ``activate_declared``.
    _DECLARED_SOURCE: str = "config"

    # Class-level default lock for instances created via object.__new__() (e.g. tests).
    # Each real instance gets its own lock in __init__; this is just a safe fallback.
    _lock: threading.Lock

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._source: str = ""
        self._activated_at: float = 0.0
        self._expires_at: float = 0.0
        self._activation_count: int = 0
        self._last_renewed_at: float = 0.0
        self._last_renewed_by: str = ""
        self._on_expired: Optional[Callable[[str], None]] = None
        self._on_activated: Optional[Callable[[str, int], None]] = None
        # True when the live grant has NO expiry: either DECLARED in config, or
        # an ad-hoc grant under ``yolo_duration: until_shutdown``. Policy
        # permits a standing grant. A permanent grant has no deadline at all, so
        # ``_expires_at`` is not consulted while it is set — but it is still kept
        # finite so the 0.0 "never activated / deactivated" sentinel and the
        # renew grace window keep their meaning for every other path.
        self._permanent: bool = False
        # Ad-hoc TTL in force, seeded from ``agent.yolo_duration`` at startup.
        self._adhoc_ttl: int = self._ADHOC_TTL_DEFAULT
        # True when ``agent.yolo_duration`` is ``until_shutdown``: an ad-hoc grant
        # then has no timed expiry and lasts until the process stops. Still
        # in-memory, so it cannot survive a restart the way a DECLARED grant does.
        self._adhoc_until_shutdown: bool = False
        # Resolves the ad-hoc duration from LIVE config at activation time.
        # Installed in production by ``install_duration_resolver``; ``None`` in
        # tests, which set ``adhoc_ttl`` / ``adhoc_until_shutdown`` directly.
        # Reading it live is what makes a duration saved from Settings apply to
        # the next activation instead of only after a restart.
        self._duration_resolver: Optional[Callable[[], tuple[int, bool]]] = None
        # Task-scoped auto-approve grants: scope key -> (activated_at, expires_at)
        # monotonic. Independent of the global override; each grant is TTL-bounded,
        # audited on activation, and slide-renewable up to a 24h ceiling from first
        # activation, so a caller (e.g. the task runner) can hold a narrow, expiring
        # grant without flipping the session-wide override.
        self._scoped: dict[str, tuple[float, float]] = {}

    def __getattr__(self, name: str) -> object:
        # Provide a fallback _lock for instances created with object.__new__()
        # that have not gone through __init__ (test fixtures bypass __init__).
        if name == "_lock":
            lock = threading.Lock()
            object.__setattr__(self, "_lock", lock)
            return lock
        if name == "_activation_ttl":
            object.__setattr__(self, "_activation_ttl", 0)
            return 0
        if name == "_scoped":
            scoped: dict[str, tuple[float, float]] = {}
            object.__setattr__(self, "_scoped", scoped)
            return scoped
        # Same reason as _lock/_scoped: test fixtures build instances via
        # object.__new__() and set fields by hand, so the expiry path must still
        # be able to read these.
        if name == "_permanent":
            object.__setattr__(self, "_permanent", False)
            return False
        if name == "_adhoc_ttl":
            object.__setattr__(self, "_adhoc_ttl", self._ADHOC_TTL_DEFAULT)
            return self._ADHOC_TTL_DEFAULT
        if name == "_adhoc_until_shutdown":
            object.__setattr__(self, "_adhoc_until_shutdown", False)
            return False
        if name == "_duration_resolver":
            object.__setattr__(self, "_duration_resolver", None)
            return None
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # ── Callback properties ──────────────────────────────────────────────────

    @property
    def on_expired(self) -> Optional[Callable[[str], None]]:
        return self._on_expired

    @on_expired.setter
    def on_expired(self, cb: Optional[Callable[[str], None]]) -> None:
        self._on_expired = cb

    @property
    def on_activated(self) -> Optional[Callable[[str, int], None]]:
        return self._on_activated

    @on_activated.setter
    def on_activated(self, cb: Optional[Callable[[str, int], None]]) -> None:
        self._on_activated = cb

    @property
    def adhoc_ttl(self) -> int:
        """Seconds an ad-hoc grant lasts (Slack, dashboard, API — all the same)."""
        return self._adhoc_ttl

    @adhoc_ttl.setter
    def adhoc_ttl(self, secs: int) -> None:
        self._adhoc_ttl = max(1, min(int(secs), self._MAX_TTL))

    @property
    def adhoc_until_shutdown(self) -> bool:
        """True when an ad-hoc grant should last until the process stops."""
        return bool(self._adhoc_until_shutdown)

    @adhoc_until_shutdown.setter
    def adhoc_until_shutdown(self, value: bool) -> None:
        self._adhoc_until_shutdown = bool(value)

    @property
    def duration_resolver(self) -> Optional[Callable[[], tuple[int, bool]]]:
        return self._duration_resolver

    @duration_resolver.setter
    def duration_resolver(self, fn: Optional[Callable[[], tuple[int, bool]]]) -> None:
        self._duration_resolver = fn

    def current_adhoc_duration(self) -> tuple[int, bool]:
        """``(ttl_secs, until_shutdown)`` for a NEW ad-hoc grant, resolved live.

        Consults the installed resolver (live config + governance clamp) so a
        duration saved from Settings applies to the next activation without a
        restart. Falls back to the last known values if the resolver fails, so a
        transient config read error cannot wedge activation.
        """
        resolver = self._duration_resolver
        if resolver is not None:
            try:
                ttl, until_shutdown = resolver()
                return max(1, min(int(ttl), self._MAX_TTL)), bool(until_shutdown)
            except Exception:
                logger.warning(
                    "ad-hoc duration resolver failed; using the last known value",
                    exc_info=True,
                )
        return self._adhoc_ttl, bool(self._adhoc_until_shutdown)

    @property
    def is_permanent(self) -> bool:
        """True when the live grant has no expiry at all."""
        return bool(self._permanent) and bool(self._active)

    # ── Public API ───────────────────────────────────────────────────────────

    def activate(self, source: str, ttl: Optional[int] = None) -> ActivationResult:
        """Activate a TTL-bounded (ad-hoc) override for the given source.

        Every ad-hoc surface gets the SAME duration — see ``_ADHOC_TTL_DEFAULT``.
        When ``agent.yolo_duration`` is ``until_shutdown`` an ad-hoc grant has no
        timed expiry and lasts until the process stops (still in-memory, so a
        restart clears it). For the operator's declared
        ``dangerouslySkipPermissions`` grant, which is re-established on every
        startup, use :meth:`activate_declared` instead.

        Args:
            source: Trigger source (``slack``, ``dashboard``, ``config``, …).
            ttl: Explicit TTL in seconds. Defaults to the in-force ad-hoc
                 duration. Capped at ``_MAX_TTL``. Passing an explicit ttl always
                 produces a timed grant, even under ``until_shutdown``.

        Returns:
            ActivationResult with effective TTL and wall-clock activation time.
        """
        if ttl is None:
            ttl, until_shutdown = self.current_adhoc_duration()
            if until_shutdown:
                return self._commit_activation(source, ttl=0, permanent=True)
        ttl = min(ttl, self._MAX_TTL)
        return self._commit_activation(source, ttl=ttl, permanent=False)

    def activate_declared(self, source: str = _DECLARED_SOURCE) -> ActivationResult:
        """Activate a NON-EXPIRING override for an operator-declared grant.

        ``dangerouslySkipPermissions`` is a standing instruction, not a session-scoped
        one: honouring it for 24h and then silently reverting to
        prompt-for-everything is the defect this replaces. The grant is still
        re-established and re-audited on every startup (state is in-memory), is
        cleared the moment the operator picks another approval mode, and is
        deniable by the enterprise governance ceiling — callers must consult
        :func:`declared_grant_permitted` first and fall back to ``activate`` when
        policy forbids a standing grant.
        """
        return self._commit_activation(source, ttl=0, permanent=True)

    def _commit_activation(self, source: str, *, ttl: int, permanent: bool) -> ActivationResult:
        """Shared activation commit: audit fail-closed, then install the grant."""
        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc)
        activated_at_iso = now_wall.isoformat()
        ttl_desc = "permanent" if permanent else f"{ttl}s"

        # Snapshot state under lock for reactivation check
        with self._lock:
            was_active = self._active
            prev_source = self._source
            prev_remaining = (
                -1
                if (self._active and self._permanent)
                else (max(0, int(self._expires_at - now_mono)) if self._active else 0)
            )

        # Audit BEFORE committing — fail-closed with no race window
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate",
                outcome="enabled",
                resources=f"source:{source}, ttl:{ttl_desc}",
                critical=True,
            )
        except Exception:
            logger.error("SEL audit failed; refusing safety override activation", exc_info=True)
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        # Log reactivation only after critical audit succeeds
        if was_active:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:reactivate",
                outcome="enabled",
                resources=f"prev_source:{prev_source}, prev_remaining:{prev_remaining}s, new_source:{source}, new_ttl:{ttl_desc}",
            )

        # Only commit after audit succeeds
        with self._lock:
            self._active = True
            self._source = source
            self._permanent = permanent
            self._activated_at = now_mono
            # Kept finite even when permanent so the 0.0 inactive sentinel and
            # the renew grace window keep working; it is simply not consulted.
            self._expires_at = now_mono + (ttl if ttl > 0 else self._MAX_TTL)
            self._activation_count += 1
            self._last_renewed_at = 0.0
            self._last_renewed_by = ""
            # The TTL the operator chose for THIS grant, which scales the
            # automatic-lease ceiling. Stored rather than re-read from config so a
            # later config edit cannot retroactively widen a live grant.
            self._activation_ttl = ttl if ttl > 0 else self._MAX_TTL

        cb = self._on_activated
        if cb is not None:
            try:
                cb(source, ttl)
            except Exception:
                logger.warning("on_activated callback raised", exc_info=True)

        return ActivationResult(
            active=True,
            ttl=ttl,
            source=source,
            activated_at_iso=activated_at_iso,
        )

    def renew(self, source: str) -> RenewResult:
        """Renew (extend) the override using the source's default TTL.

        Succeeds if the override is currently active OR if it expired within
        the ``_RENEW_GRACE_SECS`` grace window.

        Returns:
            RenewResult.renewed=True on success, False otherwise.
        """
        now_mono = time.monotonic()
        ttl = 0
        # Resolved BEFORE taking the lock: the resolver reads config from disk,
        # and holding the state lock across that I/O would stall every concurrent
        # is_active() check.
        renew_ttl = min(self.current_adhoc_duration()[0], self._MAX_TTL)

        denied = False
        with self._lock:
            # A permanent grant has nothing to extend and must never be
            # downgraded to a finite deadline by a renew.
            if self._active and self._permanent:
                return RenewResult(renewed=True, ttl=-1, source=source)
            currently_active = self._active and self._expires_at > now_mono
            in_grace = (
                not currently_active
                and self._expires_at > 0
                and (now_mono - self._expires_at) <= self._RENEW_GRACE_SECS
            )
            if currently_active or in_grace:
                ttl = renew_ttl
                self._active = True
                self._expires_at = now_mono + ttl
                self._last_renewed_at = now_mono
                self._last_renewed_by = source
            else:
                denied = True

        if denied:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="denied",
                resources="reason:not_active",
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")

        self._log_sel(
            caller="safety_override",
            operation="safety_override:renew",
            outcome="renewed",
            resources=f"source:{source}, new_ttl:{ttl}s",
        )
        return RenewResult(renewed=True, ttl=ttl, source=source)

    def _grant_is_live_locked(self, now_mono: float) -> bool:
        """Is there a grant to EXTEND right now? Caller must hold ``_lock``.

        ``is_active()`` without the expiry callback: the same question, asked from
        a context that must not cause side effects.

        ``_expires_at > 0`` is NOT a sufficient stand-in for this. It was used as
        the "lapsed versus explicitly stopped" discriminator on the theory that
        ``deactivate()`` zeroes it -- but ``deactivate()`` returns early when the
        grant is already inactive, which is exactly the state lazy expiry leaves
        behind. So an operator switching back to normal mode on a lapsed grant
        left ``_expires_at`` positive and indistinguishable from "merely lapsed",
        and a lease would have re-enabled auto-approval they had just turned off.

        Requiring the grant to be LIVE removes the ambiguity instead of refining
        it: a lease only ever extends something already in force, and nothing dead
        is revived regardless of how it died. Proactive renewal always satisfies
        this, because it runs while headroom remains.
        """
        if not self._active or self._activated_at <= 0:
            return False
        if self._permanent:
            return True
        return now_mono < self._expires_at

    def _lease_ceiling_locked(self) -> float:
        """Absolute instant past which no automatic lease may extend this grant.

        Caller must hold ``_lock``. Proportional to the operator's chosen TTL and
        capped at ``_MAX_TTL`` — see ``_LEASE_CEILING_MULTIPLE``.
        """
        ttl = self._activation_ttl if self._activation_ttl > 0 else self._MAX_TTL
        span = min(self._MAX_TTL, self._LEASE_CEILING_MULTIPLE * ttl)
        return self._activated_at + span

    def renew_lease(self, source: str, ttl: int) -> RenewResult:
        """Extend the live grant by one short AUTOMATIC lease.

        Separate from :meth:`renew` on purpose. ``renew`` is a human action and
        takes the configured ad-hoc duration; this is a machine action taken on
        behalf of an unattended loop, so it carries an explicit short lease
        rather than a 6 h refill, and it is bounded absolutely by
        ``_activated_at + _MAX_TTL``.

        That ceiling is anchored on the ACTIVATION, not on this call, which is
        what makes it a real bound: no ad-hoc grant stays active more than
        ``_MAX_TTL`` past the moment a human enabled it, however many leases are
        issued. A lease that would cross the ceiling is truncated to land exactly
        on it; one issued at or after it is refused. A cumulative
        extension-seconds budget was tried first and was wrong -- a 24 h
        activation plus 24 h of leases authorizes 48 h on one human decision,
        which is precisely what ``_MAX_TTL`` exists to forbid.

        Only ever EXTENDS a grant that is live at the moment of the call; it never
        revives a dead one, whether it died by expiry or by ``deactivate``. The
        renew grace window is therefore irrelevant here -- there is no
        post-expiry renewal to grant, so nothing depends on how far past the
        deadline the caller arrived.

        That is stricter than tracking "lapsed versus explicitly stopped", and it
        is stricter on purpose: ``deactivate()`` returns early when the grant is
        already inactive, so it does not zero ``_expires_at`` for a grant that had
        already lapsed. An operator switching back to normal mode in that state
        was indistinguishable from a grant merely waiting to be renewed, and a
        lease would have re-enabled auto-approval they had just turned off.

        Proactive callers are unaffected: renewal runs while headroom remains, so
        the grant is live by construction.

        Fails (``renewed=False``) with ``reason``:

        - ``invalid_ttl`` -- non-positive lease.
        - ``never_active`` -- no grant has ever been activated. The ordinary state
          of an install that does not use auto-approve; not an event.
        - ``not_active`` -- a grant existed and is gone: lapsed, or deactivated.
          Terminal for the mechanism, because a lease extends and never revives, so
          a caller should surface it rather than retry.
        - ``lease_cap`` -- the absolute ceiling is reached. Terminal: let the
          grant lapse and say so. Only a human re-activation lifts it, and
          because the bound is a deadline rather than a budget it cannot be
          walked down in ever-smaller retries.
        - ``audit_failed`` -- the SEL audit could not be written, so the lease is
          refused rather than granted unrecorded.
        - ``raced`` -- a concurrent ``activate``/``renew`` replaced or extended the
          grant while the audit ran unlocked, so this lease would have overwritten
          it. The override is still LIVE: a caller must not treat this like an
          expiry and demote.
        - ``no_extension`` -- the ceiling-truncated lease would land at or before
          the deadline the grant already has, so applying it would SHORTEN the
          grant. Also LIVE; also not an expiry.

        A permanent grant returns ``renewed=True, ttl=-1`` and is left alone --
        there is no deadline to extend, and installing one would be a silent
        downgrade of the operator's standing decision.

        Audit shape: ``authorized`` is written fail-closed BEFORE the commit (no
        authority extended without a durable trace), then resolved by exactly one
        of ``renewed`` or ``aborted``. The pre-commit event deliberately does NOT
        say "renewed": unlike ``_commit_activation``, whose commit cannot be
        refused, this one can be, and a log that over-reports renewals is worse
        than one that reports them a moment late.
        """
        if ttl <= 0:
            return RenewResult(renewed=False, ttl=0, source=source, reason="invalid_ttl")
        lease = min(int(ttl), self._MAX_TTL)
        now_mono = time.monotonic()
        denied_reason = ""
        granted = 0
        seen_activated_at = 0.0
        seen_expires_at = 0.0
        with self._lock:
            if self._active and self._permanent:
                return RenewResult(renewed=True, ttl=-1, source=source)
            if not self._grant_is_live_locked(now_mono):
                # Two shapes of "no": never granted at all (the ordinary state of an
                # install that does not use auto-approve) versus granted and now
                # gone. Only the second is an event.
                denied_reason = "never_active" if self._activated_at <= 0 else "not_active"
            else:
                ceiling = self._lease_ceiling_locked()
                # Reduce the float clock to WHOLE SECONDS once, here, and do all
                # lease arithmetic in ints from this point on. Computing the grant
                # as `int(min(now + lease, ceiling) - now)` looks equivalent and is
                # not: `(now + 900.0) - now` is 899.9999999999999 for some `now`,
                # and int() truncates toward zero, so a full 900s lease silently
                # became 899s depending on the host's clock bits. It passed locally
                # and failed on three CI platforms at once.
                #
                # Flooring is also the correct direction for a ceiling bound: a
                # partial second is dropped rather than crossed.
                remaining = int(ceiling - now_mono)
                if remaining <= 0:
                    # Also catches sub-second headroom, which `now_mono >= ceiling`
                    # let through and would have granted a 0s lease — nominally
                    # "renewed" while expiring at once.
                    denied_reason = "lease_cap"
                else:
                    # Truncate rather than refuse when only part of the lease
                    # fits: refusing would drop the grant early for no gain, and
                    # the ceiling still holds exactly. Both operands are ints, so
                    # a lease that fits is granted EXACTLY.
                    granted = min(lease, remaining)
                    # Snapshot the grant's identity so the post-audit commit can
                    # tell whether it is still extending the SAME grant it was
                    # authorized against. Every mutator moves at least one of
                    # these: activate() sets both, renew() moves the expiry,
                    # deactivate() zeroes it.
                    seen_activated_at = self._activated_at
                    seen_expires_at = self._expires_at

        if denied_reason:
            ceiling_left = self.lease_ceiling_remaining_secs()
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew_lease",
                outcome="denied",
                resources=(
                    f"source:{source}, lease:{lease}s, reason:{denied_reason}, "
                    f"ceiling_in:{ceiling_left}s"
                ),
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason=denied_reason)

        # Audit BEFORE committing, fail-closed — the same contract
        # ``_commit_activation`` follows: no extension of auto-approval authority
        # without a durable trace of it. This path needs it MORE than a human
        # renewal does, not less, because there is no operator present to notice
        # that tools kept being approved.
        #
        # But the outcome word is ``authorized``, NOT ``renewed``. Unlike
        # ``_commit_activation``, whose commit cannot be refused, this commit CAN
        # be (a concurrent deactivate or re-activation), so recording "renewed"
        # here would put a renewal in the audit log that never happened — the log
        # would over-report exactly the fact an auditor consults it for. Intent is
        # what is durable before the act; the act is recorded after it.
        ceiling_left = self.lease_ceiling_remaining_secs()
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew_lease",
                outcome="authorized",
                resources=(
                    f"source:{source}, granted:{granted}s, requested:{lease}s, "
                    f"ceiling_in:{ceiling_left}s"
                ),
                critical=True,
            )
        except Exception:
            logger.error("SEL audit failed; refusing safety override lease", exc_info=True)
            return RenewResult(renewed=False, ttl=0, source=source, reason="audit_failed")

        # What the `authorized` event above actually claimed. The commit block
        # re-derives `granted` from a fresh clock, so without this the `aborted`
        # event could cite a number that was never authorized.
        authorized = granted

        commit_failed = ""
        with self._lock:
            # The audit ran with the lock RELEASED (a SEL write must not be done
            # under it), so both the grant AND the clock may have moved. Every
            # decision below is re-derived from a FRESHLY read clock: reusing the
            # pre-audit instant would let a grant that expired during the SEL write
            # still read as live, and would then install a deadline measured from
            # an instant already in the past.
            commit_now = time.monotonic()
            if not self._grant_is_live_locked(commit_now):
                # Deactivated, or lapsed, while the audit ran unlocked. Refuse,
                # and let the caller demote.
                commit_failed = "not_active"
            elif self._activated_at != seen_activated_at or self._expires_at != seen_expires_at:
                # A concurrent activate()/renew() replaced or extended the grant.
                # This lease was authorized against the OLD one, and committing it
                # would overwrite a fresh multi-hour grant with one short lease.
                # Refuse — but the caller must NOT demote, because the override is
                # live; it is simply no longer this lease's business.
                commit_failed = "raced"
            else:
                remaining = int(self._lease_ceiling_locked() - commit_now)
                granted = min(lease, remaining) if remaining > 0 else 0
                new_deadline = commit_now + granted
                if granted <= 0:
                    commit_failed = "lease_cap"
                elif new_deadline <= self._expires_at:
                    # A lease must never move the deadline BACKWARDS. A human
                    # ``renew()`` can carry a grant past the automatic ceiling, so
                    # the ceiling-truncated lease can be shorter than what the
                    # operator already has — installing it would quietly cut their
                    # grant short in the name of extending it.
                    commit_failed = "no_extension"
                else:
                    self._active = True
                    self._expires_at = new_deadline
                    self._last_renewed_at = commit_now
                    self._last_renewed_by = source

        if commit_failed:
            # Resolve the ``authorized`` record above: the lease was authorized but
            # NOT applied. Non-critical — the fail-closed requirement is already
            # satisfied by the pre-commit event, and refusing to report a refusal
            # would be the wrong way round.
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew_lease",
                outcome="aborted",
                resources=(
                    f"source:{source}, authorized:{authorized}s, reason:{commit_failed}"
                ),
            )
            return RenewResult(renewed=False, ttl=0, source=source, reason=commit_failed)

        self._log_sel(
            caller="safety_override",
            operation="safety_override:renew_lease",
            outcome="renewed",
            resources=f"source:{source}, granted:{granted}s",
        )
        return RenewResult(renewed=True, ttl=granted, source=source)

    def has_been_renewed(self) -> bool:
        """True when the current grant has already been renewed at least once.

        Lets a caller tell a grant's FIRST automatic lease from its later ones
        without tracking that itself — used to notify a human once per grant
        rather than once per lease.
        """
        with self._lock:
            return self._last_renewed_at > 0

    def lease_ceiling_remaining_secs(self) -> int:
        """Seconds left before the absolute automatic-extension ceiling.

        0 when there is nothing to extend or the ceiling has passed; -1 for a
        permanent grant, which has no ceiling.
        """
        with self._lock:
            if self._active and self._permanent:
                return -1
            if self._expires_at <= 0 or self._activated_at <= 0:
                return 0
            return max(0, int(self._lease_ceiling_locked() - time.monotonic()))

    def deactivate(self, source: str) -> None:
        """Deactivate the override immediately.  No-op if already inactive."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._permanent = False
            self._expires_at = 0.0

        self._log_sel(
            caller="safety_override",
            operation="safety_override:deactivate",
            outcome="disabled",
            resources=f"source:{source}",
        )

    # ── Task-scoped grants ───────────────────────────────────────────────────

    def activate_scoped(
        self, scope: str, source: str, ttl: Optional[int] = None
    ) -> ActivationResult:
        """Activate a narrow, TTL-bounded auto-approve grant for ``scope``.

        Unlike ``activate()`` this does NOT flip the session-wide override; it
        records an expiring grant for a single scope key (e.g. one task run).
        The activation is audited fail-closed to the SEL BEFORE it is committed,
        exactly like the global ``activate()``, so no grant exists without an
        audit trail. TTL defaults to the source's default and is capped at the
        24h hard ceiling.
        """
        if ttl is None:
            ttl = self._adhoc_ttl
        ttl = min(ttl, self._MAX_TTL)
        now_mono = time.monotonic()
        activated_at_iso = datetime.now(tz=timezone.utc).isoformat()

        # Fail-closed audit before commit — no grant without a trace.
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate_scoped",
                outcome="enabled",
                resources=f"scope:{scope}, source:{source}, ttl:{ttl}s",
                critical=True,
            )
        except Exception:
            logger.error(
                "SEL audit failed; refusing scoped safety override activation", exc_info=True
            )
            return ActivationResult(active=False, ttl=0, source=source, activated_at_iso="")

        with self._lock:
            self._scoped[scope] = (now_mono, now_mono + ttl)

        return ActivationResult(
            active=True, ttl=ttl, source=source, activated_at_iso=activated_at_iso
        )

    def renew_scoped(
        self, scope: str, source: str, ttl: Optional[int] = None
    ) -> RenewResult:
        """Slide a scoped grant's expiry forward on activity, capped at the ceiling.

        Extends the grant to ``min(now + ttl, activated_at + _MAX_TTL)`` so an
        actively-progressing run does not lose trust at the base TTL, while the
        absolute 24h hard ceiling from first activation is still honored (an
        abandoned run with no activity simply lapses). No-op / not-renewed if the
        grant is absent or the ceiling is already reached. Intentionally NOT
        SEL-logged per call — it extends an already-audited grant within its
        audited ceiling, and per-tool-call logging would flood the SEL.
        """
        if ttl is None:
            ttl = self._adhoc_ttl
        ttl = min(ttl, self._MAX_TTL)
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return RenewResult(renewed=False, ttl=0, source=source, reason="not_active")
            activated_at, _ = entry
            ceiling = activated_at + self._MAX_TTL
            if now_mono >= ceiling:
                return RenewResult(renewed=False, ttl=0, source=source, reason="ceiling_reached")
            new_expiry = min(now_mono + ttl, ceiling)
            self._scoped[scope] = (activated_at, new_expiry)
            remaining = max(0, int(new_expiry - now_mono))
        return RenewResult(renewed=True, ttl=remaining, source=source)

    def is_scope_active(self, scope: str) -> bool:
        """Return True if ``scope`` has a live (unexpired) grant.

        Expires the grant and logs a SEL event when its TTL has lapsed.
        """
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return False
            if now_mono < entry[1]:
                return True
            del self._scoped[scope]

        self._log_sel(
            caller="safety_override",
            operation="safety_override:scope_expired",
            outcome="expired",
            resources=f"scope:{scope}",
        )
        return False

    def deactivate_scope(self, scope: str) -> None:
        """Revoke a scoped grant immediately. No-op if absent."""
        with self._lock:
            existed = self._scoped.pop(scope, None) is not None
        if existed:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:deactivate_scope",
                outcome="disabled",
                resources=f"scope:{scope}",
            )

    def scope_remaining_secs(self, scope: str) -> int:
        """Return seconds remaining on a scoped grant, 0 if absent/expired.

        Pure read — does NOT expire or SEL-log a lapsed grant (that is the
        enforcement path's job via ``is_scope_active``), so a status/UI poll can
        never emit a ``scope_expired`` event or mutate state.
        """
        now_mono = time.monotonic()
        with self._lock:
            entry = self._scoped.get(scope)
            if entry is None:
                return 0
            return max(0, int(entry[1] - now_mono))

    def is_active(self) -> bool:
        """Return True if the override is currently active.

        Triggers expiry bookkeeping (callback + SEL log) when the TTL lapses.
        A DECLARED grant has no deadline, so it never reaches that path.
        """
        now_mono = time.monotonic()

        with self._lock:
            if not self._active:
                return False

            # Declared grants do not expire — the operator's config IS the
            # authority, and it is re-read on every startup.
            if self._permanent:
                return True

            if now_mono < self._expires_at:
                return True

            # TTL lapsed — expire now
            self._active = False
            expired_source = self._source

        # Callbacks and SEL logging happen outside the lock to avoid deadlocks.
        self._log_sel(
            caller="safety_override",
            operation="safety_override:expired",
            outcome="expired",
            resources=f"source:{expired_source}",
        )

        cb = self._on_expired
        if cb is not None:
            try:
                cb(expired_source)
            except Exception:
                logger.warning("on_expired callback raised", exc_info=True)

        # Re-read rather than returning a literal False. The callback runs
        # outside the lock precisely so it may act, and one legitimate action is
        # to renew the grant (``renew_lease``, for an unattended loop that is
        # still armed). Returning False unconditionally would report the grant
        # as gone to the very caller that triggered the lapse, while every later
        # caller saw it live — and that caller is frequently a tool-approval
        # check, so the visible symptom was one unexplained approval prompt
        # immediately after the boundary, then normal operation. That is a
        # near-unattributable flake, not a graceful degradation.
        #
        # Reading under the lock keeps this honest against a concurrent
        # deactivate: the answer is whatever the state says now, not what this
        # frame decided a moment ago.
        with self._lock:
            if not self._active:
                return False
            if self._permanent:
                return True
            return time.monotonic() < self._expires_at

    def remaining_secs(self) -> int:
        """Return seconds remaining; 0 if inactive, -1 if it never expires."""
        self.is_active()
        now_mono = time.monotonic()
        with self._lock:
            if not self._active:
                return 0
            if self._permanent:
                return -1
            remaining = self._expires_at - now_mono
            return max(0, int(remaining))

    def remaining_secs_passive(self) -> int:
        """Seconds remaining, WITHOUT triggering lazy expiry.

        ``remaining_secs`` calls ``is_active()`` first, which is the lazy-expiry
        trigger: it can fire ``on_expired``, and the dashboard's handler touches
        ``state`` (WebSocket sends, slot updates). That makes the plain accessor
        unsafe to call from a worker thread, where there is no running event loop —
        the sends fail, clients are dropped, and the expiry notice is lost.

        Callers off the event loop use this. Nothing is lost by not triggering
        expiry here: every other ``is_active()`` caller still does, so the
        callback fires from a thread that can service it.

        A grant already past its deadline reports 0 rather than a negative number,
        so a caller sees "no headroom" and can decide to renew — which
        ``renew_lease`` handles, bounded by the ceiling.
        """
        now_mono = time.monotonic()
        with self._lock:
            if not self._active:
                return 0
            if self._permanent:
                return -1
            return max(0, int(self._expires_at - now_mono))

    def log_lease_denied(self, source: str, reason: str) -> None:
        """Record a lease denial decided by a CALLER, not by this object.

        ``renew_lease`` audits every denial it makes itself; a caller that refuses
        to even ask — because policy forbids extending — is making the same kind of
        permission decision and it belongs in the same audit stream under the same
        operation name. Without this, "auto-approval stopped being extended because
        the operator disabled it" is the one such decision with no trace, and an
        auditor reconstructing why an unattended run stalled would find nothing.
        """
        self._log_sel(
            caller="safety_override",
            operation="safety_override:renew_lease",
            outcome="denied",
            resources=f"source:{source}, reason:{reason}",
        )

    def status(self) -> OverrideStatus:
        """Return a point-in-time status snapshot.

        Monotonic timestamps are converted to wall-clock ISO 8601 UTC by
        computing the offset from ``time.monotonic()`` to ``datetime.now()``.
        """
        self.is_active()

        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc).timestamp()

        with self._lock:
            permanent = bool(self._permanent)
            # A permanent grant is active regardless of the (unconsulted)
            # deadline — deriving ``active`` from ``_expires_at`` alone would
            # report it inactive once that finite placeholder passed.
            active = self._active and (permanent or self._expires_at > now_mono)
            source = self._source
            count = self._activation_count
            activated_at = self._activated_at
            expires_at = self._expires_at
            last_renewed_at = self._last_renewed_at
            last_renewed_by = self._last_renewed_by

        def _mono_to_iso(mono_ts: float) -> Optional[str]:
            if mono_ts <= 0.0:
                return None
            wall_ts = now_wall + (mono_ts - now_mono)
            return datetime.fromtimestamp(wall_ts, tz=timezone.utc).isoformat()

        remaining = 0
        if active:
            remaining = -1 if permanent else max(0, int(expires_at - now_mono))

        return OverrideStatus(
            active=active,
            source=source,
            remaining_secs=remaining,
            activation_count=count,
            activated_at_iso=_mono_to_iso(activated_at) if active else None,
            expires_at_iso=None if permanent else (_mono_to_iso(expires_at) if active else None),
            last_renewed_at_iso=_mono_to_iso(last_renewed_at),
            last_renewed_by=last_renewed_by,
            permanent=permanent and active,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _log_sel(
        self,
        *,
        caller: str,
        operation: str,
        outcome: str,
        resources: str = "",
        critical: bool = False,
    ) -> None:
        """Log a SEL event.

        When ``critical=True`` the exception is re-raised so the caller can
        enforce fail-closed behaviour (e.g. activation must roll back).
        Otherwise the failure is swallowed and only a warning is emitted.
        """
        try:
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome=outcome,
                source="safety_override",
                resources=resources,
                critical=critical,
            )
        except Exception:
            if critical:
                raise
            logger.warning("SEL log failed for %s/%s", operation, outcome, exc_info=True)


# ─── Module-level singleton ──────────────────────────────────────────────────

_singleton: Optional[SafetyOverride] = None
_singleton_lock = threading.Lock()


def safety_override() -> SafetyOverride:
    """Return the module-level singleton SafetyOverride instance."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SafetyOverride()
    return _singleton


def reset_singleton() -> None:
    """Reset the singleton.  Intended for use in tests only."""
    global _singleton
    with _singleton_lock:
        _singleton = None


_PERMANENT_MEMBER = "permanent"
_UNTIL_SHUTDOWN_MEMBER = "until_shutdown"
_GOVERNANCE_SCOPE = "yolo_duration"


def _duration_member_permitted(member: str) -> bool:
    """Ask the enterprise ceiling whether a duration member may be selected.

    Evaluated against the HOST profile (these are gateway-level decisions, not
    per-session ones) with ``fail_closed=True``, so a governance-evaluation error
    DENIES the riskier duration rather than silently granting it. With no policy
    configured — the standalone default — an ungoverned scope permits, so a solo
    operator's config is honoured.
    """
    # Deferred import: keeps this module free of a governance/config dependency
    # at import time (it is imported very early by the security/hook layers), so
    # no import cycle is possible regardless of which entrypoint loads first.
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )
    except Exception:
        logger.debug("governance layer unavailable; permitting %s", member, exc_info=True)
        return True
    decision = governance_permits(
        _GOVERNANCE_SCOPE,
        member,
        session_key=HOST_SESSION_KEY,
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


def declared_grant_permitted() -> bool:
    """True when policy allows a DECLARED grant to persist without expiry.

    ``dangerouslySkipPermissions: true`` is the operator's standing instruction,
    but on a managed fleet an admin must be able to forbid a never-expiring
    grant. Denying the ``permanent`` member of the ``yolo_duration`` scope forces
    a declared grant back onto the ordinary ad-hoc duration.
    """
    return _duration_member_permitted(_PERMANENT_MEMBER)


def until_shutdown_permitted() -> bool:
    """True when policy allows the ad-hoc ``until_shutdown`` duration."""
    return _duration_member_permitted(_UNTIL_SHUTDOWN_MEMBER)


def resolve_configured_duration() -> tuple[int, bool]:
    """``(ttl_secs, until_shutdown)`` from live config, with the policy clamp.

    Read at every ad-hoc activation, so a duration saved from Settings takes
    effect on the next activation rather than only after a restart.
    ``until_shutdown`` is clamped back to the default TTL when policy forbids it.
    """
    from kiro_crew.config.loader import (
        YOLO_UNTIL_SHUTDOWN,
        KiroCrewConfig,
        yolo_duration_to_secs,
    )

    label = KiroCrewConfig.load().agent.yolo_duration
    if label == YOLO_UNTIL_SHUTDOWN:
        if until_shutdown_permitted():
            return SafetyOverride._ADHOC_TTL_DEFAULT, True
        logger.info(
            "Enterprise policy forbids the until_shutdown auto-approve duration; "
            "using the default timed duration"
        )
        return SafetyOverride._ADHOC_TTL_DEFAULT, False
    return yolo_duration_to_secs(label), False


def install_duration_resolver() -> None:
    """Make ad-hoc activations read their duration from live config.

    Called from every entrypoint that can hand out an ad-hoc grant, so Slack, the
    dashboard and the API all agree — and so a duration change applies without a
    restart. Idempotent.
    """
    safety_override().duration_resolver = resolve_configured_duration


def apply_config_duration() -> int:
    """Seed the ad-hoc duration once and return the TTL (0 for until_shutdown).

    Kept for the startup log and for callers that want the value up front; the
    resolver installed by :func:`install_duration_resolver` is what keeps it
    current afterwards.
    """
    so = safety_override()
    install_duration_resolver()
    try:
        ttl, until_shutdown = resolve_configured_duration()
    except Exception:
        logger.warning("could not read agent.yolo_duration; using the default", exc_info=True)
        so.adhoc_until_shutdown = False
        so.adhoc_ttl = SafetyOverride._ADHOC_TTL_DEFAULT
        return so.adhoc_ttl
    so.adhoc_until_shutdown = until_shutdown
    so.adhoc_ttl = ttl
    return 0 if until_shutdown else ttl


def grant_declared_yolo() -> ActivationResult:
    """Install the operator's declared ``dangerouslySkipPermissions`` grant.

    Permanent when policy permits, otherwise clamped to the ad-hoc duration so
    the admin ceiling wins. Shared by the dashboard and Slack startup paths so a
    headless ``--slack-only`` gateway behaves identically to a full one.
    """
    apply_config_duration()
    so = safety_override()
    if declared_grant_permitted():
        return so.activate_declared()
    logger.info(
        "Enterprise policy forbids a never-expiring auto-approve grant; "
        "the declared grant falls back to the ad-hoc duration"
    )
    return so.activate(SafetyOverride._DECLARED_SOURCE)
