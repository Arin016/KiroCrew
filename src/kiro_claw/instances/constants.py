"""Tunable constants for the Instances feature.

Isolated in this module so resource limits and defaults can be adjusted in one
place without hunting through the registry / tunnel-manager code.

These values are the *defaults* for the corresponding ``InstancesConfig`` fields
in ``kiro_claw.config.loader``; a user can override them via
``kiroclaw config set instances.<key> <value>``. Keeping the canonical default
here (and referencing it from the dataclass) means the constant and the config
default can never drift apart.
"""

from __future__ import annotations

# Maximum number of remote instances kept "warm" (iframe mounted + tunnel +
# WebSocket live) at once. Each warm instance is a full dashboard SPA, so this
# bounds memory/socket usage; least-recently-used instances beyond the cap are
# lazily evicted and reconnected on demand.
DEFAULT_WARM_SET_CAP: int = 5

# First local loopback port handed out for an SSH ``-L`` forward. The port
# allocator increments from here, skipping ports already in use. Chosen to sit
# just above the default dashboard port (7777).
DEFAULT_TUNNEL_BASE_PORT: int = 7778

# Health-probe cadence/threshold for a connected tunnel. Poll every interval,
# and after this many *consecutive* failures treat the tunnel as unhealthy
# (Stage 2 self-heal hooks the existing exit seam). interval <= 0 disables the
# probe.
DEFAULT_PROBE_INTERVAL_SECS: int = 30
DEFAULT_PROBE_FAILURE_THRESHOLD: int = 3

# Max consecutive self-heal attempts before giving up on an unhealthy tunnel
# (2-tier recovery). Reset to 0 once a rebuild succeeds, so a tunnel that
# flaps-then-recovers isn't permanently capped. Bounds the probe->teardown->
# recover->probe loop so a persistently-broken host can't churn forever.
DEFAULT_MAX_RECOVERY_ATTEMPTS: int = 3

# Proactively re-mint each instance's dashboard token at this fraction of its
# TTL, before the 20h cap. 0.8 = refresh at 80% elapsed.
DEFAULT_TOKEN_REFRESH_FRACTION: float = 0.8

# Timeout (secs) for the loopback liveness probe that validates a *stored* token
# before the API hands it to the browser on (re)connect. A stored token can go
# stale while the tunnel stays CONNECTED (a failed self-heal re-mint, or a remote
# `kiroclaw restart` that invalidates tokens); an iframe loaded with a stale
# token gets a server-rendered 403 page, so the SPA never boots to fire the
# reactive `mc-auth-expired` recovery. The probe (GET /api/status?token=... over
# the existing tunnel — no SSH) closes that initial-load gap. It is
# deny-by-default: anything but a positive 2xx (including a timeout/connection
# error) is treated as invalid and forces a fresh mint; if that mint also fails
# the link is genuinely down and the caller returns an error rather than serving
# an unconfirmed token. Kept tight so a tab activation never blocks perceptibly.
DEFAULT_TOKEN_PROBE_TIMEOUT_SECS: float = 2.0
