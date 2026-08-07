"""Tests for kiro_crew.browser.setup — Playwright MCP setup (OSS stub).

The upstream build installed Playwright MCP via an Amazon-internal package
manager (AIM) and wired an Amazon-auth cookie/storage-state flow. In the
open-source build those steps are neutralized: ``is_playwright_installed``
always reports False (no internal package manager) and
``ensure_playwright_installed`` is a no-op. The generic Netscape cookie
parsing, Playwright config generation and storage-state refresh still work.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import kiro_crew.browser.setup as setup_mod
from kiro_crew.browser.setup import (
    _converge_playwright_agent_files,
    _drop_superseded_playwright,
    _entry_is_playwright_proxy,
    check_playwright_launchable,
    converge_playwright_servers,
    ensure_playwright_installed,
    generate_playwright_config,
    inject_cookies_via_playwright,
    is_headed,
    is_playwright_installed,
    migrate_owned_playwright_registration,
    patch_mcp_extension,
    patch_mcp_headless,
    refresh_storage_state,
    register_playwright_proxy,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform_compat import IS_POSIX

# Canonical slash-free key KiroCrew registers the Playwright proxy under.
_CANONICAL = mcp_server_alias("@playwright/mcp")  # "playwright-mcp"

# ── Sample cookie data ────────────────────────────────────────────────────────

SAMPLE_COOKIES = """\
# Netscape HTTP Cookie File
sso.example.com\tFALSE\t/\tTRUE\t9999999999\tuser_name\ttestuser
#HttpOnly_.sso.example.com\tTRUE\t/\tTRUE\t9999999999\ttpm_metrics\teyJTdHVmZg==
"""


# ── TestIsPlaywrightInstalled ────────────────────────────────────────────────


class TestIsPlaywrightInstalled:
    def test_returns_false_in_oss(self):
        # The Amazon-internal package manager that backed this check is not
        # shipped in OSS, so the stub always reports the package as not
        # installed (rather than shelling out to an internal tool).
        assert is_playwright_installed() is False


# ── TestEnsurePlaywrightInstalled ────────────────────────────────────────────


class TestEnsurePlaywrightInstalled:
    def test_is_noop_in_oss(self):
        # The upstream flow installed Playwright MCP via an Amazon-internal
        # package manager; that path is removed in OSS, so this is a no-op
        # that neither raises nor returns a value.
        assert ensure_playwright_installed() is None


# ── TestIsHeaded / TestGetPlaywrightMcpArgs ──────────────────────────────────


class TestIsHeaded:
    def test_headed_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        assert is_headed() is True

    def test_headless_on_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert is_headed() is False

    def test_headed_on_windows(self, monkeypatch):
        # Windows has a desktop session and interactive SSO — run a visible
        # Chromium window like macOS, not the Linux headless mode.
        monkeypatch.setattr("platform.system", lambda: "Windows")
        assert is_headed() is True


# ── TestInjectCookiesViaPlaywright ───────────────────────────────────────────


class TestInjectCookiesViaPlaywright:
    def test_returns_dict_with_cookies_and_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert "cookies" in result
        assert "count" in result

    def test_count_matches_cookies_length(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == len(result["cookies"])
        assert result["count"] == 2

    def test_parses_cookie_fields(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        names = {c["name"] for c in result["cookies"]}
        assert "user_name" in names
        assert "tpm_metrics" in names

    def test_default_path_used_when_no_cookie_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        result = inject_cookies_via_playwright()
        assert result["count"] == 2

    def test_missing_file_returns_empty_cookies(self, tmp_path: Path):
        missing = tmp_path / "no_such_cookie"
        result = inject_cookies_via_playwright(str(missing))
        assert result["cookies"] == []
        assert result["count"] == 0

    def test_httponly_cookie_parsed_correctly(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        httponly_cookies = [c for c in result["cookies"] if c.get("httpOnly")]
        assert len(httponly_cookies) == 1
        assert httponly_cookies[0]["name"] == "tpm_metrics"

    def test_empty_cookie_file_returns_zero_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == 0
        assert result["cookies"] == []


# ── TestGeneratePlaywrightConfig ─────────────────────────────────────────────


class TestGeneratePlaywrightConfig:
    def test_creates_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert config_path.exists()

    def test_does_not_write_remote_debugging_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # B-minus dropped the CDP debug port — the live mirror now rides the
        # proxy's existing screenshot path, so no remote-debugging port is opened.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        args = config["browser"]["launchOptions"]["args"]
        assert not any("remote-debugging-port" in a for a in args)

    def test_config_has_correct_structure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "browser" in config
        assert "capabilities" in config
        assert config["browser"]["browserName"] == "chromium"
        # contextOptions is always present; ``storageState`` is conditional on the
        # cookie jar existing (see TestGeneratePlaywrightConfigStorageState).
        assert isinstance(config["browser"]["contextOptions"], dict)

    def test_storage_state_path_is_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The config path now derives from config_dir(), which reads KIROCREW_HOME
        # first (the conftest autouse fixture pins it). Clear it so config_dir()
        # resolves from the patched Path.home -> ~/.kiro/crew under tmp_path.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # ``storageState`` is only emitted when the jar is on disk, so create it.
        jar = tmp_path / ".kiro" / "crew" / "playwright-storage-state.json"
        jar.parent.mkdir(parents=True, exist_ok=True)
        jar.write_text('{"cookies": [], "origins": []}')
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        storage_state = config["browser"]["contextOptions"]["storageState"]
        assert storage_state.startswith(str(tmp_path))
        assert "playwright-storage-state.json" in storage_state

    def test_config_written_to_kirocrew_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Data home moved from top-level ~/.kirocrew to ~/.kiro/crew (config_dir()).
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert ".kiro/crew" in str(config_path)
        assert config_path.name == "playwright-config.json"

    def test_parent_dir_created_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        crew_dir = tmp_path / ".kiro" / "crew"
        assert not crew_dir.exists()
        generate_playwright_config()
        assert crew_dir.exists()

    def test_config_pins_chromium_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Without this pin @playwright/mcp defaults launchOptions.channel to the
        # branded "chrome" channel, which overrides browserName and is absent on
        # headless/Cloud Desktop hosts; pin it to bundled "chromium".
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert config["browser"]["launchOptions"]["channel"] == "chromium"

    def test_config_runs_headless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The dashboard Browser panel mirror is the view surface, so the browser
        # runs headless — no visible OS window (and works on display-less Linux).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text(encoding="utf-8"))
        assert config["browser"]["launchOptions"]["headless"] is True


# ── TestBrowseSetupHelpers (guided one-command setup) ────────────────────────


class TestCheckPlaywrightLaunchable:
    def test_ok_when_resolver_returns_cmd(self, monkeypatch: pytest.MonkeyPatch):
        # setup.py imports _resolve_playwright_cmd at module scope, so patch the
        # name where it is looked up (setup_mod), not on the origin module.
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda: "/usr/bin/npx")
        ok, detail = check_playwright_launchable()
        assert ok is True
        assert detail == "/usr/bin/npx"

    def test_not_ok_with_install_hint_when_unresolvable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(setup_mod, "_resolve_playwright_cmd", lambda: None)
        ok, detail = check_playwright_launchable()
        assert ok is False
        assert "@playwright/mcp" in detail


class TestRegisterPlaywrightProxy:
    def test_creates_mcp_json_and_registers_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A fresh user has no ~/.kiro/settings/mcp.json; register creates it and
        # writes the canonical proxy entry so one command fully wires the panel.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert not mcp_json.exists()
        returned, status = register_playwright_proxy()
        assert returned == mcp_json and mcp_json.exists()
        assert status == "registered"
        servers = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]
        assert _CANONICAL in servers
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_registers_into_existing_mcp_json_without_clobbering_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = _write_mcp_json(tmp_path, {"other-mcp": {"command": "foo"}})
        _, status = register_playwright_proxy()
        assert status == "registered"
        servers = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]
        assert _CANONICAL in servers
        assert servers["other-mcp"] == {"command": "foo"}

    def test_keeps_user_direct_server_under_canonical_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A user hand-authored their OWN direct (non-proxy) server under the
        # canonical `playwright-mcp` key. `browse setup` must NOT overwrite it —
        # authorship is by launch target, not key name. Leave it byte-identical
        # and report kept-user-entry.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        _, status = register_playwright_proxy()
        assert status == "kept-user-entry"
        assert mcp_json.read_text(encoding="utf-8") == before


# ── TestRefreshStorageState ──────────────────────────────────────────────────


class TestRefreshStorageState:
    def test_returns_error_when_cookie_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "no_cookie"
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", missing)
        result = refresh_storage_state()
        assert result["ok"] is False
        # OSS build has no bundled browser-auth cookie source.
        assert "not available in OSS" in result["error"]

    def test_returns_error_when_no_cookies_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        result = refresh_storage_state()
        assert result["ok"] is False
        assert "no cookies" in result["error"]

    def test_success_creates_storage_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert result["ok"] is True
        assert result["count"] == 2
        storage_path = Path(result["path"])
        assert storage_path.exists()

    def test_success_storage_state_valid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        storage_path = Path(result["path"])
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        assert "cookies" in data
        assert "origins" in data
        assert len(data["cookies"]) == 2

    def test_success_returns_expired_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        sso_dir = tmp_path / ".sso"
        sso_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "SSO_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert "expired" in result
        assert isinstance(result["expired"], int)


# ── TestGeneratePlaywrightConfigStorageState ─────────────────────────────────


class TestGeneratePlaywrightConfigStorageState:
    """``storageState`` must only be wired in when the cookie jar exists.

    Playwright fails context creation on a missing storage-state path, which the
    agent sees as an opaque HTTP 400 on every browser call.
    """

    def _ctx_options(self, tmp_path: Path) -> dict:
        cfg = json.loads((tmp_path / ".kiro" / "crew" / "playwright-config.json").read_text())
        return cfg["browser"]["contextOptions"]

    def test_omitted_when_storage_state_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        generate_playwright_config()
        assert "storageState" not in self._ctx_options(tmp_path)

    def test_included_when_storage_state_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        state = tmp_path / ".kiro" / "crew" / "playwright-storage-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"cookies": [], "origins": []}')
        generate_playwright_config()
        assert self._ctx_options(tmp_path)["storageState"] == str(state)


# ── TestPatchWritesCanonicalKey ──────────────────────────────────────────────


def _write_mcp_json(tmp_path: Path, servers: dict) -> Path:
    """Seed ~/.kiro/settings/mcp.json under a monkeypatched home."""
    mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
    mcp_json.parent.mkdir(parents=True, exist_ok=True)
    mcp_json.write_text(json.dumps({"mcpServers": servers}, indent=2))
    return mcp_json


def _read_servers(mcp_json: Path) -> dict:
    return json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]


class TestPatchWritesCanonicalKey:
    """patch_mcp_* register under the canonical alias and drop superseded keys."""

    def test_headless_writes_canonical_and_drops_superseded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                "@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "other-mcp": {"command": "foo"},
            },
        )
        patch_mcp_headless()
        servers = _read_servers(mcp_json)
        # Exactly the canonical key + the untouched user server survive.
        assert set(servers) == {_CANONICAL, "other-mcp"}
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_extension_writes_canonical_and_drops_superseded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod.platform_compat, "chmod_safe", lambda *a, **k: None)
        mcp_json = _write_mcp_json(
            tmp_path,
            {"npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}},
        )
        patch_mcp_extension("tok-123")
        servers = _read_servers(mcp_json)
        assert set(servers) == {_CANONICAL}
        assert servers[_CANONICAL]["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] == "tok-123"

    def test_drop_superseded_never_drops_canonical(self, tmp_path, monkeypatch):
        # A superseded key is dropped ONLY when its spec is actually the proxy.
        # (home -> tmp_path so no real ownership manifest colors the decision.)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        servers = {
            _CANONICAL: {"command": "kirocrew"},
            "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert _CANONICAL in servers
        assert "playwright-proxy-mcp" not in servers

    def test_drop_superseded_preserves_user_direct_server(self, tmp_path, monkeypatch):
        # A user-declared DIRECT (non-proxy) server keyed under a superseded name
        # (@playwright/mcp pointing at the real npm package) is NOT KiroCrew's and
        # must survive — authorship is by launch target, not key name. No manifest
        # marks it, so it is preserved.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        servers = {
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "npm:@playwright/mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        # The proxy-spec superseded key is dropped; the user's direct one stays.
        assert "npm:@playwright/mcp" not in servers
        assert servers["@playwright/mcp"] == {
            "command": "npx",
            "args": ["@playwright/mcp@latest"],
        }

    def test_patch_records_owned_key_in_manifest(self, tmp_path, monkeypatch):
        # Arbiter regression (manifest-on-write): patch_mcp_* records the canonical
        # key it wrote in the KiroCrew-owned ownership manifest, so future
        # migrations have an explicit authorship signal (not just the argv
        # heuristic).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        _write_mcp_json(tmp_path, {"other-mcp": {"command": "foo"}})
        patch_mcp_headless()
        assert _CANONICAL in setup_mod._load_owned_mcp_keys()
        # Manifest file is owner-only.
        if IS_POSIX:
            mode = stat.S_IMODE(setup_mod._owned_mcp_keys_path().stat().st_mode)
            assert mode == 0o600

    def test_drop_superseded_uses_manifest_over_mutated_spec(self, tmp_path, monkeypatch):
        # A superseded key recorded in the manifest is KiroCrew's even if its spec
        # was later mutated to no longer look like the proxy — the explicit
        # ownership marker wins over the argv heuristic.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._record_owned_mcp_key("playwright-proxy-mcp")
        servers = {
            _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            # Spec no longer looks like the proxy, but the manifest says it's ours.
            "playwright-proxy-mcp": {"command": "wrapper", "args": ["--opaque"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert "playwright-proxy-mcp" not in servers

    def test_manifest_never_drops_unrecorded_user_direct_key(self, tmp_path, monkeypatch):
        # Defense: a manifest recording OTHER keys must not cause a user's direct
        # @playwright/mcp (not in the manifest, not a proxy) to be dropped.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._record_owned_mcp_key("playwright-proxy-mcp")
        servers = {
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert servers["@playwright/mcp"] == {"command": "npx", "args": ["@playwright/mcp@latest"]}


class TestPatchMalformedMcpJson:
    """A user-owned mcp.json may hold valid JSON that isn't an object, or an
    mcpServers that isn't a dict. patch_mcp_* guarded only JSONDecodeError/OSError,
    so data.setdefault / servers[...] raised an uncaught AttributeError/TypeError.
    The patcher must reset the bad shape and still register the canonical key."""

    @pytest.mark.parametrize("patcher", ["extension", "headless"])
    @pytest.mark.parametrize("bad", ["[]", "null", '"hi"', "42", '{"mcpServers": []}'])
    def test_non_object_shape_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patcher: str, bad: str
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_json.parent.mkdir(parents=True, exist_ok=True)
        mcp_json.write_text(bad)
        if patcher == "extension":
            patch_mcp_extension("tok-123")  # must not raise
        else:
            patch_mcp_headless()  # must not raise
        # The bad shape was reset and the canonical proxy key registered.
        servers = _read_servers(mcp_json)
        assert _CANONICAL in servers


# ── TestMigrateOwnedPlaywrightRegistration ───────────────────────────────────


class TestMigrateOwnedPlaywrightRegistration:
    def test_migrates_legacy_key_to_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                "@playwright/mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                }
            },
        )
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        assert set(servers) == {_CANONICAL}

    def test_migrates_legacy_direct_npm_entry_to_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 MEDIUM regression: KiroCrew's ORIGINAL boot migration upgraded a
        # legacy DIRECT npm-launched Playwright (key `npm:@playwright/mcp`, command
        # not the proxy) to the compression proxy. That direct->proxy upgrade must
        # still happen — the `npm:` key is a KiroCrew install artifact, so a direct
        # spec under it is ours to migrate (and remove), not left behind.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = _write_mcp_json(
            tmp_path,
            {"npm:@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]}},
        )
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        # Upgraded: canonical proxy present, the legacy direct entry removed.
        assert set(servers) == {_CANONICAL}
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]

    def test_drop_superseded_removes_legacy_direct_npm_key(self):
        # The `npm:@playwright/mcp` key is a KiroCrew artifact: its DIRECT spec is
        # dropped when KiroCrew rewrites its registration (so no second backend
        # lingers). The bare `@playwright/mcp` direct key is NOT (user may own it).
        servers = {
            "npm:@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        }
        _drop_superseded_playwright(servers, _CANONICAL)
        assert "npm:@playwright/mcp" not in servers
        assert servers["@playwright/mcp"] == {"command": "npx", "args": ["@playwright/mcp@latest"]}

    def test_noop_when_already_canonical_no_churn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                }
            },
        )
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        # Byte-identical: an already-canonical proxy is left untouched (no churn).
        assert mcp_json.read_text(encoding="utf-8") == before

    def test_does_not_add_when_no_playwright(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        mcp_json = _write_mcp_json(tmp_path, {"some-user-mcp": {"command": "foo"}})
        migrate_owned_playwright_registration()
        servers = _read_servers(mcp_json)
        assert set(servers) == {"some-user-mcp"}
        assert _CANONICAL not in servers

    def test_noop_when_mcp_json_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # No mcp.json, no agent dirs — must not raise.
        migrate_owned_playwright_registration()

    def test_leaves_user_direct_playwright_server_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # BLOCK-finding regression: a user's DIRECT @playwright/mcp entry in
        # kiro's mcp.json (a superseded KEY, but a non-proxy spec) must not be
        # rewritten or dropped by the boot-time convergence — its key name is
        # not proof of KiroCrew authorship.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(tmp_path, {"@playwright/mcp": dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        # Byte-identical: the user's direct server was left exactly as-is.
        assert mcp_json.read_text(encoding="utf-8") == before
        assert _read_servers(mcp_json)["@playwright/mcp"] == direct

    def test_leaves_user_direct_server_under_canonical_key_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT-review regression: a user's DIRECT (non-proxy) server keyed under
        # the CANONICAL `playwright-mcp` key must not be clobbered on boot. With
        # no superseded proxy to migrate, and canonical held by a user entry,
        # the guard must return before calling patch_mcp_* (which would do
        # servers[canonical] = proxy_entry and destroy the config every restart).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        mcp_json = _write_mcp_json(tmp_path, {_CANONICAL: dict(direct)})
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        assert mcp_json.read_text(encoding="utf-8") == before
        assert _read_servers(mcp_json)[_CANONICAL] == direct

    def test_leaves_user_canonical_even_when_superseded_proxy_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Defense in depth: a user owns the canonical key with a direct server
        # AND a legacy proxy also exists. Migrating would overwrite the user's
        # canonical entry, so the guard leaves mcp.json untouched (the legacy
        # proxy is still folded for display/launch by the read/pool layers).
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        mcp_json = _write_mcp_json(
            tmp_path,
            {
                _CANONICAL: dict(direct),
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            },
        )
        before = mcp_json.read_text(encoding="utf-8")
        migrate_owned_playwright_registration()
        assert mcp_json.read_text(encoding="utf-8") == before


# ── TestConvergePlaywrightServers ────────────────────────────────────────────


class TestConvergePlaywrightServers:
    def test_collapses_canonical_and_legacy_by_target(self):
        cfg = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "user-helper": {"command": "node", "args": ["h.js"]},
            },
            "tools": ["@playwright-proxy-mcp", "@other"],
            "allowedTools": [f"@{_CANONICAL}", "@playwright-proxy-mcp"],
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL, "user-helper"}
        # Dropped @ref rewritten to canonical; allowedTools de-duped.
        assert cfg["tools"] == [f"@{_CANONICAL}", "@other"]
        assert cfg["allowedTools"] == [f"@{_CANONICAL}"]

    def test_renames_sole_legacy_to_canonical(self):
        cfg = {
            "mcpServers": {
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        assert "--config" in cfg["mcpServers"][_CANONICAL]["args"]

    def test_noop_when_only_canonical(self):
        cfg = {
            "mcpServers": {_CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}}
        }
        assert converge_playwright_servers(cfg) is False

    def test_noop_when_no_playwright(self):
        cfg = {"mcpServers": {"foo": {"command": "x"}}}
        assert converge_playwright_servers(cfg) is False

    def test_ignores_non_proxy_server_named_playwright(self):
        # A user server whose name contains "playwright" but does NOT launch the
        # proxy must not be matched or rewritten.
        spec = {"command": "node", "args": ["my-playwright-helper.js"]}
        assert _entry_is_playwright_proxy("my-playwright-helper", spec, _CANONICAL) is False
        cfg = {"mcpServers": {"my-playwright-helper": spec}}
        assert converge_playwright_servers(cfg) is False

    def test_preserves_user_direct_playwright_under_superseded_key(self):
        # BLOCK-finding regression: a user hand-declares a DIRECT (non-proxy)
        # @playwright/mcp server (the real npm package, a superseded KEY name).
        # Authorship is by launch target, so this is NOT collapsed/dropped even
        # though its key is in _SUPERSEDED_PLAYWRIGHT_KEYS.
        direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        assert _entry_is_playwright_proxy("@playwright/mcp", direct, _CANONICAL) is False
        cfg = {"mcpServers": {"@playwright/mcp": direct}}
        assert converge_playwright_servers(cfg) is False
        assert cfg["mcpServers"]["@playwright/mcp"] == direct

    def test_collapses_proxy_but_keeps_user_direct_alongside(self):
        # A real proxy (legacy key) AND a user's direct @playwright/mcp coexist:
        # the proxy converges onto the canonical key; the user's direct server is
        # untouched.
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        cfg = {
            "mcpServers": {
                "@playwright/mcp": direct,
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert cfg["mcpServers"]["@playwright/mcp"] == direct
        assert _CANONICAL in cfg["mcpServers"]
        assert "playwright-proxy-mcp" not in cfg["mcpServers"]

    def test_user_direct_under_canonical_key_never_clobbered_by_legacy_proxy(self):
        # GPT 5.6 HIGH regression: a user's DIRECT (non-proxy) server occupies the
        # canonical `playwright-mcp` key while a legacy KiroCrew proxy sits under
        # another key. The survivor selection must NOT pick the non-proxy canonical
        # entry and delete the real proxy — that would silently destroy KiroCrew's
        # proxy. Since there is only one proxy and it can't move onto the user's
        # canonical slot, nothing collapses and the config is left untouched.
        user_direct = {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}
        proxy = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "x"]}
        cfg = {
            "mcpServers": {
                _CANONICAL: user_direct,
                "playwright-proxy-mcp": proxy,
            }
        }
        assert converge_playwright_servers(cfg) is False
        # Both entries survive byte-identical: the user's canonical direct server
        # AND KiroCrew's proxy under its own legacy key.
        assert cfg["mcpServers"][_CANONICAL] == user_direct
        assert cfg["mcpServers"]["playwright-proxy-mcp"] == proxy

    def test_extension_survivor_wins_over_headless_when_extension_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 MEDIUM regression: an active --extension entry and a stale
        # --config headless entry coexist. The headless entry has MORE args, so
        # arg-count alone would let it win and silently disable extension mode.
        # With extension mode enabled, the --extension entry must survive.
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: True)
        ext = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        headless = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
        }
        cfg = {"mcpServers": {_CANONICAL: headless, "playwright-proxy-mcp": ext}}
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        # The extension entry won despite having fewer args.
        assert "--extension" in cfg["mcpServers"][_CANONICAL]["args"]

    def test_headless_survivor_wins_over_extension_when_config_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Mirror: with extension mode OFF, the --config headless entry is the one
        # that matches the current mode and must survive.
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        ext = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension", "--foo", "--bar"],
        }
        headless = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
        }
        cfg = {"mcpServers": {_CANONICAL: ext, "playwright-proxy-mcp": headless}}
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        assert "--config" in cfg["mcpServers"][_CANONICAL]["args"]
        assert "--extension" not in cfg["mcpServers"][_CANONICAL]["args"]

    def test_survivor_is_most_complete_even_when_canonical_is_bare(self):
        # GPT 5.6 HIGH regression: a BARE canonical proxy coexists with a
        # fully-wired legacy proxy (--config/token). The survivor must be the
        # WIRED spec (never discard a working configuration for a bare
        # duplicate), stored under the canonical key.
        bare_canon = {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}
        wired_legacy = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/x/pw.json"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        cfg = {
            "mcpServers": {
                _CANONICAL: bare_canon,
                "playwright-proxy-mcp": wired_legacy,
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert set(cfg["mcpServers"]) == {_CANONICAL}
        # The wired spec survived under the canonical key; the bare one is gone.
        assert cfg["mcpServers"][_CANONICAL] == wired_legacy

    def test_dedupes_multiple_legacy_proxies_without_touching_user_canonical(self):
        # Two legacy proxies coexist with a user's direct server under canonical.
        # The two proxies must collapse to one (still under a legacy key, never
        # onto the user's canonical slot); the user's canonical entry is untouched.
        user_direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        proxy_full = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "x"]}
        proxy_bare = {"command": "kirocrew", "args": ["mcp-playwright-proxy"]}
        cfg = {
            "mcpServers": {
                _CANONICAL: user_direct,
                "playwright-proxy-mcp": proxy_full,
                "npm:@playwright/mcp": proxy_bare,
            }
        }
        assert converge_playwright_servers(cfg) is True
        assert cfg["mcpServers"][_CANONICAL] == user_direct
        # Exactly one proxy survives (the more completely-wired one); the user's
        # canonical direct server is preserved alongside it.
        surviving_proxies = [
            n for n, s in cfg["mcpServers"].items() if "mcp-playwright-proxy" in s.get("args", [])
        ]
        assert len(surviving_proxies) == 1
        assert cfg["mcpServers"][surviving_proxies[0]] == proxy_full


# ── TestConvergePlaywrightAgentFiles ─────────────────────────────────────────


class TestConvergePlaywrightAgentFiles:
    def test_sweeps_kiro_and_cc_agent_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        cc_dir = tmp_path / ".claude" / "agents"
        kiro_dir.mkdir(parents=True)
        cc_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        kiro_file = kiro_dir / "kirocrew.json"
        cc_file = cc_dir / "kirocrew.mcp.json"
        kiro_file.write_text(json.dumps(dup))
        cc_file.write_text(json.dumps(dup))
        # A .bak file must be skipped (only the exact owned filenames are swept).
        (kiro_dir / "kirocrew.json.bak.123").write_text(json.dumps(dup))

        _converge_playwright_agent_files()

        assert set(json.loads(kiro_file.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert set(json.loads(cc_file.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        # The .bak file was NOT swept (still holds the duplicate).
        assert (
            "playwright-proxy-mcp"
            in json.loads((kiro_dir / "kirocrew.json.bak.123").read_text(encoding="utf-8"))[
                "mcpServers"
            ]
        )

    def test_no_error_when_dirs_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _converge_playwright_agent_files()  # must not raise

    def test_leaves_user_owned_agent_files_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 HIGH regression: the sweep rewrites ONLY the EXACT filenames
        # KiroCrew generates (an explicit allowlist, not a ``kirocrew*`` prefix
        # glob). A user's own agent — including one they named
        # ``kirocrew-custom.json`` — may carry intentionally distinct proxy
        # entries; a restart must not collapse them and overwrite the user's file.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        cc_dir = tmp_path / ".claude" / "agents"
        kiro_dir.mkdir(parents=True)
        cc_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                },
                "playwright-proxy-mcp": {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
            }
        }
        # KiroCrew-owned (exact generated names): swept.
        owned = kiro_dir / "kirocrew.json"
        owned_variant = kiro_dir / "kirocrew-research.json"
        # User-owned: left alone — incl. a ``kirocrew``-PREFIXED custom name that
        # a prefix glob would wrongly have matched, and unrelated names.
        user_prefixed = kiro_dir / "kirocrew-custom.json"
        user_kiro = kiro_dir / "my-custom-agent.json"
        user_cc = cc_dir / "my-agent.mcp.json"
        for f in (owned, owned_variant, user_prefixed, user_kiro, user_cc):
            f.write_text(json.dumps(dup))

        _converge_playwright_agent_files()

        # KiroCrew-owned files converged to one server.
        assert set(json.loads(owned.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert set(json.loads(owned_variant.read_text(encoding="utf-8"))["mcpServers"]) == {
            _CANONICAL
        }
        # User-owned files byte-identical (both proxies preserved).
        assert (
            "playwright-proxy-mcp"
            in json.loads(user_prefixed.read_text(encoding="utf-8"))["mcpServers"]
        )
        assert (
            "playwright-proxy-mcp"
            in json.loads(user_kiro.read_text(encoding="utf-8"))["mcpServers"]
        )
        assert (
            "playwright-proxy-mcp" in json.loads(user_cc.read_text(encoding="utf-8"))["mcpServers"]
        )

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_preserves_0600_file_mode_on_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # GPT 5.6 HIGH regression: an agent config holding MCP env credentials may
        # be mode 0600. The convergence sweep's atomic write must NOT recreate it
        # with the umask default (0644) and expose secrets to other local users.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kiro_dir = tmp_path / ".kiro" / "agents"
        kiro_dir.mkdir(parents=True)
        dup = {
            "mcpServers": {
                _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--config", "x"],
                    "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "secret"},
                },
            }
        }
        secret_file = kiro_dir / "kirocrew.json"
        secret_file.write_text(json.dumps(dup))
        os.chmod(secret_file, 0o600)

        _converge_playwright_agent_files()

        # Converged (one server left) AND still owner-only readable.
        assert set(json.loads(secret_file.read_text(encoding="utf-8"))["mcpServers"]) == {
            _CANONICAL
        }
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


# ── TestConvergeKirocrewMcpJson ──────────────────────────────────────────────


class TestConvergeKirocrewMcpJson:
    """Arbiter regression: KiroCrew's own <data-home>/mcp.json is healed at the
    source, so a stale proxy key there isn't re-injected into the agent config on
    every rebuild for the per-rebuild backstop to undo forever."""

    def test_converges_stale_proxy_key_at_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        f.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        _CANONICAL: {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy", "--config", "x"],
                        },
                        "playwright-proxy-mcp": {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy"],
                        },
                    }
                }
            )
        )
        setup_mod._converge_kirocrew_mcp_json()
        # The stale duplicate proxy is gone at the source.
        assert set(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}

    def test_noop_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        setup_mod._converge_kirocrew_mcp_json()  # must not raise

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_preserves_file_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        f.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        _CANONICAL: {"command": "kirocrew", "args": ["mcp-playwright-proxy"]},
                        "playwright-proxy-mcp": {
                            "command": "kirocrew",
                            "args": ["mcp-playwright-proxy", "--config", "x"],
                        },
                    }
                }
            )
        )
        os.chmod(f, 0o600)
        setup_mod._converge_kirocrew_mcp_json()
        assert set(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == {_CANONICAL}
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_leaves_user_direct_server_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A user's direct (non-proxy) server in <data-home>/mcp.json is not a
        # proxy, so convergence is a no-op and the file is left byte-identical.
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        d = config_dir()
        f = d / "mcp.json"
        original = json.dumps(
            {"mcpServers": {"@playwright/mcp": {"command": "npx", "args": ["@playwright/mcp"]}}}
        )
        f.write_text(original)
        setup_mod._converge_kirocrew_mcp_json()
        assert f.read_text(encoding="utf-8") == original


# ── TestConvergeDropLogging / owned-filename source of truth ──────────────────


class TestConvergeForensics:
    def test_dropped_spec_logged_with_env_values_redacted(self, caplog):
        # Arbiter regression: a dropped proxy's spec is logged in full (so a
        # wrongly-deleted entry can be reconstructed) but its env VALUES are
        # masked — a token like PLAYWRIGHT_MCP_EXTENSION_TOKEN never hits the log.
        import logging as _logging

        cfg = {
            "mcpServers": {
                _CANONICAL: {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy", "--extension"],
                },
                "playwright-proxy-mcp": {
                    "command": "kirocrew",
                    "args": ["mcp-playwright-proxy"],
                    "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "super-secret-token"},
                },
            }
        }
        with caplog.at_level(_logging.INFO, logger="kiro_crew.browser.setup"):
            assert converge_playwright_servers(cfg) is True
        log_text = caplog.text
        # The dropped key + its arg wiring are diagnosable; the token is not.
        assert "playwright-proxy-mcp" in log_text
        assert "super-secret-token" not in log_text
        assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" in log_text  # key kept, value masked

    def test_redact_spec_for_log_masks_env_values_only(self):
        spec = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"TOK": "secret", "OTHER": "also-secret"},
        }
        safe = setup_mod._redact_spec_for_log(spec)
        assert safe["command"] == "kirocrew"
        assert safe["args"] == ["mcp-playwright-proxy", "--extension"]
        assert safe["env"] == {"TOK": "***", "OTHER": "***"}
        # Original spec is not mutated.
        assert spec["env"]["TOK"] == "secret"

    def test_owned_allowlist_is_the_leaf_module_source_of_truth(self):
        # Item 2 regression: the sweep's allowlist IS the agent_files leaf module
        # (not a hand-copied literal), so adding a managed spec in one place is
        # picked up here with no drift.
        from kiro_crew import agent_files

        assert setup_mod._OWNED_KIRO_AGENT_FILES is agent_files.OWNED_KIRO_AGENT_FILES
        assert setup_mod._OWNED_CC_AGENT_FILES is agent_files.OWNED_CC_AGENT_FILES
        # And agent.py's own filename constants come from the same leaf module.
        from kiro_crew import agent as agent_mod

        assert agent_mod.AGENT_FILENAME == agent_files.AGENT_FILENAME
        assert agent_mod.AGENT_FILENAME in agent_files.OWNED_KIRO_AGENT_FILES


# ── TestSyncAgentSpecsProxyEntry ─────────────────────────────────────────────


class TestSyncAgentSpecsProxyEntry:
    """kiro-cli launches MCP servers from ``~/.kiro/agents/*.json``.

    A mode change that only reaches ``~/.kiro/settings/mcp.json`` is invisible to
    the agent, because ``rebuild_agent_config`` merges the global file with
    ``setdefault`` and so preserves whatever the spec already declared.
    """

    EXT_ENTRY = {
        "command": "/opt/kirocrew",
        "args": ["mcp-playwright-proxy", "--extension"],
        "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
    }

    def _agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_decline_shared_agent_home", lambda **_: None)
        return agents

    STALE = {
        "command": "/opt/kirocrew",
        "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
    }

    def _write_spec(self, agents: Path, name: str, servers: dict) -> Path:
        p = agents / name
        p.write_text(json.dumps({"name": name.removesuffix(".json"), "mcpServers": servers}))
        return p

    def _servers(self, path: Path) -> dict:
        return json.loads(path.read_text())["mcpServers"]

    def test_replaces_stale_config_entry_with_new_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.STALE)})
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        assert self._servers(spec)["playwright-mcp"] == self.EXT_ENTRY

    def test_updates_legacy_key_in_place_preserving_refs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A legacy key is repointed, never renamed/removed, so an existing
        # ``@playwright-proxy-mcp`` reference in tools/allowedTools still resolves.
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-proxy-mcp": dict(self.STALE)})
        setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY)
        servers = self._servers(spec)
        assert servers["playwright-proxy-mcp"] == self.EXT_ENTRY
        assert "playwright-mcp" not in servers

    def test_leaves_user_authored_agent_file_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A hand-authored agent sharing the name prefix is NOT an owned file, so
        # its deliberately-distinct Playwright wiring must survive a mode change.
        agents = self._agents_dir(tmp_path, monkeypatch)
        custom = self._write_spec(
            agents, "kirocrew-custom.json", {"playwright-mcp": dict(self.STALE)}
        )
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert self._servers(custom)["playwright-mcp"] == self.STALE

    def test_leaves_user_authored_non_proxy_server_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        user_entry = {"command": "npx", "args": ["@playwright/mcp", "--headed"]}
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(user_entry)})
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert self._servers(spec)["playwright-mcp"] == user_entry

    def test_never_adds_playwright_to_an_agent_without_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew-lite.json", {"kirocrew-core": {"command": "x"}})
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert "playwright-mcp" not in self._servers(spec)

    def test_preserves_resolved_command_against_unresolved_bare_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # _kirocrew_bin() degrades to a bare "kirocrew" when PATH cannot resolve
        # it (a GUI launch inherits no shell profile). A mode toggle must not
        # trade the spec's working absolute path for a name that fails to spawn.
        agents = self._agents_dir(tmp_path, monkeypatch)
        existing = {
            "command": "/opt/venv/bin/kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
        }
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": existing})
        bare = {
            "command": "kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        assert setup_mod._sync_agent_specs_proxy_entry(bare) == 1
        got = self._servers(spec)["playwright-mcp"]
        assert got["command"] == "/opt/venv/bin/kirocrew"
        assert got["args"] == ["mcp-playwright-proxy", "--extension"]

    def test_fills_in_command_when_spec_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(
            agents,
            "kirocrew.json",
            {"playwright-mcp": {"args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"]}},
        )
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        assert self._servers(spec)["playwright-mcp"]["command"] == self.EXT_ENTRY["command"]

    def test_preserves_per_server_policy_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A mode change decides HOW the server launches. Sibling keys are policy
        # someone set deliberately — wiping disabledTools would silently
        # re-enable a tool that was turned off.
        agents = self._agents_dir(tmp_path, monkeypatch)
        existing = dict(self.STALE)
        existing.update(
            {
                "disabled": False,
                "disabledTools": ["browser_evaluate"],
                "autoApprove": ["browser_navigate"],
            }
        )
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": existing})
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        got = self._servers(spec)["playwright-mcp"]
        assert got["args"] == self.EXT_ENTRY["args"]
        assert got["disabledTools"] == ["browser_evaluate"]
        assert got["autoApprove"] == ["browser_navigate"]
        assert got["disabled"] is False

    def test_leaving_extension_mode_drops_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A stale secret must not linger once the mode no longer uses it, while an
        # unrelated env var the user set is left in place.
        agents = self._agents_dir(tmp_path, monkeypatch)
        existing = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok", "HTTP_PROXY": "http://p:8080"},
        }
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": existing})
        headless = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
        }
        assert setup_mod._sync_agent_specs_proxy_entry(headless) == 1
        env = self._servers(spec)["playwright-mcp"]["env"]
        assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" not in env
        assert env["HTTP_PROXY"] == "http://p:8080"

    def test_no_write_when_merge_is_a_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Already on the target mode: nothing to change, so no rewrite at all.
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.EXT_ENTRY)})
        before = spec.stat().st_mtime_ns
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert spec.stat().st_mtime_ns == before

    def test_syncs_the_cc_sidecar_on_a_default_data_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        cc = tmp_path / ".claude" / "agents"
        cc.mkdir(parents=True, exist_ok=True)
        sidecar = cc / "kirocrew.mcp.json"
        sidecar.write_text(json.dumps({"mcpServers": {"playwright-mcp": dict(self.STALE)}}))
        assert agents.exists()
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        assert self._servers(sidecar)["playwright-mcp"]["args"] == self.EXT_ENTRY["args"]

    @pytest.mark.parametrize("var", ["KIROCREW_HOME", "KIROCREW_POD"])
    def test_leaves_the_host_cc_sidecar_alone_from_an_isolated_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
    ):
        # ``Path.home()/.claude`` is host-absolute — no isolation variable moves
        # it — so an instance on its own data home must not stamp its config path
        # into the host's sidecar, which teardown would leave dangling.
        agents = self._agents_dir(tmp_path, monkeypatch)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv(var, str(tmp_path / "isolated"))
        cc = tmp_path / ".claude" / "agents"
        cc.mkdir(parents=True, exist_ok=True)
        sidecar = cc / "kirocrew.mcp.json"
        sidecar.write_text(json.dumps({"mcpServers": {"playwright-mcp": dict(self.STALE)}}))
        assert agents.exists()
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert self._servers(sidecar)["playwright-mcp"] == self.STALE

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_tightens_a_world_readable_secret_spec_even_when_content_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Another writer can land a token-bearing spec at the umask default. The
        # content then already matches, so the no-op path must still narrow the
        # file rather than leaving the secret readable by other local accounts.
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.EXT_ENTRY)})
        os.chmod(spec, 0o644)
        body_before = spec.read_text()
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert stat.S_IMODE(spec.stat().st_mode) == 0o600
        assert spec.read_text() == body_before

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_does_not_tighten_a_secret_free_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No secret on disk, so the owner's permission choice is left alone.
        agents = self._agents_dir(tmp_path, monkeypatch)
        headless = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
        }
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(headless)})
        os.chmod(spec, 0o644)
        assert setup_mod._sync_agent_specs_proxy_entry(headless) == 0
        assert stat.S_IMODE(spec.stat().st_mode) == 0o644

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_token_bearing_spec_is_tightened_not_inherited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The entry carries the extension token, so a spec sitting at the umask
        # default must NOT inherit 0644 — that would publish the token to every
        # local account.
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.STALE)})
        os.chmod(spec, 0o644)
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        assert stat.S_IMODE(spec.stat().st_mode) == 0o600

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX permission bits only")
    def test_headless_entry_preserves_existing_bits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No secret is introduced by a headless entry, so the file's own
        # permission decision is left alone.
        agents = self._agents_dir(tmp_path, monkeypatch)
        ext_spec = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": ext_spec})
        os.chmod(spec, 0o640)
        headless = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
        }
        assert setup_mod._sync_agent_specs_proxy_entry(headless) == 1
        assert stat.S_IMODE(spec.stat().st_mode) == 0o640

    def test_declines_from_worktree_or_isolated_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        spec = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.STALE)})
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_decline_shared_agent_home", lambda **_: spec)
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 0
        assert self._servers(spec)["playwright-mcp"] == self.STALE

    def test_malformed_spec_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / "kirocrew-lite.json").write_text("{not json")
        good = self._write_spec(agents, "kirocrew.json", {"playwright-mcp": dict(self.STALE)})
        assert setup_mod._sync_agent_specs_proxy_entry(self.EXT_ENTRY) == 1
        assert self._servers(good)["playwright-mcp"] == self.EXT_ENTRY


# ── TestHealBrowseModeDrift ──────────────────────────────────────────────────


class TestHealBrowseModeDrift:
    """Gateway boot must re-apply the recorded mode, not just collapse duplicates.

    The converge sweeps key off duplicate/legacy keys, so an install with exactly
    one canonical proxy carrying stale args stayed broken across restart while the
    dashboard showed the mode the user had already chosen.
    """

    def _wire(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "kiro_agents_dir", lambda: agents)
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "_decline_shared_agent_home", lambda **_: None)
        mcp = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "_kiro_mcp_json_path", lambda: mcp)
        # Extension mode recorded, with a token, so the mode resolves to --extension.
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: True)
        monkeypatch.setattr(setup_mod, "get_extension_token", lambda: "tok")
        monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "/opt/kirocrew")
        return mcp, agents

    def _spec_args(self, path: Path) -> list:
        return json.loads(path.read_text())["mcpServers"]["playwright-mcp"]["args"]

    def test_heals_stale_agent_spec_even_when_mcp_json_is_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The real-world broken shape: the global file already says --extension,
        # so anything keying off mcp.json alone concludes the install is healthy.
        mcp, agents = self._wire(tmp_path, monkeypatch)
        correct = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        mcp.write_text(json.dumps({"mcpServers": {"playwright-mcp": correct}}))
        spec = agents / "kirocrew.json"
        spec.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright-mcp": {
                            "command": "/opt/kirocrew",
                            "args": ["mcp-playwright-proxy", "--config", "/tmp/pw.json"],
                        }
                    }
                }
            )
        )
        setup_mod._heal_browse_mode_drift()
        assert self._spec_args(spec) == ["mcp-playwright-proxy", "--extension"]

    def test_no_write_when_already_consistent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Runs on every boot, so a healthy install must not be rewritten.
        mcp, agents = self._wire(tmp_path, monkeypatch)
        entry = {
            "command": "/opt/kirocrew",
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"},
        }
        mcp.write_text(json.dumps({"mcpServers": {"playwright-mcp": entry}}, indent=2))
        spec = agents / "kirocrew.json"
        spec.write_text(json.dumps({"mcpServers": {"playwright-mcp": dict(entry)}}))
        mcp_before = mcp.stat().st_mtime_ns
        spec_before = spec.stat().st_mtime_ns
        setup_mod._heal_browse_mode_drift()
        assert mcp.stat().st_mtime_ns == mcp_before
        assert spec.stat().st_mtime_ns == spec_before

    def test_never_adds_playwright_to_a_spec_without_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Runs unattended on every boot, so the "never add" invariant matters more
        # here than on an explicit user action.
        mcp, agents = self._wire(tmp_path, monkeypatch)
        spec = agents / "kirocrew.json"
        spec.write_text(json.dumps({"mcpServers": {"kirocrew-core": {"command": "x"}}}))
        setup_mod._heal_browse_mode_drift()
        assert "playwright-mcp" not in json.loads(spec.read_text())["mcpServers"]
        assert not mcp.exists()

    def test_boot_migration_invokes_the_heal(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        for name in (
            "_migrate_owned_kiro_registration",
            "_converge_kirocrew_mcp_json",
            "_converge_playwright_agent_files",
            "_heal_browse_mode_drift",
        ):
            monkeypatch.setattr(setup_mod, name, lambda n=name: calls.append(n))
        setup_mod.migrate_owned_playwright_registration()
        assert calls[-1] == "_heal_browse_mode_drift"
