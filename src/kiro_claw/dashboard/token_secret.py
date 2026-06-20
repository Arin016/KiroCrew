"""Persistent HMAC signing secret for dashboard auth tokens.

Extracted from ``token_auth.py`` so both the token-auth module and the
refresh-token module can import the secret without creating a circular
dependency between them. The secret is shared across both modules.

The secret is stored at ``<config_dir>/token_signing.key`` (owner-only
0600). Persistence is required for correctness: tokens and session
cookies are HMAC-signed with this key, so a fresh random secret on every
process start would invalidate every outstanding Slack link and cookie,
locking users out after any gateway restart.

Loading is LAZY (``_get_secret()``), NOT a module-level call: merely
*importing* this module must not write ``token_signing.key`` into
``$KIROCLAW_HOME``. The CLI imports token_auth (and thus this module)
transitively for every ``kiroclaw`` subcommand, so an import-time write
(a) breaks ``gateway --seed`` — which requires an empty target home and
refuses a non-empty one — and (b) pollutes the home for read-only
commands like ``kiroclaw --help``.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_SECRET_KEY_FILE = "token_signing.key"


def _load_or_create_secret() -> bytes:
    """Return the HMAC signing secret, persisted across restarts.

    See module docstring for the persistence rationale. Falls back to an
    ephemeral secret if the key file is unwritable — tokens still work
    within this process; they just won't survive a restart (the
    pre-existing behaviour).
    """
    # Local import: config.loader pulls in modules that import token_auth
    # (which re-exports this module), so a top-level import here risks a
    # circular import. Matches the other config_dir() call sites in the
    # dashboard auth modules.
    from kiro_claw.config.loader import config_dir

    try:
        key_path = config_dir() / _SECRET_KEY_FILE
        if key_path.exists():
            existing = key_path.read_bytes()
            if len(existing) >= 32:
                # Re-enforce 0600 at load time, not just at creation: perms may
                # have been relaxed since (backup restore, manual edit, migration)
                # and this key signs all auth tokens/cookies.
                try:
                    os.chmod(key_path, 0o600)
                except OSError:
                    logger.warning(
                        "failed to enforce 0600 permissions on token signing key %s; "
                        "file may be readable by other users",
                        key_path,
                        exc_info=True,
                    )
                return existing
        key = os.urandom(32)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            # Security-sensitive: this key signs all auth tokens/cookies, so a
            # world-readable key file is a real exposure. Warn loudly rather
            # than failing — the secret still works for signing this session.
            logger.warning(
                "failed to set 0600 permissions on token signing key %s; "
                "file may be readable by other users",
                key_path,
                exc_info=True,
            )
        return key
    except OSError:
        # Fall back to an ephemeral secret if the key file is unwritable.
        logger.warning("token signing key not persisted; using ephemeral secret", exc_info=True)
        return os.urandom(32)


_SECRET: bytes | None = None
_SECRET_LOCK = threading.Lock()


def _get_secret() -> bytes:
    """Return the HMAC signing secret, loading/creating it on first use.

    Lazy (NOT a module-level call) so that merely *importing* this module
    does not write ``token_signing.key`` into ``$KIROCLAW_HOME`` — see the
    module docstring. Memoized under a lock so the key is loaded exactly
    once even under concurrent first use.
    """
    global _SECRET
    if _SECRET is None:
        with _SECRET_LOCK:
            if _SECRET is None:
                _SECRET = _load_or_create_secret()
    return _SECRET
