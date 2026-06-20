"""Dashboard token authentication.

HMAC-SHA256 token generation, validation, IP binding, consumption
tracking, and aiohttp middleware for Slack-gated dashboard access.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from aiohttp import web

from kiro_claw.dashboard.origin import is_loopback
from kiro_claw.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    REFRESH_COOKIE_PATH,
    generate_refresh_token,
    refresh_cookie_name,
)

# Canonical HMAC-secret definitions live in token_secret to break the import
# cycle between token_auth and refresh_tokens. Re-exported here for backwards
# compatibility — callers elsewhere import these names from token_auth. The
# fork keeps the LAZY _get_secret() (NOT an eager module-level _SECRET =
# _load_or_create_secret()) so that merely importing this module never writes
# token_signing.key into $KIROCLAW_HOME (the CLI imports token_auth for every
# kiroclaw subcommand; an import-time write would break gateway --seed and
# pollute the home for read-only commands).
from kiro_claw.dashboard.token_secret import (  # noqa: F401  # re-exports
    _SECRET_KEY_FILE,
    _get_secret,
    _load_or_create_secret,
)
from kiro_claw.sel import sel as _sel_fn

logger = logging.getLogger(__name__)


_REVOCATION_FILE = "token_revocation.gen"


def _load_revocation_gen() -> int:
    """Return the persisted revocation generation counter (0 if unset).

    Every minted token embeds the current ``gen``; cookie validation rejects a
    token whose ``gen`` is below the current value. ``revoke_all_sessions()``
    bumps and persists it, so an operator ``kiroclaw logout`` invalidates ALL
    outstanding tokens/cookies — including established browser cookies, which
    the nonce store (per-process, cleared on restart) could not. Persisting the
    counter is what lets it survive a gateway restart WITHOUT logging users out:
    the gen is reloaded unchanged, so previously-issued cookies still match.
    """
    from kiro_claw.config.loader import config_dir

    try:
        p = config_dir() / _REVOCATION_FILE
        if p.exists():
            return int(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        logger.warning("could not read token revocation counter; assuming 0", exc_info=True)
    return 0


def _bump_revocation_gen() -> int:
    """Increment and persist the revocation counter. Returns the new value.

    Falls back to an in-memory bump if the file is unwritable (revocation still
    holds for the life of this process, the pre-existing best-effort behaviour).
    """
    global _REVOCATION_GEN
    _REVOCATION_GEN += 1
    from kiro_claw.config.loader import config_dir

    try:
        p = config_dir() / _REVOCATION_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(_REVOCATION_GEN), encoding="utf-8")
    except OSError:
        logger.warning("could not persist token revocation counter", exc_info=True)
    return _REVOCATION_GEN


_REVOCATION_GEN = _load_revocation_gen()


class TokenStateManager:
    """Thread-safe manager for token authentication state.

    Encapsulates all mutable token state (nonces, IP bindings, consumption)
    with consistent locking. Uses OrderedDict for O(1) nonce eviction.

    Threading model: This class uses threading.Lock (not asyncio.Lock) because
    token operations are called from both async contexts (aiohttp middleware)
    and sync contexts (CLI commands like `kiroclaw token`). The lock hold time
    is minimal (dict operations only), so blocking the event loop is negligible.
    """

    def __init__(self, max_concurrent_nonces: int = 50) -> None:
        self._lock = threading.Lock()
        self._max_nonces = max_concurrent_nonces
        # OrderedDict maintains insertion order for O(1) oldest eviction
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._ip_bindings: dict[str, tuple[str, float]] = {}  # token → (ip, exp)
        self._consumed: dict[str, float] = {}  # token → exp

    def register_nonce(self, nonce: str, expiry: float) -> str | None:
        """Register a nonce with its expiry time, evicting oldest if over limit."""
        with self._lock:
            self._nonces[nonce] = expiry
            self._nonces.move_to_end(nonce)  # Most recent at end
            if len(self._nonces) > self._max_nonces:
                evicted, _ = self._nonces.popitem(last=False)
                return evicted
            return None

    def is_nonce_valid(self, nonce: str) -> tuple[bool, str]:
        """Check if nonce is valid. Returns (valid, reason).

        Deny-by-default: rejects if no nonces registered or nonce not in set.
        Refreshes the nonce's eviction position on each successful check so
        that actively-used sessions are not evicted by newer token grants.
        """
        with self._lock:
            if not self._nonces:
                return False, "no active sessions"
            if nonce not in self._nonces:
                return False, "token superseded"
            self._nonces.move_to_end(nonce)
            return True, ""

    def bind_ip(self, token: str, ip: str, session_exp: float) -> None:
        """Bind a token to a client IP address."""
        with self._lock:
            self._ip_bindings[token] = (ip, session_exp)

    def check_ip(self, token: str, ip: str) -> bool:
        """Check if token is bound to the given IP (or unbound)."""
        with self._lock:
            entry = self._ip_bindings.get(token)
            return entry is None or entry[0] == ip

    def mark_consumed(self, token: str, session_exp: float) -> None:
        """Mark a token as consumed (used for one-time token patterns)."""
        with self._lock:
            self._consumed[token] = session_exp

    def is_consumed(self, token: str) -> bool:
        """Check if a token has been consumed."""
        with self._lock:
            return token in self._consumed

    def try_consume(self, token: str, session_exp: float) -> bool:
        """Atomically mark token consumed if not already.

        Returns True if this call consumed it, False if already consumed.
        """
        with self._lock:
            if token in self._consumed:
                return False
            self._consumed[token] = session_exp
            return True

    def evict_expired(self, now: float) -> None:
        """Remove all expired entries from all state stores."""
        with self._lock:
            # Evict expired IP bindings
            expired_tokens = [t for t, (_, exp) in self._ip_bindings.items() if exp < now]
            for t in expired_tokens:
                self._ip_bindings.pop(t, None)
            # Evict consumed tokens independently using their own expiry
            expired_consumed = [t for t, exp in self._consumed.items() if exp < now]
            for t in expired_consumed:
                self._consumed.pop(t, None)
            # Evict expired nonces
            expired_nonces = [n for n, exp in self._nonces.items() if exp < now]
            for n in expired_nonces:
                self._nonces.pop(n, None)

    def clear_all(self) -> None:
        """Clear all token state (nonces, IP bindings, consumed tokens)."""
        with self._lock:
            self._nonces.clear()
            self._ip_bindings.clear()
            self._consumed.clear()


# Maximum concurrent valid tokens before oldest is evicted.
# Raised from 5 to 50 so pending Slack challenge links aren't evicted
# by other token minting activity (crons, dashboard links, etc.).
MAX_CONCURRENT_NONCES = 50

# Module-level singleton instance
_state: TokenStateManager = TokenStateManager(max_concurrent_nonces=MAX_CONCURRENT_NONCES)

# Public static-asset prefixes exempt from token auth (GET of non-secret files
# the dashboard HTML references before the auth cookie is established).
# /fonts/ holds the self-hosted AWS Diatype woff2 files (public.html @font-face
# url('/fonts/...')); without the exemption the auth middleware 403s each font
# request and the browser, parsing the 403 HTML body as a font, logs
# "invalid sfntVersion" and falls back to a default typeface.
_BYPASS_PREFIXES = ("/assets/", "/static/", "/fonts/")
_BYPASS_EXACT = {"/logo.png", "/manifest.json", "/sw.js", "/pcm-worklet.js", "/api/token/local", "/api/shutdown"}

# Anchored bypass for installed-app static UI bundles only (federated-app
# RFC §3.8). Matches /apps/{name}/ui/<anything>, where {name} is the
# canonical app-name pattern. Must NOT match /apps/{name}/api/... — that
# path is the gateway-authenticated reverse proxy to the app backend
# (handle_app_api_proxy in kiro_claw/apps/routes.py) and continues to
# require a valid token. The bounded character class prevents ReDoS.
_APPS_UI_BYPASS_RE = re.compile(r"^/apps/[a-z0-9][a-z0-9_-]*/ui/")

# Link click window — URL must be opened within this time
LINK_WINDOW_SECS = 300  # 5 minutes
# Maximum session TTL — cookie cannot exceed this
MAX_SESSION_TTL_SECS = 20 * 3600  # 20 hours

_403_HTML = (
    "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' "
    "content='width=device-width,initial-scale=1'><title>Access Denied</title>"
    "<style>"
    "*{{margin:0;padding:0;box-sizing:border-box}}"
    "body{{font-family:system-ui,-apple-system,sans-serif;display:flex;"
    "align-items:center;justify-content:center;height:100vh;"
    "background:#f8fafc;color:#1e293b}}"
    ".c{{text-align:center;max-width:420px;padding:24px}}"
    ".logo{{font-size:48px;margin-bottom:16px}}"
    "h1{{font-size:20px;margin-bottom:8px}}"
    "p{{color:#64748b;font-size:13px;line-height:1.6;margin-bottom:16px}}"
    "code{{background:#e2e8f0;padding:2px 6px;border-radius:4px;color:#c2410c;"
    "font-size:13px}}"
    "input{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #cbd5e1;"
    "background:#fff;color:#1e293b;font-size:14px;margin-bottom:10px;outline:none}}"
    "input:focus{{border-color:#f97316}}"
    "button{{padding:8px 24px;border-radius:8px;border:none;cursor:pointer;"
    "background:#f97316;color:#fff;font-size:14px;font-weight:600}}"
    "button:hover{{background:#ea580c}}"
    ".err{{color:#dc2626;font-size:12px;margin-top:8px;display:none}}"
    "@media(prefers-color-scheme:dark){{body{{background:#0f1117;color:#e2e8f0}}"
    "p{{color:#94a3b8}}code{{background:#1e293b;color:#f97316}}"
    "input{{border-color:#334155;background:#1e293b;color:#e2e8f0}}"
    ".err{{color:#ef4444}}}}"
    "</style></head><body>"
    "<div class='c'>"
    "<div class='logo'>🐾</div>"
    "<h1>403 — {reason}</h1>"
    "<p>Run <code>kiroclaw token</code> in your terminal, then paste the URL below.</p>"
    "<input id='u' type='text' placeholder='Paste token URL or raw token…' autofocus>"
    "<button onclick='go()'>Connect</button>"
    "<div class='err' id='e'>Invalid URL</div>"
    "</div>"
    "<script>"
    "function go(){{var v=document.getElementById('u').value.trim();if(!v)return;"
    "var t;try{{var u=new URL(v);t=u.searchParams.get('token')}}"
    "catch(_){{t=v}}if(t){{window.location.href="
    "window.location.protocol+'//'+window.location.host+'?token='+encodeURIComponent(t)}}"
    "else{{document.getElementById('e').style.display='block'}}}}"
    "document.getElementById('u').addEventListener('keydown',"
    "function(e){{if(e.key==='Enter')go()}});"
    "</script>"
    "</body></html>"
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def _sign(payload: bytes) -> str:
    return _b64url_encode(hmac.new(_get_secret(), payload, hashlib.sha256).digest())


def generate_token(
    user_id: str,
    ttl_seconds: int = 3600,
    *,
    app: str = "",
    prompt: str = "",
    extra: dict[str, str] | None = None,
) -> str:
    """Return ``base64url(payload).base64url(signature)``.

    The token carries two expiry times:
    - ``exp``: link click window (5 minutes) — URL must be opened before this
    - ``session_exp``: cookie session TTL (capped at 20 hours)

    When *app* is provided, the token payload includes ``"app": app`` so
    downstream middleware can extract the verified app identity.

    When *prompt* is provided, it is included in the signed payload so the
    dashboard can auto-submit the user's original Slack message. The prompt
    is covered by the HMAC signature to prevent tampering.

    *extra* adds further string claims to the signed payload — used by the
    Slack challenge-and-redirect flow to carry ``channel``, ``thread_ts`` and
    an existing linked ``session_key`` so the dashboard can reconnect to (or
    auto-link) the correct Slack-linked session instead of always spawning a
    fresh, disconnected one. Reserved keys (sub/exp/session_exp/iat/nonce/app/
    prompt) cannot be overridden.

    Up to ``_MAX_CONCURRENT_NONCES`` tokens can be valid concurrently.
    When the limit is exceeded, the oldest nonce is evicted (O(1) via OrderedDict).
    """
    _evict_expired()
    now = time.time()
    nonce = os.urandom(8).hex()
    session_ttl = min(ttl_seconds, MAX_SESSION_TTL_SECS)

    evicted = _state.register_nonce(nonce, now + session_ttl)
    if evicted:
        _sel_fn().log_api_access(
            caller=user_id,
            operation="nonce_evicted",
            outcome="ok",
            source="token_auth",
            resources=f"evicted_nonce={evicted}",
        )

    payload_dict: dict[str, object] = {
        "sub": user_id,
        "exp": now + LINK_WINDOW_SECS,
        "session_exp": now + session_ttl,
        "iat": now,
        "nonce": nonce,
        # Revocation generation: validate_token rejects a token whose gen is
        # below the current persisted value, so revoke_all_sessions() kills
        # established cookies (not just the per-process nonce store).
        "gen": _REVOCATION_GEN,
    }
    if app:
        payload_dict["app"] = app
    if prompt:
        payload_dict["prompt"] = prompt
    if extra:
        _reserved = {"sub", "exp", "session_exp", "iat", "nonce", "gen", "app", "prompt"}
        for k, v in extra.items():
            if k not in _reserved and isinstance(v, str) and v:
                payload_dict[k] = v
    payload = json.dumps(payload_dict, separators=(",", ":")).encode()
    encoded_payload = _b64url_encode(payload)
    signature = _sign(payload)
    return f"{encoded_payload}.{signature}"


def validate_token(token: str, *, use_session_exp: bool = False) -> tuple[bool, str, str]:
    """Return ``(valid, user_id, reason)``.

    When *use_session_exp* is ``True`` (cookie-based access), validates
    against ``session_exp`` instead of ``exp`` (link click window).
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False, "", "malformed token"
    encoded_payload, sig = parts
    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except Exception:
        return False, "", "invalid encoding"
    expected = _sign(payload_bytes)
    if not hmac.compare_digest(sig, expected):
        return False, "", "invalid signature"
    try:
        data = json.loads(payload_bytes)
    except Exception:
        return False, "", "invalid payload"
    exp_field = "session_exp" if use_session_exp else "exp"
    if time.time() > data.get(exp_field, data.get("exp", 0)):
        return False, "", "token expired"
    # Revocation generation: an explicit revoke_all_sessions() (e.g. kiroclaw
    # logout) bumps the persisted counter. A token minted before that — link OR
    # cookie — carries a lower gen and is rejected. This is the ONLY check that
    # invalidates an established cookie (the nonce store is per-process and
    # restart-cleared; the HMAC secret is persisted, not rotated), so it is what
    # makes "revoke all sessions" actually revoke cookie sessions. Tokens minted
    # before this field existed default to gen 0, matching the initial counter.
    if int(data.get("gen", 0)) < _REVOCATION_GEN:
        return False, "", "session revoked"
    # Nonce is a single-use guard for the one-time LINK click only. For an
    # established session cookie (use_session_exp=True), a valid HMAC signature
    # plus an unexpired session_exp is sufficient — requiring the in-memory
    # nonce there would invalidate every live cookie on each gateway restart
    # (the nonce store is per-process), locking users out for no security gain.
    # Cookie revocation is handled by the gen check above, not the nonce.
    if not use_session_exp:
        token_nonce = data.get("nonce", "")
        valid, reason = _state.is_nonce_valid(token_nonce)
        if not valid:
            return False, "", reason
    return True, data.get("sub", ""), ""


def validate_token_with_app(
    token: str, *, use_session_exp: bool = False
) -> tuple[bool, str, str, str]:
    """Return ``(valid, user_id, reason, app_name)``.

    Extends :func:`validate_token` by also extracting the ``app`` field
    from the token payload.  This avoids changing the existing
    ``validate_token`` signature.
    """
    valid, user_id, reason = validate_token(token, use_session_exp=use_session_exp)
    if not valid:
        return False, user_id, reason, ""
    # Extract app from payload
    app_name = ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        app_name = data.get("app", "")
    except Exception:
        pass
    return valid, user_id, reason, app_name


def extract_prompt_from_token(token: str) -> str:
    """Extract the ``prompt`` field from a validated token payload.

    Validates the token first (deny-by-default). Returns the prompt
    string if valid and present, empty string otherwise.
    """
    valid, _user_id, _reason = validate_token(token)
    if not valid:
        return ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        return data.get("prompt", "")
    except Exception as exc:
        logger.warning(
            "extract_prompt_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return ""


def extract_claims_from_token(token: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Extract selected string claims from a validated token payload.

    Validates the token first (deny-by-default). Returns a dict containing
    only the requested *keys* that are present and string-typed; returns an
    empty dict if the token is invalid. Used by the Slack challenge-redirect
    frontend to recover ``channel``/``thread_ts``/``session_key`` so it can
    reconnect to (or auto-link) the correct Slack-linked session.

    Validates against ``session_exp`` (use_session_exp=True), NOT the 5-minute
    link window: claim recovery happens after the user has clicked through and
    established a session, so binding it to the link ``exp`` would lose the
    thread context (channel/thread_ts/session_key) the moment the click window
    closed, breaking auto-link/reconnect for the rest of the session.
    """
    valid, _user_id, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return {}
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception as exc:
        logger.warning(
            "extract_claims_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return {}
    out: dict[str, str] = {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    return out


def generate_app_secret() -> str:
    """Generate a random 64-char hex secret for app authentication."""
    return os.urandom(32).hex()


def validate_app_secret(app_name: str, provided_secret: str) -> bool:
    """Validate an app secret against the stored secret on disk.

    Reads ``~/.kiroclaw/apps/{app_name}/.app_secret`` and performs
    constant-time comparison via :func:`hmac.compare_digest`.
    Returns ``False`` if the file doesn't exist or doesn't match.
    """
    from kiro_claw.config.loader import config_dir

    secret_path = config_dir() / "apps" / app_name / ".app_secret"
    try:
        stored = secret_path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return False
    if not stored or not provided_secret:
        return False
    return hmac.compare_digest(stored, provided_secret)


def write_app_secret(app_name: str, secret: str) -> None:
    """Write an app secret to ``~/.kiroclaw/apps/{app_name}/.app_secret``.

    Creates the directory if needed and sets file mode to 0o600.
    """
    from kiro_claw.config.loader import config_dir

    secret_dir = config_dir() / "apps" / app_name
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secret_dir / ".app_secret"
    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _evict_expired() -> None:
    """Remove token state entries whose session has expired."""
    _state.evict_expired(time.time())


def bind_token_ip(token: str, ip: str, session_exp: float = 0.0) -> None:
    """Bind a token to a client IP for session validation."""
    _state.bind_ip(token, ip, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def check_token_ip(token: str, ip: str) -> bool:
    """Check if token is bound to the given IP (or unbound)."""
    return _state.check_ip(token, ip)


def mark_consumed(token: str, session_exp: float = 0.0) -> None:
    """Mark a token as consumed."""
    _state.mark_consumed(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def is_consumed(token: str) -> bool:
    """Check if a token has been consumed."""
    return _state.is_consumed(token)


def try_consume(token: str, session_exp: float = 0.0) -> bool:
    """Atomically consume a token if not already consumed.

    Returns True if this call consumed it, False if already consumed.
    """
    return _state.try_consume(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def revoke_all_sessions() -> None:
    """Revoke all active dashboard sessions (also used for test isolation).

    Emits a SEL audit event before clearing state so the revocation is recorded.
    """
    _sel_fn().log_api_access(
        caller="system",
        operation="dashboard_sessions_revoked",
        outcome="ok",
        source="token_auth",
        resources="action=revoke_all",
    )
    _state.clear_all()
    # Bump the persisted revocation generation so already-issued cookies (which
    # the cleared per-process nonce store cannot touch) are rejected on their
    # next request. This is what makes logout actually end cookie sessions.
    _bump_revocation_gen()


def parse_duration(s: str) -> int | None:
    """Parse ``'<int>h'`` or ``'<int>m'`` into seconds, or *None*.

    Returns *None* for invalid input. Caps at ``MAX_SESSION_TTL_SECS``.
    """
    m = re.fullmatch(r"(\d+)(h|m)", s)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    secs = value * 3600 if unit == "h" else value * 60
    return min(secs, MAX_SESSION_TTL_SECS)


def _cookie_port_from_host(request: web.Request, fallback: int) -> str:
    """Return the port the browser connects to (Host header), else *fallback*.

    The dashboard cookie is named ``mc_token_<port>``. Keying it by the server's
    own listen port breaks under SSH tunnels: two Cloud Desktops both serving on
    7777 but tunneled to distinct local ports would collide on a single
    ``mc_token_7777`` because browser cookies are not isolated by port (RFC 6265
    scopes by host only). The browser-facing port is unique per dashboard, so it
    is the correct key. Falls back to the server port when the Host header
    carries no port, preserving behavior for direct (non-tunnel) access.
    """
    host = request.headers.get("Host", "")
    if ":" in host:
        candidate = host.rsplit(":", 1)[-1].strip()
        if candidate.isdigit():
            return candidate
    return str(fallback)


def token_auth_middleware(
    *,
    internal_paths: frozenset[str] = frozenset(),
    mixed_internal_paths: frozenset[str] = frozenset(),
    internal_secret: str = "",
    port: int = 8765,
    local_only: bool = True,
) -> Callable[..., Any]:
    """Factory returning aiohttp middleware for token-based dashboard auth.

    ALL requests require a valid token — loopback is no longer exempt
    because local port forwarders (socat, ssh -R, custom scripts) make
    remote traffic appear as 127.0.0.1, bypassing auth entirely.

    *internal_paths* are exact paths that internal processes (mcp-core,
    doctor) call — these require loopback AND a matching
    ``X-Internal-Secret`` header (read from ``~/.kiroclaw/.local_secret``).
    Non-loopback access to these paths is always denied.

    *mixed_internal_paths* are paths called by BOTH internal processes
    (loopback + secret) AND the browser (cookie auth).  On non-loopback
    they perform explicit cookie validation (deny-by-default) instead
    of hard-denying, so DCV/SSH-forwarded browsers polling these routes
    (e.g. ``/api/spawn`` every 5s) don't trigger false session-expired
    banners.  Use this for any internal-path that the browser polls.

    """

    def _extract_and_validate_token(request: web.Request, _port: int) -> tuple[bool, str, str]:
        """Extract token from query param or cookie and validate it.

        Used by internal-path browser auth (no secret header).  The main
        auth flow has its own extraction with IP-binding and from_cookie
        tracking that this helper intentionally does not replicate.
        """
        cookie_name = f"mc_token_{_cookie_port_from_host(request, _port)}"
        token = request.query.get("token") or request.cookies.get(cookie_name, "")
        if not token:
            return False, "", "no token"
        return validate_token(token, use_session_exp=True)

    @web.middleware
    async def middleware(request: web.Request, handler: object) -> web.StreamResponse:
        path = request.path

        # Internal API paths: loopback + secret grants immediate access.
        # If the secret is missing (browser request), fall through to
        # normal cookie auth so dashboard pages can call these routes.
        _matches_strict = internal_paths and (
            path in internal_paths or any(path.startswith(p + "/") for p in internal_paths)
        )
        _matches_mixed = mixed_internal_paths and (
            path in mixed_internal_paths
            or any(path.startswith(p + "/") for p in mixed_internal_paths)
        )
        # local_only=False: treat ALL internal paths as mixed (backward compat
        # with mainline's local_only semantics — user opted into remote access)
        if not local_only and _matches_strict and not _matches_mixed:
            _matches_mixed = True
            _matches_strict = False
        _matches_internal = _matches_strict or _matches_mixed
        if _matches_internal and is_loopback(request.remote or ""):
            _has_secret_header = "X-Internal-Secret" in request.headers
            if _has_secret_header:
                _provided_secret = request.headers["X-Internal-Secret"]
                # Secret header present — validate it strictly
                if not internal_secret:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error="no internal secret configured",
                    )
                    _log_auth(request, "internal", "denied", "no internal secret configured")
                    return _deny(request, "Forbidden")
                if hmac.compare_digest(internal_secret, _provided_secret):
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="granted",
                        source="token_auth",
                        resources=path,
                    )
                    _log_auth(request, "internal", "granted", "")
                    return await handler(request)  # type: ignore[operator]
                # Wrong secret → deny (don't fall through)
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="wrong secret",
                )
                _log_auth(request, "internal", "denied", "wrong secret")
                return _deny(request, "Forbidden")
            # No secret header (browser request) → verify cookie/query-param auth
            # inline to satisfy deny-by-default: positively confirm auth
            # at the decision point rather than deferring to downstream.
            # NOTE: uses _extract_and_validate_token helper (defined above)
            # for cookie/query-param validation.
            _valid, _uid, _reason = _extract_and_validate_token(request, port)
            if not _valid:
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error=f"cookie auth failed: {_reason}",
                )
                _log_auth(request, "internal", "denied", f"cookie auth failed: {_reason}")
                return _deny(request, "Forbidden")
            _sel = _sel_fn()
            _sel.log_api_access(
                caller=request.remote or "",
                operation="internal_auth",
                outcome="granted",
                source="token_auth",
                resources=path,
                error="cookie auth (no secret header)",
            )
            _log_auth(request, "internal", "granted", f"cookie auth for {_uid}")
            return await handler(request)  # type: ignore[operator]
        elif _matches_internal:
            if _matches_mixed:
                # Mixed paths on non-loopback (DCV/SSH-forwarded browsers):
                # explicit cookie validation, mirroring the loopback
                # no-secret-header branch above.  Deny-by-default —
                # positively confirm auth at this decision point rather
                # than relying on downstream fall-through.
                # If X-Internal-Secret header is present, validate it first
                # (defense-in-depth: wrong secret = deny, even with valid cookie)
                if "X-Internal-Secret" in request.headers:
                    if not internal_secret or not hmac.compare_digest(
                        internal_secret, request.headers["X-Internal-Secret"]
                    ):
                        _sel = _sel_fn()
                        _sel.log_api_access(
                            caller=request.remote or "",
                            operation="internal_auth",
                            outcome="denied",
                            source="token_auth",
                            resources=path,
                            error="wrong secret (non-loopback mixed)",
                        )
                        _log_auth(
                            request, "internal", "denied", "wrong secret (non-loopback mixed)"
                        )
                        return _deny(request, "Forbidden")
                _valid, _uid, _reason = _extract_and_validate_token(request, port)
                if not _valid:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=request.remote or "",
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error=f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    _log_auth(
                        request,
                        "internal",
                        "denied",
                        f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    return _deny(request, "Forbidden")
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="granted",
                    source="token_auth",
                    resources=path,
                    error="mixed non-loopback cookie auth",
                )
                _log_auth(
                    request, "internal", "granted", f"mixed non-loopback cookie auth for {_uid}"
                )
                return await handler(request)  # type: ignore[operator]
            else:
                # INVARIANT: non-loopback access to strict internal paths is
                # ALWAYS denied.  Do NOT remove this branch — without it,
                # non-loopback requests would silently fall through to
                # normal cookie auth, defeating the machine-to-machine
                # isolation that the internal-secret design provides.
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="non-loopback source",
                )
                _log_auth(request, "internal", "denied", "non-loopback source")
                return _deny(request, "Forbidden")

        # Bypass static assets
        if any(path.startswith(p) for p in _BYPASS_PREFIXES):
            return await handler(request)  # type: ignore[operator]
        if path in _BYPASS_EXACT:
            return await handler(request)  # type: ignore[operator]
        # Icon files: anchored regex with bounded digit count to prevent
        # ReDoS and ensure only legitimate PWA icon paths bypass auth.
        if re.fullmatch(r"/icon-\d{1,4}\.png", path):
            return await handler(request)  # type: ignore[operator]

        # Installed-app UI bundles: anchored to /apps/{name}/ui/* only.
        # Does NOT match the reverse-proxy path /apps/{name}/api/*.
        # Restricted to safe methods (GET/HEAD) — static file serving only.
        # If a write-capable handler is ever registered under /apps/{name}/ui/,
        # it stays auth-protected because the bypass never fires for it.
        if _APPS_UI_BYPASS_RE.match(path) and request.method in ("GET", "HEAD"):
            return await handler(request)  # type: ignore[operator]

        # Bypass app token exchange (App Kit §5.1) — app authenticates
        # via X-App-Secret header, not a token cookie.
        if re.match(r"^/api/apps/[a-z0-9][a-z0-9_-]*/token$", path) and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/refresh — the handler authenticates via the
        # refresh cookie (path-restricted to this endpoint), not the access
        # cookie. Adding here lets refresh succeed even when the access
        # cookie has just expired (the whole point of the refresh flow).
        # GET is also allowed for /api/auth/me which is gated by normal auth
        # below — only POST /api/auth/refresh bypasses.
        if path == "/api/auth/refresh" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/logout — same rationale as /api/auth/refresh:
        # the handler authenticates via the refresh cookie and must work
        # even if the access cookie has just expired (so a user can still
        # tear down their refresh chain on the way out).
        if path == "/api/auth/logout" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Extract token from query param or cookie
        cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
        token = request.query.get("token") or ""
        from_cookie = False
        if not token:
            token = request.cookies.get(cookie_name, "")
            from_cookie = bool(token)

        if not token:
            _log_auth(request, "", "denied", "Token required")
            return _deny(request, "Token required")

        valid, user_id, reason, app_name = validate_token_with_app(
            token, use_session_exp=from_cookie
        )
        if not valid:
            _log_auth(request, "", "denied", reason)
            return _deny(request, reason)

        client_ip = request.remote or "unknown"

        if not check_token_ip(token, client_ip):
            _log_auth(request, user_id, "denied", "IP mismatch")
            return _deny(request, "IP mismatch")

        # Extract session_exp for cookie and IP binding on first query-param use
        session_exp = 0.0
        if not from_cookie:
            try:
                payload_bytes = _b64url_decode(token.split(".")[0])
                data = json.loads(payload_bytes)
                session_exp = data.get("session_exp", 0.0)
            except Exception:
                pass
            bind_token_ip(token, client_ip, session_exp)

        # Expose authenticated identity to handlers (deny-by-default)
        request["user"] = user_id
        request["app"] = app_name

        # Proceed to handler
        resp = await handler(request)  # type: ignore[operator]

        # Set cookie after handler (needs response object)
        if not from_cookie:
            cookie_max_age = MAX_SESSION_TTL_SECS
            if session_exp:
                remaining = int(session_exp - time.time())
                if 0 < remaining <= MAX_SESSION_TTL_SECS:
                    cookie_max_age = remaining
            resp.set_cookie(
                cookie_name,
                token,
                httponly=True,
                samesite="Lax",
                # Secure only when over HTTPS — localhost HTTP must
                # not set this or the browser refuses to send it back.
                secure=(request.scheme == "https"),
                path="/",
                max_age=cookie_max_age,
            )
            # Clean up legacy cookie from pre-port-specific era
            resp.set_cookie("mc_token", "", max_age=0, path="/")

            # Initial mint via token URL: also attach a refresh cookie so
            # the user does not have to re-mint via URL every ~20h. Inlined
            # here (rather than calling handlers.auth_refresh) to keep the
            # import top-level and the cycle direction one-way:
            # token_auth → refresh_tokens, never the reverse.
            try:
                refresh_token, chain_id, _jti, refresh_exp = generate_refresh_token(user_id)
                refresh_remaining = int(refresh_exp - time.time())
                if refresh_remaining > 0:
                    resp.set_cookie(
                        refresh_cookie_name(_cookie_port_from_host(request, port)),
                        refresh_token,
                        httponly=True,
                        samesite="Lax",
                        secure=(request.scheme == "https"),
                        path=REFRESH_COOKIE_PATH,
                        max_age=min(refresh_remaining, MAX_REFRESH_TTL_SECS),
                    )
                    # Audit the initial-mint event so forensics can trace any
                    # subsequent chain revocation back to the user it was issued to.
                    try:
                        _sel_fn().log_api_access(
                            caller=user_id,
                            operation="refresh_token_initial_mint",
                            outcome="ok",
                            source="refresh_tokens",
                            resources=chain_id,
                        )
                    except Exception as exc:  # pragma: no cover
                        # SEL must never block auth flows, but log the failure
                        # so it's observable.
                        logger.debug("token_auth: SEL audit failed: %s", exc)
            except Exception as _refresh_err:
                # Refresh cookie is best-effort. If something goes wrong
                # here, the access cookie still works as before — the
                # user just won't get the refresh upgrade until next mint.
                logger.warning(
                    "token_auth: failed to attach refresh cookie (%s); "
                    "access cookie still set, user can re-mint as before",
                    _refresh_err,
                )

        _log_auth(request, user_id, "ok", "")
        return resp  # type: ignore[return-value]

    middleware._is_token_auth = True  # type: ignore[attr-defined]  # sentinel for server.py security gate
    return middleware


def _deny(request: web.Request, reason: str) -> web.Response:
    headers = {"X-Auth-Required": "true"}
    if request.path.startswith("/api/"):
        return web.json_response({"error": reason}, status=403, headers=headers)
    return web.Response(
        text=_403_HTML.format(reason=reason),
        status=403,
        content_type="text/html",
        headers=headers,
    )


def _log_auth(request: web.Request, user_id: str, outcome: str, error: str) -> None:
    try:
        _sel_fn().log_api_access(
            caller=user_id or request.remote or "unknown",
            operation="dashboard.token_auth",
            outcome=outcome,
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("Failed to log auth event to SEL", exc_info=True)
