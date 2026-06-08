"""Pure filesystem path primitives for KiroClaw configuration.

This is a **leaf module**: it depends only on the standard library
(``os``, ``sys``, ``pathlib``, ``logging``) and imports nothing from
``kiro_claw``. Modules that only need to locate ``~/.kiroclaw/`` should import
from here directly::

    from kiro_claw.config.paths import config_dir

so they don't transitively pull in the full config loader (DTOs, schema
validation, the process-global cache, and the lazily-imported provider
factory) the way ``from kiro_claw.config.loader import config_dir`` does.

Only the genuinely pure primitives live here. The *dir-derived* helpers
(``config_path``, ``config_local_path``, ``workspace_root``, ``workspace_dir_for``,
``outbox_dir``, ``env_path``, …) remain in :mod:`kiro_claw.config.loader` so that
their ``config_dir()`` lookups resolve in the loader namespace — preserving the
``patch("kiro_claw.config.loader.config_dir", ...)`` test seam used across the
suite.

All names here are also re-exported from ``kiro_claw.config.loader`` for
backward compatibility, so existing callers continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".kiroclaw"
OUTBOX_DIR_NAME = "outbox"

# Cross-platform workspace root for LLM working directories.
# Override: KIROCLAW_WORKSPACE env var or ~/.kiroclaw/workspace_dir
# macOS: /Volumes/workplace/kiroclaw-workspace (fallback ~/workplace)
# Linux: ~/workplace/kiroclaw-workspace
_WORKSPACE_DIR_NAME = "kiroclaw-workspace"


def config_dir() -> Path:
    override = os.environ.get("KIROCLAW_HOME")
    if override:
        p = Path(override).expanduser().resolve()
        # Refuse root or system directories as config home
        if p == Path("/") or p.parts[:2] in (("/", "usr"), ("/", "System"), ("/", "etc")):
            logger.warning("KIROCLAW_HOME=%s is a system directory, ignoring", override)
        else:
            p.mkdir(parents=True, exist_ok=True)
            return p
    d = Path.home() / CONFIG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_package_dir() -> Path:
    """Return the installed ``kiro_claw/config/`` directory.

    This is the source of truth for bundled config data files (``defaults.json``,
    ``prompt.md``, persona/orchestrator prompts). ``paths.py`` lives directly in
    the config package, so this is simply its parent directory.
    """
    return Path(__file__).resolve().parent


def _default_workspace_base() -> Path:
    """Return the platform-specific default base for the workspace."""
    if sys.platform == "darwin":
        vol = Path("/Volumes/workplace")
        return vol if vol.is_dir() else Path.home() / "workplace"
    return Path.home() / "workplace"


def _safe_dir_name(key: str) -> str:
    """Sanitize a session key into a safe directory name."""
    return key.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")
