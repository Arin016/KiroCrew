"""Profile resolution — decides which edition loads at boot.

The profile is a *load trigger*, not a security decision: capability comes from
the installed companion package, not from the profile claim.  A forged signal at
worst loads a stricter posture on a host that has nothing to enforce it.

Precedence (first match wins):
  1. ``KIROCLAW_PROFILE`` env var (explicit operator/dev override).
  2. A non-empty ``kiroclaw.plugins`` entry-point group (companion installed) —
     the cheap, authoritative signal: capability comes from the installed
     companion, so its presence is what actually matters.
  3. Identity signal: a present ``~/.midway`` directory.  A cheap filesystem
     stat (no subprocess) that flags an Amazon host which has NOT installed the
     companion, so discovery fails closed instead of running open defaults.
  4. Otherwise ``standalone``.

Note: the core does NOT spawn ``kiro-cli whoami`` to read the SSO issuer — that
added a blocking subprocess to every standalone boot and baked an Amazon-only
string into the open-source core.  Entry-point presence + the ``~/.midway`` stat
cover the trigger cases; the companion's own identity provider refines the
principal once loaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from kiro_claw.platform.context import PROFILE_AMAZON, PROFILE_STANDALONE

if TYPE_CHECKING:
    from kiro_claw.config.loader import KiroClawConfig

logger = logging.getLogger(__name__)

_VALID_PROFILES = frozenset({PROFILE_STANDALONE, PROFILE_AMAZON})


def resolve_profile(cfg: "KiroClawConfig", *, entry_points: "Sequence[object]") -> str:
    """Resolve the active profile.  See module docstring for precedence."""
    # 1. Explicit env override.
    env = os.environ.get("KIROCLAW_PROFILE", "").strip().lower()
    if env in _VALID_PROFILES:
        return env
    if env:
        logger.warning("Unknown KIROCLAW_PROFILE=%r; falling back to standalone", env)
        return PROFILE_STANDALONE

    # 2. Companion installed (cheap, authoritative — no subprocess, no marker).
    if entry_points:
        return PROFILE_AMAZON

    # 3. Identity signal — a cheap ``~/.midway`` stat (no subprocess).  Flags an
    #    Amazon host without the companion so discovery fails closed.
    try:
        if (Path.home() / ".midway").exists():
            return PROFILE_AMAZON
    except Exception:
        logger.debug("home/.midway probe failed", exc_info=True)

    # 4. Default.
    return PROFILE_STANDALONE
