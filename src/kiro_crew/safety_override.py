"""Time-limited safety override — replaces permanent YOLO mode.

Provides a ``SafetyOverride`` class that can be activated for a bounded TTL
(default per-source) and automatically expires.  A 5-minute grace window
after expiry allows renew() to reactivate without a full re-activation flow.

Sources and default TTLs:
- slack     → 30 min
- dashboard → 6 h
- config    → 24 h  (startup only)

Hard ceiling: 24 h regardless of requested TTL.

When ``duration_mode`` is set to ``"until_shutdown"`` (from ``agent.yolo_duration``
in config), activations from the ``dashboard`` and ``config`` sources are granted
with NO expiry — they stay active until the gateway process stops, then clear (the
state is in-memory, so nothing survives a restart). Slack activations are never
affected by ``duration_mode``: ``!yolo on`` always keeps its 30-min TTL, because
the short Slack TTL is itself a safety property against a remote surface pinning
unbounded auto-approve.

All state changes are logged to the Security Event Log (SEL).
"""

from __future__ import annotations

import logging
import math
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
    remaining_secs: int  # -1 when the grant has no expiry (until_shutdown)
    activation_count: int
    activated_at_iso: Optional[str]  # None when inactive
    expires_at_iso: Optional[str]  # None when inactive OR until_shutdown (no wall-clock expiry)
    last_renewed_at_iso: Optional[str]  # None if never renewed
    last_renewed_by: str
    until_shutdown: bool = False  # True when the active grant has no expiry


# ─── Core class ──────────────────────────────────────────────────────────────


class SafetyOverride:
    """Time-limited safety override with SEL audit trail.

    All public methods are thread-safe.
    """

    # ── Constants ────────────────────────────────────────────────────────────

    _MAX_TTL: int = 86400  # 24 h hard ceiling
    _SLACK_TTL: int = 1800  # 30 min
    _DASHBOARD_TTL: int = 21600  # 6 h
    _CONFIG_TTL: int = 86400  # 24 h (config-triggered startup)
    _RENEW_GRACE_SECS: int = 300  # 5-min grace window after expiry

    _SOURCE_TTLS: dict[str, int] = {
        "slack": _SLACK_TTL,
        "dashboard": _DASHBOARD_TTL,
        "config": _CONFIG_TTL,
    }

    # Sources whose default-TTL activations honor duration_mode="until_shutdown".
    # Slack is deliberately excluded: its short TTL guards against a remote
    # surface pinning unbounded auto-approve, so `!yolo on` always stays finite.
    _UNTIL_SHUTDOWN_SOURCES: frozenset[str] = frozenset({"dashboard", "config"})

    # Class-level default lock for instances created via object.__new__() (e.g. tests).
    # Each real instance gets its own lock in __init__; this is just a safe fallback.
    _lock: threading.Lock

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._source: str = ""
        self._activated_at: float = 0.0
        self._expires_at: float = 0.0
        self._until_shutdown: bool = False  # True when the live grant has no expiry
        # Duration policy for dashboard/config activations: "default" (tiered
        # per-source TTLs) or "until_shutdown" (no expiry until the process stops).
        # Set from agent.yolo_duration by whoever wires the singleton; kept here
        # (not read from config) to keep this module config-agnostic and pure.
        self._duration_mode: str = "default"
        self._activation_count: int = 0
        self._last_renewed_at: float = 0.0
        self._last_renewed_by: str = ""
        self._on_expired: Optional[Callable[[str], None]] = None
        self._on_activated: Optional[Callable[[str, int], None]] = None
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
        if name == "_scoped":
            scoped: dict[str, tuple[float, float]] = {}
            object.__setattr__(self, "_scoped", scoped)
            return scoped
        if name == "_until_shutdown":
            object.__setattr__(self, "_until_shutdown", False)
            return False
        if name == "_duration_mode":
            object.__setattr__(self, "_duration_mode", "default")
            return "default"
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

    # ── Duration policy ──────────────────────────────────────────────────────

    @property
    def duration_mode(self) -> str:
        """Current duration policy: ``"default"`` or ``"until_shutdown"``."""
        return self._duration_mode

    @duration_mode.setter
    def duration_mode(self, value: str) -> None:
        """Set the duration policy. Unknown values fall back to ``"default"``.

        Does NOT retroactively change an already-active grant — it governs the
        next activation. This mirrors config: flipping the setting takes effect
        the next time YOLO is turned on.
        """
        self._duration_mode = value if value in ("default", "until_shutdown") else "default"

    # ── Public API ───────────────────────────────────────────────────────────

    def activate(self, source: str, ttl: Optional[int] = None) -> ActivationResult:
        """Activate the override for the given source.

        Args:
            source: Trigger source (``slack``, ``dashboard``, ``config``, …).
            ttl: Override TTL in seconds.  Defaults to the source's default TTL.
                 Capped at ``_MAX_TTL``.  When ``duration_mode`` is
                 ``"until_shutdown"`` AND ``source`` is dashboard/config AND no
                 explicit ``ttl`` was passed, the grant is given NO expiry (it
                 lasts until the process stops); the reported ``ttl`` is ``-1``.

        Returns:
            ActivationResult with effective TTL and wall-clock activation time.
        """
        # until_shutdown only applies to a source's DEFAULT TTL (no explicit ttl),
        # for the eligible sources, when the policy is set. An explicit ttl (e.g.
        # Slack's) always takes the normal capped path.
        until_shutdown = (
            ttl is None
            and self._duration_mode == "until_shutdown"
            and source in self._UNTIL_SHUTDOWN_SOURCES
        )
        if ttl is None:
            ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
        ttl = min(ttl, self._MAX_TTL)
        report_ttl = -1 if until_shutdown else ttl
        ttl_label = "until_shutdown" if until_shutdown else f"{ttl}s"

        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc)
        activated_at_iso = now_wall.isoformat()

        # Snapshot state under lock for reactivation check
        with self._lock:
            was_active = self._active
            prev_source = self._source
            if not self._active:
                prev_remaining = 0
            elif self._until_shutdown:
                prev_remaining = -1  # no-expiry grant — avoid int(inf) overflow
            else:
                prev_remaining = max(0, int(self._expires_at - now_mono))
        prev_remaining_label = "until_shutdown" if prev_remaining == -1 else f"{prev_remaining}s"

        # Audit BEFORE committing — fail-closed with no race window
        try:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:activate",
                outcome="enabled",
                resources=f"source:{source}, ttl:{ttl_label}",
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
                resources=f"prev_source:{prev_source}, prev_remaining:{prev_remaining_label}, new_source:{source}, new_ttl:{ttl_label}",
            )

        # Only commit after audit succeeds
        with self._lock:
            self._active = True
            self._source = source
            self._activated_at = now_mono
            self._expires_at = math.inf if until_shutdown else now_mono + ttl
            self._until_shutdown = until_shutdown
            self._activation_count += 1
            self._last_renewed_at = 0.0
            self._last_renewed_by = ""

        cb = self._on_activated
        if cb is not None:
            try:
                cb(source, report_ttl)
            except Exception:
                logger.warning("on_activated callback raised", exc_info=True)

        return ActivationResult(
            active=True,
            ttl=report_ttl,
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
        kept_until_shutdown = False

        denied = False
        with self._lock:
            currently_active = self._active and self._expires_at > now_mono
            if currently_active and self._until_shutdown:
                # No-expiry grant: renewing must NOT downgrade it to a finite
                # TTL. Treat as a no-op success that keeps it active.
                kept_until_shutdown = True
                self._last_renewed_at = now_mono
                self._last_renewed_by = source
            elif currently_active or (
                self._expires_at > 0
                and (now_mono - self._expires_at) <= self._RENEW_GRACE_SECS
            ):
                ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
                ttl = min(ttl, self._MAX_TTL)
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

        if kept_until_shutdown:
            self._log_sel(
                caller="safety_override",
                operation="safety_override:renew",
                outcome="renewed",
                resources=f"source:{source}, new_ttl:until_shutdown",
            )
            return RenewResult(renewed=True, ttl=-1, source=source)

        self._log_sel(
            caller="safety_override",
            operation="safety_override:renew",
            outcome="renewed",
            resources=f"source:{source}, new_ttl:{ttl}s",
        )
        return RenewResult(renewed=True, ttl=ttl, source=source)

    def deactivate(self, source: str) -> None:
        """Deactivate the override immediately.  No-op if already inactive."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._expires_at = 0.0
            self._until_shutdown = False

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
            ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
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
            ttl = self._SOURCE_TTLS.get(source, self._SLACK_TTL)
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
        """
        now_mono = time.monotonic()

        with self._lock:
            if not self._active:
                return False

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

        return False

    def remaining_secs(self) -> int:
        """Return seconds remaining, 0 if inactive/expired, -1 if no expiry.

        ``-1`` signals an ``until_shutdown`` grant that stays active until the
        process stops (there is no finite remaining time to report).
        """
        self.is_active()
        now_mono = time.monotonic()
        with self._lock:
            if not self._active:
                return 0
            if self._until_shutdown:
                return -1
            remaining = self._expires_at - now_mono
            return max(0, int(remaining))

    def status(self) -> OverrideStatus:
        """Return a point-in-time status snapshot.

        Monotonic timestamps are converted to wall-clock ISO 8601 UTC by
        computing the offset from ``time.monotonic()`` to ``datetime.now()``.
        """
        self.is_active()

        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc).timestamp()

        with self._lock:
            active = self._active and self._expires_at > now_mono
            source = self._source
            count = self._activation_count
            activated_at = self._activated_at
            expires_at = self._expires_at
            until_shutdown = self._until_shutdown
            last_renewed_at = self._last_renewed_at
            last_renewed_by = self._last_renewed_by

        def _mono_to_iso(mono_ts: float) -> Optional[str]:
            if mono_ts <= 0.0 or not math.isfinite(mono_ts):
                return None
            wall_ts = now_wall + (mono_ts - now_mono)
            return datetime.fromtimestamp(wall_ts, tz=timezone.utc).isoformat()

        # -1 for a no-expiry grant (until_shutdown); a finite countdown otherwise.
        remaining = 0
        if active:
            remaining = -1 if until_shutdown else max(0, int(expires_at - now_mono))

        return OverrideStatus(
            active=active,
            source=source,
            remaining_secs=remaining,
            activation_count=count,
            activated_at_iso=_mono_to_iso(activated_at) if active else None,
            # No wall-clock expiry exists for an until_shutdown grant.
            expires_at_iso=(None if until_shutdown else _mono_to_iso(expires_at)) if active else None,
            last_renewed_at_iso=_mono_to_iso(last_renewed_at),
            last_renewed_by=last_renewed_by,
            until_shutdown=until_shutdown and active,
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


# ─── Admin governance shim ───────────────────────────────────────────────────


def governed_duration_mode(configured: str, *, session_key: str = "") -> str:
    """Clamp a configured YOLO duration to what enterprise governance permits.

    This is the admin control seam. The ``yolo_duration`` governed scope lets an
    enterprise POLICY (or host PROFILE) deny the ``"until_shutdown"`` member —
    e.g. ``{"yolo_duration": {"mode": "deny", "deny": ["until_shutdown"]}}`` —
    in which case a configured ``"until_shutdown"`` is downgraded to
    ``"default"`` so no-expiry YOLO can never be chosen. Enforcement lives HERE,
    at the source that sets ``duration_mode``, not merely in the UI: hiding the
    option in the dashboard is cosmetic; this makes the restriction real even if
    the config file or an API caller asks for ``until_shutdown``.

    With no governing policy (the standalone default) the value passes through
    unchanged. Governance-evaluation errors fail CLOSED (downgrade to
    ``"default"``): this is a restriction, so an indeterminate ceiling must not
    hand out the stronger no-expiry grant.

    ``session_key`` selects the active governance profile. It defaults to the
    HOST profile (``HOST_SESSION_KEY``) because YOLO duration is a gateway-wide
    decision — an empty/session key must NOT let a per-session profile decide
    whether the stronger no-expiry grant is allowed (that would let a narrower
    surface widen a host ceiling). Callers may pass an explicit key to scope it.
    """
    if configured != "until_shutdown":
        return "default"
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            governance_permits,
        )

        decision = governance_permits(
            "yolo_duration",
            "until_shutdown",
            session_key=session_key or HOST_SESSION_KEY,
            log_warning=False,
            fail_closed=True,
        )
        permitted = bool(getattr(decision, "permitted", True))
    except Exception:
        logger.warning(
            "YOLO duration governance check failed; downgrading to 'default'", exc_info=True
        )
        return "default"
    return "until_shutdown" if permitted else "default"
