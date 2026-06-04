"""Shared origin-validation helpers for CSRF and WebSocket checks.

Centralises dashboard URL parsing, bind-address resolution, origin-set
construction, and per-request origin validation so that ``server.py``
(CSRF middleware), ``ws.py`` (WebSocket handshake), and ``gateway.py``
(startup messages) all share a single source of truth.

The only user-facing config is ``dashboard.url`` — a single URL like
``http://my-host.example.com:8080``.  Everything else (port, bind
address, allowed origins) is derived from it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import parse_qs, quote, urlparse

from aiohttp import web

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8765

_BIND_LOCAL = "127.0.0.1"
_BIND_ALL = "0.0.0.0"


# ---------------------------------------------------------------------------
# Hostname / IP helpers
# ---------------------------------------------------------------------------


def machine_hostname() -> str | None:
    """Return the machine hostname, or ``None`` on failure."""
    try:
        return socket.gethostname()
    except Exception:
        return None


def is_loopback(host: str) -> bool:
    """Return ``True`` if *host* is a loopback address (127.0.0.1, ::1, etc.)."""
    if host in ("localhost", "127.0.0.1", "::1", "kiroclaw.localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Dashboard URL parsing
# ---------------------------------------------------------------------------


def parse_dashboard_url(url: str) -> tuple[str, int]:
    """Parse ``dashboard.url`` into ``(hostname, port)``.

    Returns ``("", _DEFAULT_PORT)`` when *url* is empty.
    ``KIROCLAW_PORT`` env var always overrides the port (dev mode).
    """
    if not url:
        host, port = "", _DEFAULT_PORT
    else:
        url = _ensure_scheme(url)
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or _DEFAULT_PORT
    env_port = os.environ.get("KIROCLAW_PORT")
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            logger.warning(
                "KIROCLAW_PORT=%r is not a valid integer; using port %d from config",
                env_port,
                port,
            )
    return host, port


def _ensure_scheme(url: str) -> str:
    """Prepend ``http://`` if *url* has no scheme."""
    return url if "://" in url else f"http://{url}"


def dashboard_origin(url: str) -> str:
    """Return the browser-facing origin for *url*, or ``""`` if invalid.

    Reuses the same scheme-defaulting logic as :func:`parse_dashboard_url`
    so that bare hostnames (``myhost:8080``) are normalised to ``http://``.
    Default ports (80 for http, 443 for https) are stripped to match
    browser ``Origin`` header behaviour.
    """
    if not url:
        return ""
    url = _ensure_scheme(url)
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        logger.warning("Ignoring malformed dashboard_url: %s", url)
        return ""
    if not host:
        return ""
    if scheme not in ("http", "https"):
        logger.warning("Ignoring non-HTTP dashboard_url scheme: %s", scheme)
        return ""
    # urlparse strips [] from IPv6 — re-wrap to match browser Origin header
    if ":" in host:
        host = f"[{host}]"
    default_port = {"http": 80, "https": 443}.get(scheme)
    if port == default_port:
        port = None
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Remote-proxy detection (OSS: no managed proxy)
# ---------------------------------------------------------------------------


def devspaces_proxy_url(port: int) -> str | None:
    """Return the managed-proxy base URL, or ``None`` (always ``None`` in OSS).

    Symbol preserved for callers (``is_local_only``, ``format_dashboard_urls``,
    ``build_allowed_origins``); there is no managed reverse-proxy in the public
    build, so this always returns ``None``.  Users behind their own proxy add
    its origin via ``dashboard.url`` or ``KIROCLAW_CORS_ORIGINS``.
    """
    return None


# ---------------------------------------------------------------------------
# Local-only mode resolution
# ---------------------------------------------------------------------------


def is_local_only(dashboard_host: str, slack_connected: bool) -> bool:
    """Determine whether the dashboard should bind to loopback only.

    Always returns ``True`` (bind ``127.0.0.1``) in the public build.
    To expose the dashboard beyond loopback, run your own reverse proxy
    (e.g. Caddy/nginx with TLS) and add its origin via ``dashboard.url``
    or ``KIROCLAW_CORS_ORIGINS``.
    """
    # A managed proxy could require a 0.0.0.0 binding; there is none in OSS
    # (devspaces_proxy_url always returns None), so we always bind loopback.
    # Safety: slack_connected=True guarantees start_dashboard() mounts
    # token_auth_middleware before any non-loopback binding is used.
    if devspaces_proxy_url(0) is not None and slack_connected:
        logger.info("Managed proxy detected: binding 0.0.0.0 (token auth via Slack)")
        return False
    return True


def bind_address_for(local_only: bool) -> str:
    """Return the TCP bind address string for aiohttp."""
    return _BIND_LOCAL if local_only else _BIND_ALL


# ---------------------------------------------------------------------------
# Dashboard host / URL helpers
# ---------------------------------------------------------------------------


def resolve_dashboard_host(local_only: bool, configured_host: str = "") -> str:
    """Return the hostname users should use to reach the dashboard."""
    if configured_host:
        return configured_host
    if local_only:
        try:
            socket.getaddrinfo("kiroclaw.localhost", None)
            return "kiroclaw.localhost"
        except socket.gaierror:
            return "localhost"
    return machine_hostname() or "localhost"


def build_dashboard_url(base_url: str, token: str = "", *, local_only: bool = True) -> str:
    """Build the authenticated dashboard URL."""
    if local_only is not True and not token:
        raise ValueError("token is required when dashboard is not local-only")
    return f"{base_url}?token={quote(token, safe='')}" if token else base_url


def format_dashboard_urls(
    authed_url: str,
    *,
    port: int,
    local_only: bool = True,
    has_custom_host: bool = False,
) -> list[str]:
    """Return startup log lines describing how to reach the dashboard."""
    parsed_query = urlparse(authed_url).query
    _qs = f"?{parsed_query}" if parsed_query else ""
    if local_only is not True and "token" not in parse_qs(parsed_query):
        raise ValueError("token is required when dashboard is not local-only")
    _is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _is_remote:
        mh = machine_hostname() or "localhost"
        lines: list[str] = [
            f"🐾 Dashboard: ssh -NL {port}:localhost:{port} {mh}",
            f"             then open http://localhost:{port}{_qs}",
        ]
    else:
        lines = ["🐾 Dashboard:", f"   {authed_url}"]

    if local_only and not has_custom_host and not _is_remote:
        mh_local = machine_hostname()
        if mh_local and mh_local != "localhost":
            try:
                ip = socket.gethostbyname(mh_local)
                if ip and ip != "127.0.0.1":
                    lines.append(f"🐾 Remote:    ssh -NL {port}:localhost:{port} {mh_local}")
            except Exception:
                pass

    proxy = devspaces_proxy_url(port)
    if proxy and not local_only:
        lines.append(f"🐾 Proxy:     {proxy}{_qs}")

    if _is_remote:
        lines.append("🐾 Run 24/7:  see docs/REMOTE_DESKTOP_SETUP.md for systemd service setup")

    return lines


# ---------------------------------------------------------------------------
# Allowed-origin set
# ---------------------------------------------------------------------------


def build_allowed_origins(
    port: int, local_only: bool, configured_host: str = "", dashboard_url: str = ""
) -> set[str]:
    """Compute the set of allowed origins for the dashboard.

    When *dashboard_url* is provided, its origin (scheme + host + port)
    is added as-is so that reverse-proxy setups (e.g. Caddy with TLS on
    a custom domain) pass the CSRF check without code changes.
    """
    origins: set[str] = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://kiroclaw.localhost:{port}",
    }
    if os.environ.get("KIROCLAW_HOME"):
        origins.add("http://localhost:3000")
    if configured_host:
        origins.add(f"http://{configured_host}:{port}")
    if dashboard_url:
        origin = dashboard_origin(dashboard_url)
        if origin:
            origins.add(origin)
    if not local_only:
        mh = machine_hostname()
        if mh:
            origins.add(f"http://{mh}:{port}")
    # Managed-proxy origin (None in OSS; see devspaces_proxy_url)
    proxy = devspaces_proxy_url(port)
    if proxy:
        origins.add(proxy)
    # Manual CORS override for future environments
    for _co in os.environ.get("KIROCLAW_CORS_ORIGINS", "").split(","):
        if _co.strip():
            origins.add(_co.strip())
    return origins


# ---------------------------------------------------------------------------
# Per-request origin check
# ---------------------------------------------------------------------------


def check_origin(
    request: web.Request,
    *,
    require: bool = True,
    fallback_header: str | None = None,
) -> bool:
    """Validate the request origin against ``app["allowed_origins"]``.

    Loopback requests (127.0.0.1, ::1) without an Origin header are
    always trusted — local processes like mcp-core and doctor don't
    send Origin headers but are not cross-origin attacks.  A browser
    on the same machine would always send an Origin header.
    """
    allowed: set[str] = request.app["allowed_origins"]
    origin = request.headers.get("Origin") or ""
    if not origin and fallback_header:
        origin = request.headers.get(fallback_header, "")
    if not origin:
        # No Origin header: trust loopback (local processes), reject others
        if is_loopback(request.remote or ""):
            return True
        return not require
    origin_base = "/".join(origin.split("/")[:3]) if "://" in origin else ""
    if origin_base in allowed:
        return True
    # Trust any loopback origin regardless of port — SSH tunnels commonly
    # forward a different local port (e.g. -L 8777:localhost:8765) causing
    # the browser to send an Origin with a port not in the allowed set.
    # Token auth is the real security boundary; CSRF from localhost is not
    # a realistic threat.
    if origin_base:
        parsed_host = urlparse(origin_base).hostname or ""
        if is_loopback(parsed_host):
            return True
    return False
