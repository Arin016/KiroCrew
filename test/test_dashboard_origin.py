"""Tests for dashboard origin helpers."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from kiro_claw.dashboard.origin import (
    build_allowed_origins,
    build_dashboard_url,
    check_origin,
    dashboard_origin,
    format_dashboard_urls,
    parse_dashboard_url,
    resolve_dashboard_host,
    should_canonicalize_host,
)


class TestBuildAllowedOrigins:
    def test_default_origins(self) -> None:
        origins = build_allowed_origins(5476, local_only=True)
        assert "http://127.0.0.1:5476" in origins
        assert "http://localhost:5476" in origins
        assert "http://kiroclaw.localhost:5476" in origins

    def test_configured_host_adds_http_with_port(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, configured_host="myhost")
        assert "http://myhost:5476" in origins

    def test_dashboard_url_empty_no_extra_origin(self) -> None:
        baseline = build_allowed_origins(5476, local_only=True)
        with_empty = build_allowed_origins(5476, local_only=True, dashboard_url="")
        assert baseline == with_empty

    def test_dashboard_url_https_adds_origin(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kiroclaw.local"
        )
        assert "https://kiroclaw.local" in origins

    def test_dashboard_url_http_with_port(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, dashboard_url="http://myhost:8080")
        assert "http://myhost:8080" in origins

    def test_dashboard_url_no_scheme_normalized(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="myhost:8080"
        )
        assert "http://myhost:8080" in origins

    def test_dashboard_url_preserves_existing_origins(self) -> None:
        origins = build_allowed_origins(
            5476,
            local_only=True,
            configured_host="myhost",
            dashboard_url="https://kiroclaw.local",
        )
        assert "http://myhost:5476" in origins
        assert "https://kiroclaw.local" in origins
        assert "http://localhost:5476" in origins

    def test_dashboard_url_strips_default_https_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kiroclaw.local:443"
        )
        assert "https://kiroclaw.local" in origins
        assert "https://kiroclaw.local:443" not in origins

    def test_dashboard_url_strips_default_http_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="http://kiroclaw.local:80"
        )
        assert "http://kiroclaw.local" in origins
        assert "http://kiroclaw.local:80" not in origins

    def test_dashboard_url_keeps_non_default_port(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://kiroclaw.local:8443"
        )
        assert "https://kiroclaw.local:8443" in origins

    def test_dashboard_url_malformed_port_ignored(self) -> None:
        origins = build_allowed_origins(
            5476, local_only=True, dashboard_url="https://host:abc"
        )
        assert len([o for o in origins if "host:abc" in o]) == 0


class TestDashboardOrigin:
    def test_empty_returns_empty(self) -> None:
        assert dashboard_origin("") == ""

    def test_https_url(self) -> None:
        assert dashboard_origin("https://kiroclaw.local") == "https://kiroclaw.local"

    def test_bare_host_defaults_to_http(self) -> None:
        assert dashboard_origin("myhost:8080") == "http://myhost:8080"

    def test_strips_default_https_port(self) -> None:
        assert dashboard_origin("https://host:443") == "https://host"

    def test_malformed_port_returns_empty(self) -> None:
        assert dashboard_origin("https://host:abc") == ""

    def test_ipv6_brackets_preserved(self) -> None:
        assert dashboard_origin("http://[::1]:8080") == "http://[::1]:8080"

    def test_ipv6_no_port(self) -> None:
        assert dashboard_origin("http://[::1]") == "http://[::1]"

    def test_ftp_scheme_rejected(self) -> None:
        assert dashboard_origin("ftp://host") == ""

    def test_file_scheme_rejected(self) -> None:
        assert dashboard_origin("file:///etc/passwd") == ""


class TestSchemeAgreement:
    """Verify parse_dashboard_url and dashboard_origin agree on scheme for bare hostnames."""

    def test_bare_hostname_gets_http(self) -> None:
        host, _ = parse_dashboard_url("myhost:9090")
        origin = dashboard_origin("myhost:9090")
        assert origin == f"http://{host}:9090"


_MOD = "kiro_claw.dashboard.origin"


class TestBuildDashboardUrl:
    def test_token_appended(self) -> None:
        assert build_dashboard_url("http://localhost:5476", "abc") == "http://localhost:5476?token=abc"

    def test_empty_token_returns_bare_url(self) -> None:
        assert build_dashboard_url("http://localhost:5476") == "http://localhost:5476"

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:5476", "", local_only=False)

    def test_local_without_token_ok(self) -> None:
        assert build_dashboard_url("http://localhost:5476", "", local_only=True) == "http://localhost:5476"

    def test_not_local_with_token_ok(self) -> None:
        url = build_dashboard_url("http://host:5476", "tok", local_only=False)
        assert url == "http://host:5476?token=tok"

    def test_special_chars_in_token_are_encoded(self) -> None:
        url = build_dashboard_url("http://localhost:5476", "a&b=c#d")
        assert url == "http://localhost:5476?token=a%26b%3Dc%23d"

    def test_truthy_non_bool_local_only_still_requires_token(self) -> None:
        """AutoSDE hardening: 'local_only is not True' catches truthy non-booleans."""
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:5476", "", local_only="yes")  # type: ignore[arg-type]


class TestFormatDashboardUrls:
    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_local_direct_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476)
        assert len(lines) == 2
        assert lines[0] == "🐾 Dashboard:"
        assert "http://localhost:5476" in lines[1]

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_in_url_shown(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476?token=abc", port=5476)
        assert "token=abc" in lines[1]

    @patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 1234 5.6.7.8 5678"}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost")
    @patch(f"{_MOD}.socket.gethostbyname", side_effect=socket.gaierror)
    def test_remote_ssh_tunnel_instructions(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476?token=t", port=5476)
        assert any("ssh -NL 5476:localhost:5476 myhost" in ln for ln in lines)
        assert any("http://localhost:5476?token=t" in ln for ln in lines)
        assert any("systemd" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.corp.amazon.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_local_with_resolvable_host_adds_remote_hint(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, local_only=True)
        assert any("Remote" in ln and "ssh -NL" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.corp.amazon.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_custom_host_suppresses_remote_hint(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, has_custom_host=True)
        assert not any("Remote" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_shown_when_not_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:5476?token=t", port=5476, local_only=False)
        assert any("Proxy" in ln and "proxy.devspaces" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_hidden_when_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:5476", port=5476, local_only=True)
        assert not any("Proxy" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_propagated_to_proxy_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:5476?token=abc", port=5476, local_only=False)
        proxy_line = [ln for ln in lines if "Proxy" in ln][0]
        assert "proxy.devspaces.amazon.com?token=abc" in proxy_line

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476", port=5476, local_only=False)

    def test_not_local_with_non_token_query_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476?debug=1", port=5476, local_only=False)

    def test_truthy_non_bool_local_only_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:5476", port=5476, local_only="yes")  # type: ignore[arg-type]


class TestCheckOriginLoopbackTrust:
    """check_origin tightened (CSE SEC-016): only the bound port and explicitly
    opted-in loopback ports are trusted — not every loopback port."""

    def _make_request(self, origin: str, remote: str = "127.0.0.1", allowed=None,
                      host: str = "") -> object:
        """Create a minimal mock request with Origin header and allowed_origins.

        *host* sets the request ``Host`` header (used by the same-origin
        loopback fallback — Mesh-1864).
        """
        from unittest.mock import MagicMock

        request = MagicMock()
        headers = {}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        request.headers = headers
        request.remote = remote
        # Only allow port 5476 — simulates the default config
        if allowed is None:
            allowed = {"http://localhost:5476", "http://127.0.0.1:5476"}
        request.app = {"allowed_origins": allowed}
        return request

    def test_localhost_different_port_rejected_by_default(self) -> None:
        """A loopback origin on a non-bound port is NOT trusted by default (CSRF guard)."""
        request = self._make_request("http://localhost:8777")
        assert check_origin(request) is False

    def test_127_0_0_1_different_port_rejected_by_default(self) -> None:
        request = self._make_request("http://127.0.0.1:9999")
        assert check_origin(request) is False

    def test_opted_in_loopback_port_trusted(self) -> None:
        """A loopback port the operator added (via KIROCLAW_ALLOWED_LOOPBACK_PORTS,
        folded into allowed_origins) is accepted — SSH-tunnel support, opt-in."""
        allowed = {
            "http://localhost:5476",
            "http://127.0.0.1:5476",
            "http://localhost:8777",
            "http://127.0.0.1:8777",
        }
        request = self._make_request("http://localhost:8777", allowed=allowed)
        assert check_origin(request) is True

    def test_exact_match_still_works(self) -> None:
        """Standard case: origin matches allowed set exactly."""
        request = self._make_request("http://localhost:5476")
        assert check_origin(request) is True

    def test_non_loopback_origin_rejected(self) -> None:
        """Remote origin not in allowed set should be rejected."""
        request = self._make_request("http://evil.com:5476")
        assert check_origin(request) is False

    def test_no_origin_loopback_remote_trusted(self) -> None:
        """No Origin header from loopback remote (local process) is trusted."""
        request = self._make_request("", remote="127.0.0.1")
        assert check_origin(request) is True

    def test_no_origin_non_loopback_remote_rejected(self) -> None:
        """No Origin header from non-loopback remote is rejected."""
        request = self._make_request("", remote="10.0.0.5")
        assert check_origin(request) is False

    # --- Mesh-1864: same-origin loopback fallback (embedded multi-instance iframe) ---

    def test_same_origin_loopback_port_trusted(self) -> None:
        """The embedded instance iframe is served at <host>:<tunnelPort> and opens
        its WS to that same location.host, so Origin == Host. Trust it even though
        the port is not in allowed_origins (Mesh-1864)."""
        request = self._make_request(
            "http://kiroclaw.localhost:7779",
            host="kiroclaw.localhost:7779",
        )
        assert check_origin(request) is True

    def test_same_origin_127_loopback_port_trusted(self) -> None:
        request = self._make_request(
            "http://127.0.0.1:8777", host="127.0.0.1:8777"
        )
        assert check_origin(request) is True

    def test_origin_host_mismatch_rejected(self) -> None:
        """SEC-016 boundary preserved: a malicious local page on an arbitrary port
        sends its own Origin while the Host is the gateway's — they differ, so the
        same-origin fallback must NOT trust it."""
        request = self._make_request(
            "http://localhost:9999", host="kiroclaw.localhost:7779"
        )
        assert check_origin(request) is False

    def test_same_origin_non_loopback_not_trusted_by_fallback(self) -> None:
        """The fallback is loopback-only: a public host with Origin == Host must
        still go through the allowlist (not auto-trusted)."""
        request = self._make_request(
            "http://evil.com:7779", host="evil.com:7779"
        )
        assert check_origin(request) is False

    def test_same_origin_missing_host_header_rejected(self) -> None:
        """No Host header -> the same-origin fallback cannot confirm a match."""
        request = self._make_request("http://localhost:8777")
        assert check_origin(request) is False


class TestAllowedLoopbackPortsEnv:
    """KIROCLAW_ALLOWED_LOOPBACK_PORTS opts specific loopback ports into the allowed set."""

    @patch.dict("os.environ", {"KIROCLAW_ALLOWED_LOOPBACK_PORTS": "8777,9000"}, clear=True)
    def test_env_ports_added(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert "http://localhost:8777" in origins
        assert "http://127.0.0.1:8777" in origins
        assert "http://[::1]:8777" in origins
        assert "http://localhost:9000" in origins

    @patch.dict("os.environ", {"KIROCLAW_ALLOWED_LOOPBACK_PORTS": "notaport"}, clear=True)
    def test_non_numeric_ignored(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert not any(":notaport" in o for o in origins)

    @patch.dict("os.environ", {}, clear=True)
    def test_no_env_only_bound_port(self) -> None:
        origins = build_allowed_origins(7777, local_only=True)
        assert "http://localhost:7777" in origins
        assert "http://localhost:8777" not in origins


class TestShouldCanonicalizeHost:
    """Loopback host canonicalization for the SPA's per-origin localStorage."""

    def test_redirects_localhost_to_canonical_document_nav(self) -> None:
        assert should_canonicalize_host(
            "localhost:7777",
            "kiroclaw.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_redirects_127_to_canonical(self) -> None:
        assert should_canonicalize_host(
            "127.0.0.1:7777", "localhost", method="GET", sec_fetch_dest="document"
        )

    def test_no_redirect_when_already_canonical(self) -> None:
        assert not should_canonicalize_host(
            "kiroclaw.localhost:7777",
            "kiroclaw.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_no_redirect_for_non_document_dest(self) -> None:
        # XHR / fetch / websocket / sub-resource requests must never be redirected.
        for dest in ("empty", "websocket", "script", "style", "image", None):
            assert not should_canonicalize_host(
                "localhost:7777",
                "kiroclaw.localhost",
                method="GET",
                sec_fetch_dest=dest,
            )

    def test_no_redirect_for_mutating_methods(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert not should_canonicalize_host(
                "localhost:7777",
                "kiroclaw.localhost",
                method=method,
                sec_fetch_dest="document",
            )

    def test_no_redirect_for_non_loopback_request_host(self) -> None:
        # A real hostname / reverse-proxy vhost is never canonicalized.
        assert not should_canonicalize_host(
            "kiroclaw.example.com:7777",
            "kiroclaw.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_no_redirect_when_canonical_not_loopback(self) -> None:
        assert not should_canonicalize_host(
            "localhost:7777",
            "kiroclaw.example.com",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_host_without_port(self) -> None:
        assert should_canonicalize_host(
            "localhost", "kiroclaw.localhost", method="GET", sec_fetch_dest="document"
        )

    def test_ipv6_loopback_bracket_host_redirected(self) -> None:
        # [::1]:7777 must parse to ::1 (not "[") and converge like other loopbacks.
        assert should_canonicalize_host(
            "[::1]:7777",
            "kiroclaw.localhost",
            method="GET",
            sec_fetch_dest="document",
        )

    def test_ipv6_loopback_bracket_host_without_port(self) -> None:
        assert should_canonicalize_host(
            "[::1]", "kiroclaw.localhost", method="GET", sec_fetch_dest="document"
        )


class TestResolveDashboardHost:
    """Canonical loopback host must be plain ``localhost`` (resolves everywhere,
    including Safari / SSH tunnels — unlike ``*.localhost``)."""

    def test_local_only_returns_localhost(self) -> None:
        assert resolve_dashboard_host(local_only=True) == "localhost"

    def test_configured_host_wins(self) -> None:
        assert (
            resolve_dashboard_host(local_only=True, configured_host="myhost.example")
            == "myhost.example"
        )


class TestBuildHostCanonicalRedirect:
    """End-to-end tests for the extracted 302 middleware (runtime behavior)."""

    @pytest.mark.asyncio
    async def test_document_nav_302s_preserving_port_path_query(self) -> None:
        from urllib.parse import urlsplit

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("kiroclaw.localhost")])
        app.router.add_get("/chat", _ok)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/chat",
                params={"token": "abc123"},
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert resp.status == 302
            loc = urlsplit(resp.headers["Location"])
            assert loc.hostname == "kiroclaw.localhost"  # host converged
            assert loc.port == 7777  # port preserved
            assert loc.path == "/chat"  # path preserved
            assert "token=abc123" in loc.query  # ?token= preserved

    @pytest.mark.asyncio
    async def test_xhr_and_post_not_redirected(self) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("kiroclaw.localhost")])
        app.router.add_get("/api/x", _ok)
        app.router.add_post("/api/x", _ok)

        async with TestClient(TestServer(app)) as client:
            # XHR/fetch (Sec-Fetch-Dest: empty) on a loopback alias is NOT redirected.
            xhr = await client.get(
                "/api/x",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "empty"},
                allow_redirects=False,
            )
            assert xhr.status == 200
            # A mutating method is never redirected, even as a document nav.
            post = await client.post(
                "/api/x",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert post.status == 200

    @pytest.mark.asyncio
    async def test_empty_canonical_host_is_noop(self) -> None:
        # local_only=False passes canonical_host="" -> middleware never redirects
        # (reverse-proxy / remote-host deployments are untouched).
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_claw.dashboard.server import build_host_canonical_redirect

        async def _ok(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(middlewares=[build_host_canonical_redirect("")])
        app.router.add_get("/chat", _ok)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/chat",
                headers={"Host": "localhost:7777", "Sec-Fetch-Dest": "document"},
                allow_redirects=False,
            )
            assert resp.status == 200
