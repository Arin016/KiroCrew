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
)


class TestBuildAllowedOrigins:
    def test_default_origins(self) -> None:
        origins = build_allowed_origins(8765, local_only=True)
        assert "http://127.0.0.1:8765" in origins
        assert "http://localhost:8765" in origins
        assert "http://kiroclaw.localhost:8765" in origins

    def test_configured_host_adds_http_with_port(self) -> None:
        origins = build_allowed_origins(8765, local_only=True, configured_host="myhost")
        assert "http://myhost:8765" in origins

    def test_dashboard_url_empty_no_extra_origin(self) -> None:
        baseline = build_allowed_origins(8765, local_only=True)
        with_empty = build_allowed_origins(8765, local_only=True, dashboard_url="")
        assert baseline == with_empty

    def test_dashboard_url_https_adds_origin(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="https://kiroclaw.local"
        )
        assert "https://kiroclaw.local" in origins

    def test_dashboard_url_http_with_port(self) -> None:
        origins = build_allowed_origins(8765, local_only=True, dashboard_url="http://myhost:8080")
        assert "http://myhost:8080" in origins

    def test_dashboard_url_no_scheme_normalized(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="myhost:8080"
        )
        assert "http://myhost:8080" in origins

    def test_dashboard_url_preserves_existing_origins(self) -> None:
        origins = build_allowed_origins(
            8765,
            local_only=True,
            configured_host="myhost",
            dashboard_url="https://kiroclaw.local",
        )
        assert "http://myhost:8765" in origins
        assert "https://kiroclaw.local" in origins
        assert "http://localhost:8765" in origins

    def test_dashboard_url_strips_default_https_port(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="https://kiroclaw.local:443"
        )
        assert "https://kiroclaw.local" in origins
        assert "https://kiroclaw.local:443" not in origins

    def test_dashboard_url_strips_default_http_port(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="http://kiroclaw.local:80"
        )
        assert "http://kiroclaw.local" in origins
        assert "http://kiroclaw.local:80" not in origins

    def test_dashboard_url_keeps_non_default_port(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="https://kiroclaw.local:8443"
        )
        assert "https://kiroclaw.local:8443" in origins

    def test_dashboard_url_malformed_port_ignored(self) -> None:
        origins = build_allowed_origins(
            8765, local_only=True, dashboard_url="https://host:abc"
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
        assert build_dashboard_url("http://localhost:8765", "abc") == "http://localhost:8765?token=abc"

    def test_empty_token_returns_bare_url(self) -> None:
        assert build_dashboard_url("http://localhost:8765") == "http://localhost:8765"

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:8765", "", local_only=False)

    def test_local_without_token_ok(self) -> None:
        assert build_dashboard_url("http://localhost:8765", "", local_only=True) == "http://localhost:8765"

    def test_not_local_with_token_ok(self) -> None:
        url = build_dashboard_url("http://host:8765", "tok", local_only=False)
        assert url == "http://host:8765?token=tok"

    def test_special_chars_in_token_are_encoded(self) -> None:
        url = build_dashboard_url("http://localhost:8765", "a&b=c#d")
        assert url == "http://localhost:8765?token=a%26b%3Dc%23d"

    def test_truthy_non_bool_local_only_still_requires_token(self) -> None:
        """AutoSDE hardening: 'local_only is not True' catches truthy non-booleans."""
        with pytest.raises(ValueError, match="token is required"):
            build_dashboard_url("http://host:8765", "", local_only="yes")  # type: ignore[arg-type]


class TestFormatDashboardUrls:
    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_local_direct_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765", port=8765)
        assert len(lines) == 2
        assert lines[0] == "🐾 Dashboard:"
        assert "http://localhost:8765" in lines[1]

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_in_url_shown(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765?token=abc", port=8765)
        assert "token=abc" in lines[1]

    @patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 1234 5.6.7.8 5678"}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost")
    @patch(f"{_MOD}.socket.gethostbyname", side_effect=socket.gaierror)
    def test_remote_ssh_tunnel_instructions(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765?token=t", port=8765)
        assert any("ssh -NL 8765:localhost:8765 myhost" in ln for ln in lines)
        assert any("http://localhost:8765?token=t" in ln for ln in lines)
        assert any("systemd" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.corp.amazon.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_local_with_resolvable_host_adds_remote_hint(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765", port=8765, local_only=True)
        assert any("Remote" in ln and "ssh -NL" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value=None)
    @patch(f"{_MOD}.machine_hostname", return_value="myhost.corp.amazon.com")
    @patch(f"{_MOD}.socket.gethostbyname", return_value="10.0.0.1")
    def test_custom_host_suppresses_remote_hint(self, _dns: object, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765", port=8765, has_custom_host=True)
        assert not any("Remote" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_shown_when_not_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:8765?token=t", port=8765, local_only=False)
        assert any("Proxy" in ln and "proxy.devspaces" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_devspaces_proxy_hidden_when_local(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://localhost:8765", port=8765, local_only=True)
        assert not any("Proxy" in ln for ln in lines)

    @patch.dict("os.environ", {}, clear=True)
    @patch(f"{_MOD}.devspaces_proxy_url", return_value="https://proxy.devspaces.amazon.com")
    @patch(f"{_MOD}.machine_hostname", return_value="localhost")
    def test_token_propagated_to_proxy_url(self, _mh: object, _dp: object) -> None:
        lines = format_dashboard_urls("http://host:8765?token=abc", port=8765, local_only=False)
        proxy_line = [ln for ln in lines if "Proxy" in ln][0]
        assert "proxy.devspaces.amazon.com?token=abc" in proxy_line

    def test_not_local_without_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:8765", port=8765, local_only=False)

    def test_not_local_with_non_token_query_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:8765?debug=1", port=8765, local_only=False)

    def test_truthy_non_bool_local_only_raises(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            format_dashboard_urls("http://host:8765", port=8765, local_only="yes")  # type: ignore[arg-type]


class TestCheckOriginLoopbackTrust:
    """check_origin tightened (CSE SEC-016): only the bound port and explicitly
    opted-in loopback ports are trusted — not every loopback port."""

    def _make_request(self, origin: str, remote: str = "127.0.0.1", allowed=None) -> object:
        """Create a minimal mock request with Origin header and allowed_origins."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"Origin": origin} if origin else {}
        request.remote = remote
        # Only allow port 8765 — simulates the default config
        if allowed is None:
            allowed = {"http://localhost:8765", "http://127.0.0.1:8765"}
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
            "http://localhost:8765",
            "http://127.0.0.1:8765",
            "http://localhost:8777",
            "http://127.0.0.1:8777",
        }
        request = self._make_request("http://localhost:8777", allowed=allowed)
        assert check_origin(request) is True

    def test_exact_match_still_works(self) -> None:
        """Standard case: origin matches allowed set exactly."""
        request = self._make_request("http://localhost:8765")
        assert check_origin(request) is True

    def test_non_loopback_origin_rejected(self) -> None:
        """Remote origin not in allowed set should be rejected."""
        request = self._make_request("http://evil.com:8765")
        assert check_origin(request) is False

    def test_no_origin_loopback_remote_trusted(self) -> None:
        """No Origin header from loopback remote (local process) is trusted."""
        request = self._make_request("", remote="127.0.0.1")
        assert check_origin(request) is True

    def test_no_origin_non_loopback_remote_rejected(self) -> None:
        """No Origin header from non-loopback remote is rejected."""
        request = self._make_request("", remote="10.0.0.5")
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
