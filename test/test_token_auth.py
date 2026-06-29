"""Property tests for dashboard token authentication."""

from __future__ import annotations

import os
import socket
import string
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_claw.dashboard.token_auth import (
    MAX_CONCURRENT_NONCES,
    MAX_SESSION_TTL_SECS,
    bind_token_ip,
    check_token_ip,
    generate_token,
    is_consumed,
    mark_consumed,
    parse_duration,
    revoke_all_sessions,
    token_auth_middleware,
    try_consume,
    validate_token,
)


@pytest.fixture(autouse=True)
def clear_nonces(tmp_path, monkeypatch):
    """Isolate token state per test.

    Points config_dir at a tmp dir so the persisted revocation-generation file
    is not written to the real ~/.kiroclaw, resets the in-process gen to 0, and
    clears the nonce store. Uses _state.clear_all() (not revoke_all_sessions)
    so the gen isn't bumped between unrelated tests.
    """
    import kiro_claw.dashboard.token_auth as _ta

    monkeypatch.setattr("kiro_claw.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_ta, "_REVOCATION_GEN", 0)
    _ta._state.clear_all()
    yield
    monkeypatch.setattr(_ta, "_REVOCATION_GEN", 0)
    _ta._state.clear_all()


URL_SAFE_B64_CHARS = set(string.ascii_letters + string.digits + "-_.")


# -- Property 1: Token generation round-trip --


@pytest.mark.parametrize("user_id", ["alice", "bob@corp", "user-123", "a", "x" * 200])
def test_generate_then_validate_roundtrip(user_id: str) -> None:
    token = generate_token(user_id, ttl_seconds=60)
    valid, returned_id, reason = validate_token(token)
    assert valid is True
    assert returned_id == user_id
    assert reason == ""


# -- Property 2: Token URL safety --


@pytest.mark.parametrize("user_id", ["alice", "user/with/slashes", "emoji-☺", "a" * 300])
def test_token_url_safe_chars(user_id: str) -> None:
    token = generate_token(user_id)
    assert all(c in URL_SAFE_B64_CHARS for c in token)


# -- Property 3: Valid duration parsing --


@pytest.mark.parametrize("n", [0, 1, 5, 24, 100, 9999])
def test_parse_duration_hours(n: int) -> None:
    assert parse_duration(f"{n}h") == min(n * 3600, MAX_SESSION_TTL_SECS)


@pytest.mark.parametrize("n", [0, 1, 5, 30, 60, 9999])
def test_parse_duration_minutes(n: int) -> None:
    assert parse_duration(f"{n}m") == min(n * 60, MAX_SESSION_TTL_SECS)


# -- Property 4: Invalid duration strings rejected --


@pytest.mark.parametrize(
    "s",
    [
        "",
        "h",
        "m",
        "10",
        "10s",
        "10d",
        "abc",
        "-1h",
        "1.5h",
        "1H",
        "1M",
        " 1h",
        "1h ",
        "10hm",
        "h1",
        "m1",
    ],
)
def test_parse_duration_invalid(s: str) -> None:
    assert parse_duration(s) is None


# -- Property 13: IP binding enforcement --


def test_ip_binding_accepts_same_ip() -> None:
    token = generate_token("user1")
    bind_token_ip(token, "10.0.0.1")
    assert check_token_ip(token, "10.0.0.1") is True


def test_ip_binding_rejects_different_ip() -> None:
    token = generate_token("user2")
    bind_token_ip(token, "10.0.0.1")
    assert check_token_ip(token, "192.168.1.1") is False


def test_unbound_token_accepts_any_ip() -> None:
    token = generate_token("user3")
    assert check_token_ip(token, "10.0.0.1") is True
    assert check_token_ip(token, "192.168.1.1") is True


# -- Property 14: Token consumption --


def test_consumed_token_returns_true() -> None:
    token = generate_token("user4")
    assert is_consumed(token) is False
    mark_consumed(token)
    assert is_consumed(token) is True


def test_unconsumed_token_returns_false() -> None:
    token = generate_token("user5")
    assert is_consumed(token) is False


def test_try_consume_returns_true_once_then_false() -> None:
    """Verify try_consume atomicity: first call consumes, subsequent calls return False."""
    token = generate_token("user_try_consume")
    assert try_consume(token) is True, "first call should consume the token"
    assert try_consume(token) is False, "second call should report already consumed"
    assert is_consumed(token), "token should be marked consumed"


# -- Additional validation edge cases --


def test_expired_token_rejected() -> None:
    """Token link window (5 min) expires — URL no longer valid."""
    with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        token = generate_token("user6", ttl_seconds=3600)
    # Advance past the 5-minute link window
    with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 301
        valid, _, reason = validate_token(token)
    assert valid is False
    assert "expired" in reason


def test_session_exp_still_valid_after_link_window() -> None:
    """Cookie-based access uses session_exp, not the link window."""
    with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0
        token = generate_token("user6b", ttl_seconds=3600)
    # Past link window but within session TTL
    with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
        mock_time.time.return_value = 1000.0 + 301
        valid, uid, _ = validate_token(token, use_session_exp=True)
    assert valid is True
    assert uid == "user6b"


def test_tampered_token_rejected() -> None:
    token = generate_token("user7")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    valid, _, reason = validate_token(tampered)
    assert valid is False
    assert reason in ("invalid signature", "invalid encoding")


def test_malformed_token_rejected() -> None:
    valid, _, reason = validate_token("no-dot-here")
    assert valid is False
    assert reason == "malformed token"


# -- Middleware helpers --


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_request(
    path: str = "/",
    query: dict | None = None,
    cookies: dict | None = None,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    method: str = "GET",
) -> MagicMock:
    """Build a mock aiohttp request."""
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = query or {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = headers or {}
    req.method = method
    return req


# -- Property 5: Middleware accepts valid tokens via query param or cookie --


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["query", "cookie"])
async def test_middleware_accepts_valid_token(via: str) -> None:
    mw = token_auth_middleware()
    token = generate_token("testuser", ttl_seconds=300)

    if via == "cookie":
        # Pre-bind IP and mark consumed so cookie path works
        bind_token_ip(token, "127.0.0.1")
        mark_consumed(token)
        req = _make_request(cookies={"mc_token_5476": token})
    else:
        req = _make_request(query={"token": token})

    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"


# -- Property 6: Cookie set with correct attributes on query-param auth --


@pytest.mark.asyncio
async def test_cookie_set_on_query_param_auth() -> None:
    mw = token_auth_middleware()
    token = generate_token("cookieuser", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")

    resp = await mw(req, _ok_handler)
    assert resp.status == 200

    cookie_header = resp.cookies.get("mc_token_5476")
    assert cookie_header is not None
    assert cookie_header.value == token
    assert cookie_header["httponly"] is True or "httponly" in str(cookie_header).lower()
    assert cookie_header["samesite"] == "Lax"
    assert cookie_header["path"] == "/"


# -- Property 7: Cookie not re-set when already matching --


@pytest.mark.asyncio
async def test_cookie_not_reset_when_present() -> None:
    mw = token_auth_middleware()
    token = generate_token("existing", ttl_seconds=300)
    # Simulate prior query-param auth
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    # Cookie should NOT be re-set on cookie-based auth
    assert "mc_token_5476" not in resp.cookies


# -- Cookie keyed by browser-facing (Host) port for tunneled multi-instance --


@pytest.mark.asyncio
async def test_cookie_named_by_host_port_under_tunnel() -> None:
    """Server on 5476 reached via tunneled localhost:7778 -> cookie is
    mc_token_7778 (Host port), not mc_token_5476 (server port). Lets two
    same-server-port instances coexist without colliding in the localhost jar."""
    mw = token_auth_middleware()  # default server port 5476
    token = generate_token("tunneluser", ttl_seconds=300)
    req = _make_request(
        query={"token": token}, remote="10.0.0.1", headers={"Host": "localhost:7778"}
    )

    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.cookies.get("mc_token_7778") is not None  # keyed by Host port
    assert resp.cookies.get("mc_token_5476") is None  # not the server port


@pytest.mark.asyncio
async def test_cookie_read_uses_host_port() -> None:
    """A cookie named by the Host port authenticates the matching dashboard."""
    mw = token_auth_middleware()  # server port 5476
    token = generate_token("readuser", ttl_seconds=300)
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(
        cookies={"mc_token_7778": token}, headers={"Host": "localhost:7778"}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_cookie_server_port_name_denied_when_host_differs() -> None:
    """A server-port-named cookie does NOT authenticate a different Host port,
    so a sibling instance's cookie can't be mistaken for this one."""
    mw = token_auth_middleware()  # server port 5476
    token = generate_token("wrongport", ttl_seconds=300)
    bind_token_ip(token, "127.0.0.1")
    mark_consumed(token)

    req = _make_request(
        cookies={"mc_token_5476": token}, headers={"Host": "localhost:7778"}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status != 200


# -- Property 8: Static asset paths bypass token validation --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/assets/style.css",
        "/static/app.js",
        "/fonts/AWSDiatype-Regular.woff2",
        "/logo.png",
        "/manifest.json",
        "/sw.js",
        "/icon-192.png",
        "/icon-512.png",
        "/apps/some-app/ui/index.mjs",
        "/apps/some-app/ui/chunks/lazy-chunk.mjs",
    ],
)
async def test_static_assets_bypass_auth(path: str) -> None:
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token at all
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


# -- Property 8b: /api/apps/* still requires auth (security boundary) --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/apps",
        "/api/apps/some-app/data/config.json",
        "/api/apps/some-app/storage/state",
    ],
)
async def test_apps_api_still_requires_auth(path: str) -> None:
    """The /apps/ static bypass MUST NOT leak into /api/apps/* paths.

    Static UI bundles are public-equivalent (same as /static/), but app data
    and storage live behind /api/* and continue to require a valid token.
    """
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 8c: non-UI paths under /apps/{name}/ still require auth --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        # The reverse-proxy route at /apps/{name}/api/{path:.*} forwards to
        # the app's backend (handle_app_api_proxy). It MUST stay
        # authenticated — the proxy's HMAC only proves the request came
        # from the gateway, not that the user was authenticated.
        "/apps/some-app/api/things",
        "/apps/some-app/api/data/sensitive",
        "/apps/some-app/api/state?op=delete",
        # Future non-UI public paths under /apps/{name}/ are deny-by-default.
        # If a real need surfaces, add a dedicated regex with its own audit;
        # do not widen this bypass.
        "/apps/some-app/manifest.json",
        "/apps/some-app/config/settings.json",
        "/apps/some-app/admin",
    ],
)
async def test_apps_non_ui_paths_still_require_auth(path: str) -> None:
    """The /apps/{name}/ui/ bypass MUST NOT leak into other /apps/{name}/* paths.

    The bypass is anchored to /ui/ only (federated-app RFC §3.8). Anything
    else under /apps/{name}/ — most importantly the reverse-proxy at
    /apps/{name}/api/* — continues to require a valid token.
    """
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 8d: /apps/{name}/ui/ bypass is restricted to safe methods --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD"],
)
async def test_apps_ui_bypass_allows_safe_methods(method: str) -> None:
    """GET and HEAD on /apps/{name}/ui/* bypass auth (static file serving)."""
    mw = token_auth_middleware()
    req = _make_request(path="/apps/some-app/ui/index.mjs", method=method)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def test_apps_ui_bypass_blocks_unsafe_methods(method: str) -> None:
    """Non-safe methods on /apps/{name}/ui/* MUST require auth.

    The bypass is for static file serving only. If a write-capable handler
    is ever registered under /apps/{name}/ui/, it must remain auth-protected
    rather than silently inheriting the bypass.
    """
    mw = token_auth_middleware()
    req = _make_request(path="/apps/some-app/ui/upload", method=method)  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 9: Loopback no longer bypasses auth (port-forward fix) --


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/api/status", "/some/page"])
async def test_loopback_requires_token(path: str) -> None:
    mw = token_auth_middleware()
    req = _make_request(path=path)  # No token, loopback
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_trusts_loopback() -> None:
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_denied_in_local_only_mode() -> None:
    """Default local_only=True denies non-loopback even with valid secret."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn", remote="10.0.0.1", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_cookie_auth_when_not_local_only() -> None:
    """When local_only=False, non-loopback with valid cookie is granted."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="s", local_only=False
    )
    req = _make_request(path="/api/spawn", remote="10.0.0.1", cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_no_cookie_denied() -> None:
    """When local_only=False, non-loopback without cookie is denied."""
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="s", local_only=False
    )
    req = _make_request(path="/api/spawn", remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_wrong_secret_denied() -> None:
    """When local_only=False, wrong X-Internal-Secret is denied even with cookie."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "wrong"},
        cookies={"mc_token_5476": token},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_non_loopback_valid_secret_and_cookie_granted() -> None:
    """Both valid secret and valid cookie on non-loopback → granted."""
    token = generate_token("testuser", ttl_seconds=300)
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "real"},
        cookies={"mc_token_5476": token},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_non_loopback_valid_secret_no_cookie_denied() -> None:
    """Valid secret alone is not enough for non-loopback; cookie is still required."""
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real", local_only=False
    )
    req = _make_request(
        path="/api/spawn",
        remote="10.0.0.1",
        headers={"X-Internal-Secret": "real"},
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_rejects_wrong_secret() -> None:
    mw = token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret="real-secret"
    )
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": "wrong-secret"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_internal_path_matches_sub_paths() -> None:
    """GET /api/spawn/{id} should be granted via /api/spawn prefix."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawn/abc123", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_internal_path_does_not_match_sibling_prefix() -> None:
    """GET /api/spawnfoo must NOT be treated as internal via /api/spawn."""
    secret = "test-secret-123"
    mw = token_auth_middleware(internal_paths=frozenset({"/api/spawn"}), internal_secret=secret)
    req = _make_request(path="/api/spawnfoo", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 9b: mixed_internal_paths (loopback MCP + non-loopback browser) --


@pytest.mark.asyncio
async def test_mixed_path_loopback_with_secret_granted() -> None:
    """MCP path: loopback + X-Internal-Secret → granted via fast-path."""
    secret = "test-secret-123"
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}), internal_secret=secret
    )
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": secret})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_mixed_path_non_loopback_with_valid_cookie_granted() -> None:
    """DCV/SSH-forwarded browser: non-loopback + valid cookie → granted (no false banner)."""
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/spawn"}))
    token = generate_token("dcvuser", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(path="/api/spawn", remote="10.0.0.1", cookies={"mc_token_5476": token})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_mixed_path_non_loopback_without_cookie_denied() -> None:
    """Non-loopback + no cookie → still denied (security preserved)."""
    mw = token_auth_middleware(mixed_internal_paths=frozenset({"/api/spawn"}))
    req = _make_request(path="/api/spawn", remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_strict_path_non_loopback_still_hard_denied() -> None:
    """Strict internal path: non-loopback → hard-denied even with valid cookie
    (invariant: machine-to-machine isolation preserved)."""
    mw = token_auth_middleware(internal_paths=frozenset({"/api/send-message"}))
    token = generate_token("attacker", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(
        path="/api/send-message", remote="10.0.0.1", cookies={"mc_token_5476": token}
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


# -- Property 10: Nonce-based token invalidation --


def test_oldest_token_evicted_after_max_concurrent() -> None:
    """Token beyond MAX_CONCURRENT_NONCES evicts the oldest nonce."""
    tokens = [generate_token("user1") for _ in range(MAX_CONCURRENT_NONCES + 1)]
    valid_old, _, reason = validate_token(tokens[0])
    valid_new, _, _ = validate_token(tokens[-1])
    assert (
        not valid_old
    ), f"oldest token should be evicted after {MAX_CONCURRENT_NONCES + 1} generations"
    assert reason == "token superseded"
    assert valid_new, "most recently issued token should remain valid"
    # Verify second-oldest survives (only one evicted)
    valid_survivor, _, _ = validate_token(tokens[1])
    assert valid_survivor, "second-oldest token should survive when only one is evicted"


def test_concurrent_tokens_within_limit_all_valid() -> None:
    """Up to MAX_CONCURRENT_NONCES tokens should all remain valid."""
    tokens = [generate_token(f"user{i}") for i in range(MAX_CONCURRENT_NONCES)]
    for i, token in enumerate(tokens):
        valid, uid, _ = validate_token(token)
        assert valid, f"token {i} should be valid within concurrent limit"
        assert uid == f"user{i}"


def test_token_rejected_when_no_nonces_registered() -> None:
    """Verify deny-by-default: tokens rejected after an explicit revoke.

    revoke_all_sessions() both clears the nonce store AND bumps the persisted
    revocation generation, so a token minted before it is rejected either as
    'session revoked' (gen check, which fires first) or 'no active sessions'
    (nonce check) — both are valid deny-by-default rejections.
    """
    token = generate_token("user1")
    revoke_all_sessions()
    valid, _, reason = validate_token(token)
    assert not valid
    assert reason in ("session revoked", "no active sessions")


def test_cookie_auth_survives_nonce_store_wipe() -> None:
    """Regression: an established session cookie must survive a gateway RESTART.

    A restart reloads the persisted signing secret + revocation generation
    unchanged and re-initializes the in-memory nonce store empty. The cookie
    path (use_session_exp=True) must pass on signature + session_exp + matching
    gen alone — requiring the per-process nonce would log everyone out on every
    restart. The LINK path still enforces the nonce. We model the restart by
    clearing ONLY the nonce store (gen untouched), not via revoke_all_sessions.
    """
    from kiro_claw.dashboard.token_auth import _state

    token = generate_token("user_cookie")
    _state.clear_all()  # simulate restart: in-memory nonce store re-initialized empty
    # LINK click still requires the nonce → rejected.
    link_valid, _, link_reason = validate_token(token, use_session_exp=False)
    assert not link_valid
    assert link_reason in ("no active sessions", "token superseded")
    # COOKIE re-auth survives the restart (gen unchanged).
    cookie_valid, uid, cookie_reason = validate_token(token, use_session_exp=True)
    assert cookie_valid, f"cookie should survive restart, got: {cookie_reason}"
    assert uid == "user_cookie"


def test_revoke_all_sessions_kills_established_cookie() -> None:
    """Explicit revoke (kiroclaw logout) MUST end established cookie sessions.

    Unlike a restart, revoke_all_sessions() bumps the persisted revocation
    generation, so a cookie minted before the revoke carries a stale gen and is
    rejected on its next request — even though its HMAC signature and
    session_exp are still valid. This is the control the nonce store could not
    provide for cookies (it is per-process and restart-cleared).
    """
    token = generate_token("user_cookie")
    # Cookie is valid before revoke.
    valid_before, _, _ = validate_token(token, use_session_exp=True)
    assert valid_before
    revoke_all_sessions()  # explicit operator logout
    # Cookie is now rejected as revoked.
    valid_after, _, reason = validate_token(token, use_session_exp=True)
    assert not valid_after
    assert reason == "session revoked"


def test_signing_secret_persisted_across_loads(tmp_path, monkeypatch) -> None:
    """Regression: the HMAC signing secret must persist across processes.

    Previously _SECRET was os.urandom(32) per import, so every restart rotated
    the key and invalidated all outstanding tokens/cookies ("invalid
    signature"). The secret is now loaded-or-created from a 0600 key file.
    """
    from kiro_claw.dashboard import token_auth as ta

    monkeypatch.setattr(ta, "config_dir", lambda: tmp_path, raising=False)
    # First load creates the key file.
    monkeypatch.setattr("kiro_claw.config.loader.config_dir", lambda: tmp_path)
    s1 = ta._load_or_create_secret()
    key_file = tmp_path / ta._SECRET_KEY_FILE
    assert key_file.exists()
    assert len(s1) >= 32
    # Owner-only permissions.
    assert (key_file.stat().st_mode & 0o777) == 0o600
    # Second load returns the SAME secret (persistence).
    s2 = ta._load_or_create_secret()
    assert s1 == s2


def test_evict_expired_removes_old_entries() -> None:
    """Verify evict_expired removes expired IP bindings, consumed tokens, and nonces."""
    from kiro_claw.dashboard.token_auth import _state

    # Generate a token and bind IP / mark consumed
    token = generate_token("evict_user")
    bind_token_ip(token, "10.0.0.1", session_exp=1000.0)  # Already expired
    mark_consumed(token, session_exp=1000.0)  # Already expired

    # Manually add an expired nonce
    with _state._lock:
        _state._nonces["expired_nonce"] = 1000.0  # Already expired

    # Evict with current time > 1000
    _state.evict_expired(2000.0)

    # Verify expired entries were removed
    with _state._lock:
        assert token not in _state._ip_bindings, "expired IP binding should be evicted"
        assert token not in _state._consumed, "expired consumed token should be evicted"
        assert "expired_nonce" not in _state._nonces, "expired nonce should be evicted"


def test_token_reusable_across_multiple_validations() -> None:
    token = generate_token("user1")
    for _ in range(5):
        valid, _, _ = validate_token(token)
        assert valid, "same token should be reusable across browsers/tabs/apps"


def test_active_nonce_survives_eviction_via_refresh() -> None:
    """Validating a token refreshes its nonce position, preventing eviction."""
    old_token = generate_token("old_user")
    # Fill remaining slots
    for i in range(MAX_CONCURRENT_NONCES - 1):
        generate_token(f"filler{i}")
    # old_token is now the oldest — validate it to refresh its position
    valid, _, _ = validate_token(old_token, use_session_exp=True)
    assert valid, "old token should still be valid before overflow"
    # Generate one more to trigger eviction — old_token should survive
    generate_token("overflow")
    valid_after, _, reason = validate_token(old_token, use_session_exp=True)
    assert valid_after, f"actively-used token should survive eviction, got: {reason}"


# -- Property 12: /api/* paths get JSON 403, non-API GET paths get HTML 403 --


@pytest.mark.asyncio
async def test_api_path_gets_json_403() -> None:
    mw = token_auth_middleware()
    req = _make_request(path="/api/status", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_non_api_path_gets_html_403() -> None:
    mw = token_auth_middleware()
    req = _make_request(path="/dashboard", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert resp.content_type == "text/html"


# -- Property 15: non-local mode forces token auth for all requests --


@pytest.mark.asyncio
async def test_query_param_token_reusable_across_requests() -> None:
    """Same token can be used from multiple browsers/tabs/apps."""
    mw = token_auth_middleware()
    token = generate_token("reuse_user", ttl_seconds=300)

    # First use: succeeds
    req1 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200

    # Second use of the same token via query param: also succeeds
    req2 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 200


@pytest.mark.asyncio
async def test_non_local_requires_auth() -> None:
    """Non-loopback clients require auth."""
    mw = token_auth_middleware()
    req = _make_request(path="/", remote="10.0.0.1")  # No token
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_non_local_accepts_valid_token() -> None:
    """Non-loopback clients with valid tokens are granted access."""
    mw = token_auth_middleware()
    token = generate_token("remote_user", ttl_seconds=300)
    req = _make_request(query={"token": token}, remote="10.0.0.1")
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert resp.text == "ok"


# -- Property 16: URL uses hostname for remote access, localhost for local-only --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dashboard_url, expected_host",
    [
        ("http://myhostname:8080", "myhostname"),
        ("", "localhost"),  # no URL → localhost-only default
    ],
)
async def test_dashboard_url_host_selection(dashboard_url: str, expected_host: str) -> None:
    """!dashboard sends presigned link via DM, never in channel."""
    from kiro_claw.slack.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = dashboard_url

    expected_port = 8080 if dashboard_url else 5476

    # Unset KIROCLAW_PORT so parse_dashboard_url (which reads os.environ at
    # call time) uses the port from the URL or the hard-coded default.
    with (
        patch("kiro_claw.slack.allowlist.KiroClawConfig.load", return_value=mock_cfg),
        patch("kiro_claw.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("kiro_claw.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("kiro_claw.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror),
        patch.dict(os.environ, {}, KIROCLAW_PORT=""),
        patch("kiro_claw.slack.allowlist.sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        await _handle_slash_command(
            "!dashboard", slack, sessions, "C123", "ts1", "ts2", "sess1", "U001"
        )

    # Link sent via DM (open_dm called), not in the channel
    slack.open_dm.assert_called_once_with("U001")
    dm_msg = slack.post_message.call_args_list[0][0]
    assert dm_msg[0] == "D_DM"  # sent to DM channel
    assert f"http://{expected_host}:{expected_port}/?token=" in dm_msg[1]


# -- Property 10: SEL logs contain operation='slack.dashboard_token' with user_id and TTL --


# -- api_logout handler tests --


@pytest.mark.asyncio
async def test_api_logout_success_from_loopback() -> None:
    """POST /api/logout succeeds from loopback with valid secret."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_claw.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "test-secret-123"
    app.router.add_post("/api/logout", api_logout)

    # Generate a token first so there's something to revoke
    generate_token("user1")

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/logout",
            json={},
            headers={"X-Local-Secret": "test-secret-123"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True


@pytest.mark.asyncio
async def test_api_logout_rejects_non_loopback() -> None:
    """POST /api/logout rejects requests from non-loopback IPs."""
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_claw.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "test-secret-123"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        # Patch is_loopback to return False (simulating non-loopback request)
        with patch("kiro_claw.dashboard.handlers.is_loopback", return_value=False):
            resp = await client.post(
                "/api/logout",
                json={},
                headers={"X-Local-Secret": "test-secret-123"},
            )
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "loopback only"


@pytest.mark.asyncio
async def test_api_logout_rejects_invalid_secret() -> None:
    """POST /api/logout rejects requests with invalid secret."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_claw.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "correct-secret"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/logout",
            json={},
            headers={"X-Local-Secret": "wrong-secret"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid secret"


@pytest.mark.asyncio
async def test_api_logout_rejects_missing_secret() -> None:
    """POST /api/logout rejects requests without secret header."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_claw.dashboard.handlers import api_logout

    app = web.Application()
    app["local_secret"] = "correct-secret"
    app.router.add_post("/api/logout", api_logout)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/logout", json={})
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_arg, expected_ttl", [("", 3600), ("2h", 7200), ("30m", 1800)])
async def test_dashboard_sel_log(duration_arg: str, expected_ttl: int) -> None:
    """!dashboard logs SEL with operation='slack.dashboard_token', caller, and ttl."""
    from kiro_claw.slack.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = ""

    cmd_text = f"!dashboard {duration_arg}".strip()

    with (
        patch("kiro_claw.slack.allowlist.KiroClawConfig.load", return_value=mock_cfg),
        patch("kiro_claw.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("kiro_claw.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("kiro_claw.slack.allowlist.sel") as mock_sel,
    ):
        mock_log = MagicMock()
        mock_sel.return_value.log_api_access = mock_log
        await _handle_slash_command(
            cmd_text, slack, sessions, "C123", "ts1", "ts2", "sess1", "U_TEST"
        )

    mock_log.assert_called_once_with(
        caller="U_TEST",
        operation="slack.dashboard_token",
        outcome="ok",
        resources=f"ttl={expected_ttl}",
    )


# -- Property 17: Port-specific cookie names prevent multi-server collision --


@pytest.mark.asyncio
async def test_different_ports_use_different_cookie_names() -> None:
    """Two servers on different ports must not share cookies (RFC 6265 §8.5)."""
    mw_a = token_auth_middleware(port=5476)
    mw_b = token_auth_middleware(port=6777)
    token_a = generate_token("user_a", ttl_seconds=300)
    token_b = generate_token("user_b", ttl_seconds=300)

    # Server A sets mc_token_5476
    req_a = _make_request(query={"token": token_a}, remote="127.0.0.1")
    resp_a = await mw_a(req_a, _ok_handler)
    assert resp_a.status == 200
    assert "mc_token_5476" in resp_a.cookies
    assert "mc_token_6777" not in resp_a.cookies
    # Verify legacy mc_token cookie is expired on upgrade
    legacy = resp_a.cookies.get("mc_token")
    assert legacy is not None, "Legacy mc_token cookie should be set for expiration"
    assert legacy["max-age"] == "0"

    # Server B sets mc_token_6777
    req_b = _make_request(query={"token": token_b}, remote="127.0.0.1")
    resp_b = await mw_b(req_b, _ok_handler)
    assert resp_b.status == 200
    assert "mc_token_6777" in resp_b.cookies
    assert "mc_token_5476" not in resp_b.cookies


@pytest.mark.asyncio
async def test_wrong_port_cookie_rejected() -> None:
    """Server A must reject a cookie set by server B (different port suffix)."""
    mw_a = token_auth_middleware(port=5476)
    token_b = generate_token("user_b", ttl_seconds=300)
    bind_token_ip(token_b, "127.0.0.1")
    mark_consumed(token_b)

    # Send server B's cookie to server A — wrong cookie name
    req = _make_request(cookies={"mc_token_6777": token_b}, remote="127.0.0.1")
    resp = await mw_a(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_non_default_port_full_cycle() -> None:
    """Full query-param → cookie-set → cookie-read cycle on non-default port."""
    mw = token_auth_middleware(port=6777)
    token = generate_token("user_6777", ttl_seconds=300)

    # Step 1: query-param auth sets cookie
    req1 = _make_request(query={"token": token}, remote="10.0.0.1")
    resp1 = await mw(req1, _ok_handler)
    assert resp1.status == 200
    cookie = resp1.cookies.get("mc_token_6777")
    assert cookie is not None
    assert cookie.value == token

    # Step 2: cookie-based auth on subsequent request
    req2 = _make_request(cookies={"mc_token_6777": token}, remote="10.0.0.1")
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 200
