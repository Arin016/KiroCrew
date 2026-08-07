"""Playwright MCP browser setup (OSS stub).

The upstream build wired browser setup to a managed package installer and an
enterprise-SSO cookie/storage-state flow. In the open-source build those steps
are neutralized: every public symbol is preserved so importing modules keep
working, but SSO setup is a no-op and reports "not available in OSS".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import shutil
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.agent_files import OWNED_CC_AGENT_FILES, OWNED_KIRO_AGENT_FILES
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser.auth import parse_netscape_cookies
from kiro_crew.config.paths import config_dir, kiro_agents_dir
from kiro_crew.mcp_playwright_proxy import _resolve_playwright_cmd
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

# Optional test/override hook. Left ``None`` at import — NOT a
# ``config_dir()`` capture — so importing this module never triggers the
# one-time data-home migration as an import side effect (the migration must fire
# only at ``ensure_data_home()`` in the CLI prologue). Internal code resolves the
# cookie path through ``_cookie_path()``; tests that need a fixed path set this
# attribute (``monkeypatch.setattr(setup, "SSO_COOKIE_PATH", tmp)``).
SSO_COOKIE_PATH: "Path | None" = None


def _cookie_path() -> Path:
    """Resolve the cookie-jar path, honoring a test-set ``SSO_COOKIE_PATH``."""
    if SSO_COOKIE_PATH is not None:
        return SSO_COOKIE_PATH
    from kiro_crew.browser import auth as _auth

    return _auth.cookie_path()


# The public npm package name for the Playwright MCP server. Used only as the
# input to ``mcp_server_alias`` to derive the canonical slash-free key
# (``playwright-mcp``) KiroCrew registers the proxy under.
_PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp"

# Key names KiroCrew (or the predecessor install it descends from) historically
# registered the Playwright PROXY under. When KiroCrew (re)writes its own
# registration it converges these to the canonical ``mcp_server_alias`` form.
#
# IMPORTANT — a key name in this set is NOT proof of KiroCrew authorship. A user
# may hand-declare a *direct* (non-proxy) Playwright server under the public
# package name ``@playwright/mcp``. Authorship is decided ONLY by the resolved
# launch target (:func:`_spec_is_proxy` — the entry invokes
# ``mcp-playwright-proxy``); every drop/converge site gates on that, so a
# superseded key whose spec is a direct server is left untouched. This tuple is
# only the set of *candidate* names to inspect, never a standalone authorship
# signal — do not add a name here expecting it to be dropped by name alone.
# ``npm:@playwright/mcp`` is the legacy on-disk key earlier installs wrote; it is
# data to clean up FROM, not a key this module ever emits (both it and
# ``@playwright/mcp`` alias to the same canonical ``playwright-mcp``).
_SUPERSEDED_PLAYWRIGHT_KEYS = (
    "@playwright/mcp",
    "npm:@playwright/mcp",
    "playwright-proxy-mcp",
)

# The on-disk key earlier KiroCrew installs wrote for a DIRECT npm-launched
# Playwright server (before the compression proxy existed). Unlike the bare
# ``@playwright/mcp`` key — which a user may legitimately hand-author for their
# own direct server — the ``npm:`` prefix is a KiroCrew-generated artifact, so a
# *direct* spec under THIS specific key is KiroCrew's legacy entry and is safe to
# upgrade to the proxy and remove. (A proxy spec under any superseded key is
# handled by _drop_superseded_playwright.)
_LEGACY_DIRECT_PLAYWRIGHT_KEY = "npm:@playwright/mcp"

# EXACT filenames KiroCrew generates under ``~/.kiro/agents/`` (kiro specs) and
# ``~/.claude/agents/`` (the CC MCP sidecar). The convergence sweep rewrites ONLY
# these — an explicit allowlist, never a ``kirocrew*`` prefix glob, because a
# user is free to hand-author e.g. ``~/.kiro/agents/kirocrew-custom.json`` and a
# filename prefix does not prove KiroCrew authorship; rewriting it on a restart
# would corrupt the user's own config. Single source of truth is the leaf module
# ``agent_files`` (imported by both ``agent.py``, which WRITES these files, and
# here) so adding a managed spec is a one-line change in one place — no drift.
_OWNED_KIRO_AGENT_FILES = OWNED_KIRO_AGENT_FILES
_OWNED_CC_AGENT_FILES = OWNED_CC_AGENT_FILES

# The env var carrying the Chrome-extension connection token. Named once so the
# writer that ADDS it and the merge that must DROP it on mode-down agree.
_EXTENSION_TOKEN_ENV = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"


def is_playwright_installed() -> bool:
    """Check whether the Playwright MCP package is resolvable on PATH (OSS stub).

    The managed package manager that originally backed this check is not
    available in the open-source build, so this returns False gracefully.
    """
    return False


def ensure_playwright_installed() -> None:
    """Browser setup is not available in the open-source build (no-op stub).

    The upstream flow installed Playwright MCP via a managed package manager and
    wired enterprise-SSO cookie injection. Neither is shipped in OSS, so this is
    a no-op rather than raising.
    """
    return None


def is_headed() -> bool:
    """Return True if browser should run in headed mode.

    Headed on macOS and Windows (a desktop user session is available and a
    visible Chromium window is preferred so users can complete interactive SSO
    prompts). Headless on Linux, where the gateway typically runs on a
    server without an accessible display.
    """
    return platform.system() in ("Darwin", "Windows")


def has_playwright_extension() -> bool:
    """Check if user has opted into Playwright Chrome extension mode.

    Extension mode attaches to the user's running Chrome (with all existing auth)
    instead of launching a separate headless browser, which reuses whatever
    session and extensions the real Chrome already has.
    """
    flag_file = config_dir() / "playwright-extension-mode"
    return flag_file.exists()


def get_extension_token() -> str | None:
    """Read the stored Playwright extension token."""
    token_file = config_dir() / "playwright-extension-token"
    if token_file.exists():
        return token_file.read_text().strip() or None
    return None


def generate_playwright_config() -> Path:
    """Generate ``<config_dir>/playwright-config.json`` with absolute paths.

    The open-source build ships a generic Chromium config with no
    enterprise auth-server allowlist.
    """
    config_path = config_dir() / "playwright-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    storage_state = config_dir() / "playwright-storage-state.json"

    # ``storageState`` is only wired in when the file is actually present.
    # Playwright treats a missing storage-state path as a hard error and fails
    # context creation, which surfaces to the agent as an opaque HTTP 400 on
    # EVERY browser call — indistinguishable from a broken install. Auth is
    # optional (public sites need none), so an absent cookie jar must degrade to
    # an anonymous context rather than disable browsing altogether.
    context_options: dict[str, Any] = {}
    if storage_state.exists():
        context_options["storageState"] = str(storage_state)

    config = {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {
                "channel": "chromium",
                # Run headless: the live mirror in the dashboard Browser panel is
                # the intended view surface, so a separate visible OS window is
                # redundant (and breaks on display-less Linux hosts). Auth is
                # seeded via ``storageState`` when present, so no interactive SSO
                # window is needed.
                "headless": True,
                "args": [],
            },
            "contextOptions": context_options,
        },
        "capabilities": ["network", "storage"],
    }

    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def refresh_storage_state() -> dict[str, Any]:
    """Refresh the Playwright storage state from the browser cookie file.

    Reads cookies via the (OSS-stubbed) browser auth layer and writes them to
    a Playwright-compatible storage-state file. Returns a not-available result
    when no cookie source exists, which is the default in the open-source build.
    """
    cookie_path = _cookie_path()
    if not cookie_path.exists():
        return {"ok": False, "error": "browser auth not available in OSS"}

    cookies = parse_netscape_cookies(cookie_path)
    if not cookies:
        return {"ok": False, "error": "no cookies parsed"}

    storage_state_path = config_dir() / "playwright-storage-state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(storage_state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    expired = [c for c in cookies if 0 < c.get("expires", -1) < time.time()]
    return {
        "ok": True,
        "path": str(storage_state_path),
        "count": len(cookies),
        "expired": len(expired),
    }


def _kirocrew_bin() -> str:
    """Resolve path to the kirocrew binary."""
    return shutil.which("kirocrew") or "kirocrew"


def _spec_is_proxy(spec: Any) -> bool:
    """True iff a server spec's resolved launch target is KiroCrew's proxy.

    The ONLY reliable authorship signal: the entry invokes
    ``mcp-playwright-proxy`` (which only KiroCrew registers). A key *name*
    matching a superseded key is NOT proof of authorship — a user may key a
    *direct* (non-proxy) Playwright server under the public package name
    ``@playwright/mcp``. That user entry must never be dropped or rewritten.
    """
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    if not isinstance(args, list):
        return False
    return "mcp-playwright-proxy" in args


def _drop_superseded_playwright(servers: dict[str, Any], canonical: str) -> None:
    """Drop KiroCrew's own superseded Playwright entries from a servers dict.

    Operates in place; never drops the ``canonical`` key. Removes:

    * any superseded key recorded in the ownership MANIFEST (the authoritative
      "KiroCrew wrote this" signal), or whose spec is actually the KiroCrew proxy
      (``_spec_is_proxy`` — the launch-target fallback for pre-manifest installs);
      and
    * KiroCrew's legacy DIRECT entry under ``_LEGACY_DIRECT_PLAYWRIGHT_KEY``
      (``npm:@playwright/mcp``) even when it is a *direct* (non-proxy) spec —
      that ``npm:``-prefixed key is a KiroCrew install artifact, so once we write
      the canonical proxy the old direct entry is superseded and must be removed
      (otherwise it lingers as a second Playwright backend).

    A user-declared *direct* server under the BARE ``@playwright/mcp`` key (which
    a user may legitimately hand-author) is left untouched — it is neither in the
    manifest, nor a proxy spec, nor the ``npm:``-prefixed legacy key. Used when
    KiroCrew rewrites its own registration so the canonical (slash-free alias)
    entry is the only KiroCrew-authored one left behind.
    """
    owned = _load_owned_mcp_keys()
    for key in _SUPERSEDED_PLAYWRIGHT_KEYS:
        if key == canonical or key not in servers:
            continue
        if key in owned or _spec_is_proxy(servers[key]) or key == _LEGACY_DIRECT_PLAYWRIGHT_KEY:
            del servers[key]


def migrate_owned_playwright_registration() -> None:
    """Converge KiroCrew's own Playwright registration to one canonical server.

    Runs on gateway init. The Playwright proxy must be registered under the
    slash-free ``mcp_server_alias`` form (so kiro-cli can ``@``-reference it and
    the gateway does not derive a second pooled backend). Two KiroCrew-owned
    surfaces are converged, keyed by *resolved launch target* (an entry that
    invokes ``mcp-playwright-proxy``) plus KiroCrew's superseded keys:

    1. kiro's ``~/.kiro/settings/mcp.json`` — the browse entry KiroCrew
       co-manages — is rewritten to the canonical proxy entry. This also upgrades
       KiroCrew's legacy DIRECT ``npm:@playwright/mcp`` entry (written by installs
       that predate the compression proxy) to the proxy, preserving the original
       boot migration's direct-to-proxy behavior.
    2. KiroCrew's own ``~/.kiro/crew/mcp.json`` — the agent-specific MCP override
       merged into the agent config on every rebuild — is converged at the
       SOURCE, so a stale ``playwright-proxy-mcp`` key there is healed once rather
       than re-injected on every rebuild for the per-rebuild
       :func:`converge_playwright_servers` backstop to undo indefinitely.
    3. The KiroCrew-generated agent configs (the exact filenames in
       ``_OWNED_KIRO_AGENT_FILES`` / ``_OWNED_CC_AGENT_FILES``) are swept so any
       duplicate proxy entry (e.g. a legacy ``playwright-proxy-mcp``) collapses
       into the single canonical ``playwright-mcp``. This self-heals an existing
       machine on a plain gateway restart, without waiting for a full agent
       rebuild. Only the exact files KiroCrew writes are touched — a user's own
       custom agents in the same dirs are never rewritten.

    Never adds Playwright where none exists, never rewrites a user-declared
    server, and never mutates the user-owned discovery sources
    (``~/.claude.json``) — those converge for *display* on read (discovery
    canonicalization) and at launch (pool dedupe), not by mutating files.
    """
    _migrate_owned_kiro_registration()
    _converge_kirocrew_mcp_json()
    _converge_playwright_agent_files()
    _heal_browse_mode_drift()


def _heal_browse_mode_drift() -> None:
    """Re-apply the recorded browse mode to the owned agent specs on boot.

    The converge steps above collapse DUPLICATE and legacy proxy keys. None of
    them notices a single canonical entry whose ``args`` are stale for the mode
    the user actually chose, so an install wired before the mode write-path was
    fixed keeps launching the wrong browser across upgrade and restart — and the
    dashboard already shows the mode they picked, so nothing prompts them to
    re-toggle it.

    Scoped to the agent specs on purpose: kiro's global ``mcp.json`` is already
    converged by :func:`_migrate_owned_kiro_registration`, and the specs are the
    files kiro-cli actually launches from — the drifted shape in practice is a
    correct ``mcp.json`` beside a stale spec. Delegating to
    :func:`_sync_agent_specs_proxy_entry` inherits its invariants unchanged: only
    owned files, only entries whose launch target is already this proxy (so
    Playwright is never ADDED and a user's own server is never rewritten),
    change-detected so a healthy install performs no write, and declined outright
    from a worktree or isolated home.
    """
    _sync_agent_specs_proxy_entry(_proxy_entry_for_mode())


def _migrate_owned_kiro_registration() -> None:
    """Rewrite KiroCrew's browse entry in kiro's ``mcp.json`` to the canonical key."""
    mcp_json = _kiro_mcp_json_path()
    # Fast-path bail BEFORE taking the lock: this migration never adds Playwright
    # where none exists, and _kiro_mcp_locked would otherwise create the settings
    # dir + lock sidecar on an install that has no kiro config at all.
    if not mcp_json.is_file():
        return
    # The read + decide + write must be ONE critical section: deciding from an
    # unlocked read and then writing lets a concurrent bridge/dashboard update
    # land in between, so the write is computed from a stale snapshot and drops
    # the other writer's entries.
    with _kiro_mcp_locked():
        if not mcp_json.is_file():
            return
        try:
            data = json.loads(mcp_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return
        canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
        canon_entry = servers.get(canonical)
        canon_is_proxy = _spec_is_proxy(canon_entry)
        # There are two KiroCrew-owned things worth migrating to the canonical
        # proxy:
        #   (a) a superseded PROXY entry (a duplicate proxy under a legacy key),
        #       and
        #   (b) KiroCrew's legacy DIRECT ``npm:@playwright/mcp`` entry — the key
        #       earlier KiroCrew installs wrote for a direct npm-launched
        #       Playwright (before the compression proxy existed). Upgrading it to
        #       the proxy is the ORIGINAL purpose of this boot migration; dropping
        #       it would leave existing users on the direct server with no
        #       compression.
        # A user-declared *direct* server under the BARE ``@playwright/mcp`` key
        # is NOT KiroCrew's (authorship is by launch target, not key name) and is
        # left untouched — only the ``npm:``-prefixed key is a KiroCrew legacy
        # artifact.
        superseded_proxy_present = any(
            key != canonical and _spec_is_proxy(servers.get(key))
            for key in _SUPERSEDED_PLAYWRIGHT_KEYS
        )
        legacy_direct = servers.get(_LEGACY_DIRECT_PLAYWRIGHT_KEY)
        legacy_direct_present = isinstance(legacy_direct, dict) and not _spec_is_proxy(
            legacy_direct
        )
        # Leave the file untouched unless there is a KiroCrew-owned entry to
        # migrate, AND the canonical slot is either empty or already our proxy
        # (safe to (re)write). If the canonical key holds a user-declared *direct*
        # (non-proxy) server, migrating would write servers[canonical] =
        # proxy_entry and clobber that user config on every boot — so skip.
        if not (superseded_proxy_present or legacy_direct_present):
            return
        if canon_entry is not None and not canon_is_proxy:
            return
        _patch_mcp_for_mode_unlocked()


def _converge_kirocrew_mcp_json() -> None:
    """Converge Playwright proxies in KiroCrew's own ``<data-home>/mcp.json``.

    ``rebuild_agent_config`` merges every server from this file into the agent
    config, so a stale duplicate proxy key here (e.g. a legacy
    ``playwright-proxy-mcp``) would be re-injected on EVERY rebuild — forcing the
    per-rebuild :func:`converge_playwright_servers` backstop to undo it forever.
    Healing it at the SOURCE here (this file is unambiguously KiroCrew-owned,
    unlike the deliberately-excluded user discovery source ``~/.claude.json``)
    makes the rebuild-time pass a true backstop rather than the primary cure.
    Mode-preserving atomic write; silently skips an unreadable/non-dict/absent
    file and a no-op convergence.
    """
    path = config_dir() / "mcp.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if not converge_playwright_servers(data):
        return
    try:
        prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        prev_mode = None
    try:
        atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
    except OSError:
        pass


def _entry_is_playwright_proxy(name: str, spec: Any, canonical: str) -> bool:
    """True iff a server entry is KiroCrew's Playwright proxy.

    Authorship is proven ONLY by the *resolved launch target* — the entry
    invokes ``mcp-playwright-proxy`` (:func:`_spec_is_proxy`). The key name is
    NOT a proof of authorship: a user may hand-declare a *direct* Playwright
    server under the public package name ``@playwright/mcp`` (a superseded key),
    and that entry must never be collapsed or dropped. The single exception is
    the canonical key when it already holds the proxy — but that is covered by
    the launch-target check too, so name matching is unnecessary.
    """
    return _spec_is_proxy(spec)


def _redact_spec_for_log(spec: Any) -> Any:
    """Return a shallow copy of *spec* safe to log: ``env`` VALUES masked, keys
    kept. Leaves ``command``/``args`` intact (an ``--extension``/``--config``
    wiring is diagnostic, not secret) so a dropped entry can be reconstructed
    from the log without exposing a token like ``PLAYWRIGHT_MCP_EXTENSION_TOKEN``.
    """
    if not isinstance(spec, dict):
        return spec
    safe = dict(spec)
    env = safe.get("env")
    if isinstance(env, dict):
        safe["env"] = {k: "***" for k in env}
    return safe


def converge_playwright_servers(config: dict) -> bool:
    """Collapse every KiroCrew Playwright-proxy entry in ``config`` to the single
    canonical ``playwright-mcp`` server. Mutates ``config`` in place; returns
    ``True`` iff anything changed.

    Convergence is keyed by resolved launch target (:func:`_spec_is_proxy` — the
    entry invokes ``mcp-playwright-proxy``), so two entries that launch the same
    proxy under different names (e.g. ``playwright-mcp`` and the legacy
    ``playwright-proxy-mcp``) become one. The survivor keeps the canonical key;
    when no canonical entry exists the most completely-wired proxy entry is
    renamed to it (never dropped). ``@<dropped>`` references in
    ``tools``/``allowedTools`` are rewritten to ``@playwright-mcp`` and
    de-duplicated. Never adds Playwright where none exists, and never touches a
    server whose spec is not the proxy — including a user-declared *direct*
    ``@playwright/mcp`` entry (identity is by launch target, not key name).
    Every collapse/rename is logged so a disappearing entry is diagnosable.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    proxy_names = [n for n, s in servers.items() if _entry_is_playwright_proxy(n, s, canonical)]
    # Nothing to converge: no proxy entry, or exactly the single canonical one.
    if not proxy_names or proxy_names == [canonical]:
        return False

    # The user's recorded browse mode is the authority for which wiring should
    # win when two proxies are both configured: an ``--extension`` entry and a
    # stale ``--config`` headless entry both score as "wired", but arg-count
    # alone would let the headless one (more args) silently replace the active
    # extension entry and disable extension-mode browsing. Rank an entry matching
    # the current mode first, THEN by generic wired-ness, THEN by arg count.
    want_extension = has_playwright_extension()

    def _completeness(name: str) -> tuple[int, int, int]:
        spec = servers.get(name)
        args = (spec.get("args") or []) if isinstance(spec, dict) else []
        has_ext = "--extension" in args
        has_cfg = "--config" in args
        mode_match = 1 if (has_ext if want_extension else has_cfg) else 0
        wired = 1 if (has_ext or has_cfg) else 0
        return (mode_match, wired, len(args))

    # The SURVIVOR SPEC is the proxy that best matches the user's current mode
    # (falling back to the most completely-wired one) regardless of which key
    # currently owns it — so convergence never discards the active configuration
    # in favor of a stale or bare duplicate.
    survivor_spec = servers[max(proxy_names, key=_completeness)]

    # Pick the SURVIVOR KEY. The canonical key is used only when it is free or
    # already holds KiroCrew's proxy — never when it holds a user-declared
    # *direct* (non-proxy) server, or that user config would be clobbered.
    #   * canonical free / already-proxy  -> survivor lives at ``canonical``;
    #   * canonical occupied by a non-proxy user server -> survivor stays under
    #     the most-complete legacy proxy key so the user's canonical entry is
    #     untouched and we only collapse *duplicate* proxies onto it.
    canon_spec = servers.get(canonical)
    if canonical not in servers or _spec_is_proxy(canon_spec):
        target = canonical
    else:
        target = max(
            (n for n in proxy_names if n != canonical),
            key=_completeness,
        )

    dropped = [n for n in proxy_names if n != target]
    if not dropped:
        # Survivor already sits alone under ``target`` (a lone legacy proxy while
        # a user's direct server holds canonical) — nothing to collapse. Leaving
        # it in place is correct: never delete the last proxy, never move it onto
        # the user's canonical entry.
        return False
    # Log each dropped spec IN FULL (env VALUES redacted, keys kept) BEFORE
    # deleting it. Convergence is a destructive, unattended, every-restart path
    # whose survivor ranking depends on a live ``has_playwright_extension()``
    # probe; if that were ever transiently wrong it could drop a still-wanted
    # entry's args/env. Logging the whole spec (not just the key name) leaves a
    # forensic trail to reconstruct a wrongly-deleted entry — without ever
    # writing a token value to the log.
    dropped_specs = {n: _redact_spec_for_log(servers.get(n)) for n in dropped}
    for n in dropped:
        servers.pop(n, None)
    servers[target] = survivor_spec
    logger.info(
        "Converged Playwright proxy entries %s onto %r; dropped specs (env " "values redacted): %s",
        proxy_names,
        target,
        dropped_specs,
    )

    new_ref = f"@{target}"
    drop_refs = {f"@{n}" for n in dropped}
    for key in ("tools", "allowedTools"):
        lst = config.get(key)
        if isinstance(lst, list):
            config[key] = list(dict.fromkeys(new_ref if t in drop_refs else t for t in lst))
    return True


def _converge_playwright_agent_files() -> None:
    """Sweep KiroCrew-generated agent configs, converging Playwright to one
    canonical server. Runs on gateway init so an existing machine self-heals on
    a plain restart. Only KiroCrew-OWNED agent-config files are touched — the
    EXACT filenames in ``_OWNED_KIRO_AGENT_FILES`` under ``~/.kiro/agents/`` and
    ``_OWNED_CC_AGENT_FILES`` under ``~/.claude/agents/``, an explicit allowlist
    (not a ``kirocrew*`` prefix glob). A user's OWN agents — even one they name
    ``kirocrew-custom.json`` — live in the same dirs and may carry intentionally
    distinct Playwright entries; matching exact generated filenames is what keeps
    a restart from rewriting configs KiroCrew did not author. Silently skips
    unreadable/non-dict/absent files.
    """
    agent_files: list[Path] = []
    kiro_dir = kiro_agents_dir()
    for name in _OWNED_KIRO_AGENT_FILES:
        p = kiro_dir / name
        if p.is_file():
            agent_files.append(p)
    cc_dir = Path.home() / ".claude" / "agents"
    for name in _OWNED_CC_AGENT_FILES:
        p = cc_dir / name
        if p.is_file():
            agent_files.append(p)
    for path in agent_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if converge_playwright_servers(data):
            try:
                # Governance floor: this rewrites allowedTools while converging
                # Playwright refs, so run the whole map through the shared filter
                # before persisting — a ceiling-governed grant/autoApprove must not
                # survive a convergence sweep of a KiroCrew-owned agent config.
                # No-op on an ungoverned host.
                try:
                    from kiro_crew.platform.governance import (
                        sanitize_agent_config_governance,
                    )

                    sanitize_agent_config_governance(data)
                except Exception:  # noqa: BLE001 — never break convergence on this
                    logger.debug("governance sanitize unavailable during converge", exc_info=True)
                # Preserve the file's existing permission bits: an agent config
                # may hold MCP ``env`` credentials and be mode 0600 — atomic_write
                # would otherwise recreate it with the umask default (commonly
                # 0644), exposing secrets to other local users after startup.
                try:
                    prev_mode: int | None = stat.S_IMODE(path.stat().st_mode)
                except OSError:
                    prev_mode = None
                # Atomic write: a live kiro-cli session reads kirocrew.json
                # through the agent-config path, so a torn write (truncated
                # mid-flush) could be parsed as a corrupt config. Rename-based
                # replace makes the swap all-or-nothing.
                atomic_write(path, json.dumps(data, indent=2), mode=prev_mode)
            except OSError:
                pass


# Sidecar manifest recording the MCP server keys KiroCrew itself has written.
# kiro-cli validates ~/.kiro/settings/mcp.json (and agent specs) with
# ``deny_unknown_fields``, so an in-spec ownership sentinel is impossible; the
# manifest lives OUT of band under the KiroCrew data home (a dir KiroCrew owns
# outright). It is the FIRST authorship signal for drop/converge decisions —
# the ``mcp-playwright-proxy`` launch-target heuristic remains the fallback
# for entries written by installs that predate this manifest.
_OWNED_MCP_KEYS_MANIFEST = "owned-mcp-keys.json"


def _owned_mcp_keys_path() -> Path:
    return config_dir() / _OWNED_MCP_KEYS_MANIFEST


def _load_owned_mcp_keys() -> set[str]:
    """Return the set of MCP keys KiroCrew has recorded writing (empty on any
    read/parse error — a missing/corrupt manifest just means fall back to the
    launch-target heuristic, never a crash)."""
    path = _owned_mcp_keys_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return set()
    keys = data.get("keys") if isinstance(data, dict) else None
    return {k for k in keys if isinstance(k, str)} if isinstance(keys, list) else set()


def _record_owned_mcp_key(key: str) -> None:
    """Record *key* as KiroCrew-written in the sidecar manifest (mode 0600).

    Idempotent and best-effort: a failure to persist the marker must never break
    the MCP write it accompanies (the launch-target heuristic still covers the
    entry), so all errors are swallowed.
    """
    try:
        current = _load_owned_mcp_keys()
        if key in current:
            return
        current.add(key)
        path = _owned_mcp_keys_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"keys": sorted(current)}, indent=2), mode=0o600)
    except OSError:
        pass


def _kiro_mcp_json_path() -> Path:
    """Path to kiro's global MCP config — the file KiroCrew co-manages."""
    return Path.home() / ".kiro" / "settings" / "mcp.json"


@contextlib.contextmanager
def _kiro_mcp_locked() -> Iterator[None]:
    """Hold the exclusive advisory lock guarding kiro's global ``mcp.json``.

    Every writer of that file must serialize on the shared ``mcp.lock`` sidecar:
    the dashboard MCP handler (``handlers/mcp.py`` ``_McpFileLock``) and the app
    bridges (``apps/bridges.py``) already do. Writers coordinate ONLY if they all
    take this lock — a lock-free read-modify-write races the others and drops
    whichever side wrote first, losing that writer's server entries (an app's MCP
    server silently disappearing, or the browse entry vanishing).

    Blocking: callers on the event loop must dispatch through
    ``asyncio.to_thread``. Not reentrant — code already inside this block must
    call the ``_unlocked`` write helpers, never the public ``patch_mcp_*``
    wrappers (a second exclusive acquire on a fresh fd deadlocks the process
    against itself).
    """
    mcp_json = _kiro_mcp_json_path()
    mcp_json.parent.mkdir(parents=True, exist_ok=True)
    lock_path = mcp_json.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    # "r+" (not "r"): Windows msvcrt.locking requires a writable fd, and
    # platform_compat swallows the EACCES an "r" fd would raise — which would
    # silently degrade this to a no-op.
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=True):
            yield


def _patch_mcp_extension_unlocked(token: str) -> None:
    """Write the ``--extension`` proxy entry. Caller MUST hold ``_kiro_mcp_locked``."""
    _write_proxy_entry_unlocked(
        {
            "command": _kirocrew_bin(),
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {_EXTENSION_TOKEN_ENV: token},
        }
    )


def _patch_mcp_headless_unlocked() -> None:
    """Write the headless-config proxy entry. Caller MUST hold ``_kiro_mcp_locked``."""
    _write_proxy_entry_unlocked(
        {
            "command": _kirocrew_bin(),
            "args": [
                "mcp-playwright-proxy",
                "--config",
                str(config_dir() / "playwright-config.json"),
            ],
        }
    )


def _write_proxy_entry_unlocked(entry: dict[str, Any]) -> None:
    """Publish *entry* as the product's Playwright server everywhere it is read.

    Two files matter, and writing only the first is a silent no-op:

    * ``~/.kiro/settings/mcp.json`` — kiro's global registry.
    * ``~/.kiro/agents/*.json`` — what kiro-cli ACTUALLY launches MCP servers
      from. ``rebuild_agent_config`` merges the global file with ``setdefault``,
      so a spec that already declares a Playwright server keeps its OLD entry
      forever. Updating the global file alone therefore leaves the browse mode
      the agent really gets unchanged.

    Both receive the SAME spec, so the launch shape cannot drift between them.

    Caller MUST hold ``_kiro_mcp_locked``.
    """
    mcp_json = _kiro_mcp_json_path()
    if not mcp_json.exists():
        return
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        # A user-owned mcp.json may hold valid JSON that isn't an object (e.g.
        # `[]`/`null`/a string after truncation or a hand-edit), or an
        # mcpServers that isn't a dict. data.setdefault / servers[...] would
        # then raise AttributeError/TypeError, which the except below does NOT
        # catch. Reset a bad shape to {} — matches _migrate_playwright_to_proxy.
        if not isinstance(data, dict):
            data = {}
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = data["mcpServers"] = {}
        _drop_superseded_playwright(servers, canonical)
        servers[canonical] = entry
        mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # 0600 unconditionally: the entry carries the extension token in
        # extension mode, and the file holds other servers' credentials in both.
        platform_compat.chmod_safe(str(mcp_json), 0o600)
        _record_owned_mcp_key(canonical)
    except (json.JSONDecodeError, OSError):
        return
    _sync_agent_specs_proxy_entry(entry)


def _proxy_entry_for_mode() -> dict[str, Any]:
    """Return the proxy spec for the recorded browse mode.

    Extension mode needs a token to be usable, so a flagged-but-tokenless
    install resolves to the headless config rather than an entry whose
    ``PLAYWRIGHT_MCP_EXTENSION_TOKEN`` would be empty. Single source of the mode
    decision, shared by the mcp.json patch path and the boot-time heal, so the
    two cannot disagree about which mode is in effect.
    """
    if has_playwright_extension():
        token = get_extension_token() or ""
        if token:
            return {
                "command": _kirocrew_bin(),
                "args": ["mcp-playwright-proxy", "--extension"],
                "env": {_EXTENSION_TOKEN_ENV: token},
            }
    return {
        "command": _kirocrew_bin(),
        "args": [
            "mcp-playwright-proxy",
            "--config",
            str(config_dir() / "playwright-config.json"),
        ],
    }


def _merge_proxy_launch_fields(existing: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Return *existing* with only the MODE-dependent fields taken from *entry*.

    A browse-mode change decides how the server is launched — nothing else. Every
    sibling key is policy the user or the dashboard set (``disabled``,
    ``disabledTools``, ``autoApprove``, tool filters), so replacing the whole
    object would silently re-enable a tool someone deliberately turned off.

    ``command`` is deliberately NOT synced. ``_kirocrew_bin`` degrades to a bare
    ``"kirocrew"`` when PATH cannot resolve it — which a GUI/desktop launch that
    inherits no shell profile routinely cannot — so copying it over would replace
    a working absolute interpreter path with a name that fails at the next spawn.
    The spec's resolved command is refreshed by ``rebuild_agent_config``, whose
    job that is; here it is only filled in when the entry has none at all.

    ``env`` is merged key-wise rather than swapped: the entry contributes only
    ``PLAYWRIGHT_MCP_EXTENSION_TOKEN``, and any other variable on the server
    belongs to whoever put it there. Leaving extension mode drops the token key
    specifically — a stale secret must not linger in the spec — and removes an
    ``env`` map that this leaves empty.
    """
    merged = dict(existing)
    if not merged.get("command"):
        merged["command"] = entry["command"]
    merged["args"] = list(entry["args"])

    env_in = entry.get("env")
    env_out = dict(merged["env"]) if isinstance(merged.get("env"), dict) else {}
    if isinstance(env_in, dict) and env_in:
        env_out.update(env_in)
    else:
        env_out.pop(_EXTENSION_TOKEN_ENV, None)
    if env_out:
        merged["env"] = env_out
    else:
        merged.pop("env", None)
    return merged


def _tighten_secret_bearing_spec(path: Path, servers: dict[str, Any]) -> None:
    """chmod *path* to 0600 when one of its proxy entries carries the secret.

    Judged from what is ON DISK, not from the entry being applied: the exposure
    is a property of the file's current contents. Only narrows, and only when
    group or other actually hold a bit, so a spec already at 0600 is untouched
    and a secret-free headless spec keeps whatever permissions its owner chose.
    """
    if not any(
        _spec_is_proxy(s) and isinstance(s, dict) and (s.get("env") or {}).get(_EXTENSION_TOKEN_ENV)
        for s in servers.values()
    ):
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            platform_compat.chmod_safe(str(path), 0o600)
    except OSError:
        return


def _sync_agent_specs_proxy_entry(entry: dict[str, Any]) -> int:
    """Repoint every Kiro Crew-owned agent spec declaring the proxy at *entry*.

    Scoped to the EXACT filenames in ``_OWNED_KIRO_AGENT_FILES`` /
    ``_OWNED_CC_AGENT_FILES`` — an allowlist, not a glob. A user's own agents
    live in the same directories (even one named ``kirocrew-custom.json``) and
    may carry intentionally distinct Playwright args or env, so matching
    generated filenames is what keeps a mode change from rewriting a spec this
    product did not author. Mirrors ``_converge_playwright_agent_files``.

    Within those files, updates each owning key IN PLACE — no renames, no
    deletions — so existing ``@<server>`` references in ``tools`` /
    ``allowedTools`` keep resolving. Only entries whose launch target is the
    proxy are touched, so a hand-authored Playwright server under a canonical
    key is left alone, and Playwright is never ADDED to an agent that declared
    none.

    Returns the number of spec files updated.
    """
    # Function-local import: ``agent`` imports this module's package, so a
    # top-level import here is a circular import. Only the shared-agent-home
    # guard is needed, and only at call time.
    from kiro_crew.agent import _decline_shared_agent_home

    # An instance booted from a git worktree or its own isolated home is
    # throwaway, but these specs are shared with the real install — repointing
    # them at this tree would make the live gateway launch code that is about to
    # disappear. Same guard, same reason, as ``rebuild_agent_config``'s.
    if _decline_shared_agent_home() is not None:
        return 0

    spec_paths: list[Path] = []
    kiro_dir = kiro_agents_dir()
    for name in _OWNED_KIRO_AGENT_FILES:
        p = kiro_dir / name
        if p.is_file():
            spec_paths.append(p)
    cc_dir = Path.home() / ".claude" / "agents"
    # The Claude sidecar lives at a HOST-absolute path that no isolation variable
    # relocates: KIROCREW_HOME moves the data home and KIRO_HOME moves the kiro
    # agents dir, but ``Path.home()/.claude`` is the real user's either way. An
    # instance running on its own data home would therefore stamp ITS config path
    # into the host's sidecar, and a pod's teardown then leaves the host launching
    # a browser against a deleted file. The shared-agent-home guard above cannot
    # catch this — it reasons about ``kiro_agents_dir()``, a different directory —
    # so skip the sidecar whenever the data home is not the ambient default.
    if not (os.environ.get("KIROCREW_HOME") or os.environ.get("KIROCREW_POD")):
        for name in _OWNED_CC_AGENT_FILES:
            p = cc_dir / name
            if p.is_file():
                spec_paths.append(p)

    updated = 0
    for path in spec_paths:
        # Serialize on the SAME sidecar the app bridges use for this file. The
        # read-modify-write below is otherwise racy against app registration:
        # whichever side writes second wins wholesale, so an app's MCP server
        # entry can silently disappear (see ``_kiro_mcp_locked`` for the same
        # hazard on kiro's global mcp.json). The spec is re-read INSIDE the lock
        # so a concurrent registration is never overwritten with a stale copy.
        #
        # Function-local import: ``apps.bridges`` pulls in the app runtime, so a
        # top-level import here is a circular import.
        from kiro_crew.apps.bridges import _mcp_lock

        with _mcp_lock(target=path):
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if not isinstance(spec, dict):
                continue
            servers = spec.get("mcpServers")
            if not isinstance(servers, dict):
                continue
            changed = False
            for name, existing in list(servers.items()):
                if not _spec_is_proxy(existing):
                    continue
                merged = _merge_proxy_launch_fields(existing, entry)
                if merged != existing:
                    servers[name] = merged
                    changed = True
            if not changed:
                # Content already matches, but the FILE can still be group- or
                # world-readable while holding the connection secret: another
                # writer landing at the umask default creates exactly that state,
                # and returning early here would leave it that way — now on every
                # boot, since the heal reaches this path unattended. Tighten in
                # place; no content rewrite, and a no-op for an already-0600 spec.
                _tighten_secret_bearing_spec(path, servers)
                continue
            if entry.get("env"):
                # This entry carries the extension token. A spec left at the
                # umask default (commonly 0644) would publish that secret to
                # every local account, so tighten rather than inherit — the
                # token is new to the file, and 0600 only ever narrows access.
                mode: int | None = 0o600
            else:
                # No secret introduced: preserve whatever the file already had
                # rather than imposing a permission decision (mirrors
                # ``_converge_playwright_agent_files``).
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                except OSError:
                    mode = None
            try:
                # Atomic write: a live kiro-cli session reads these specs to
                # spawn MCP servers, so a torn write could be parsed as a
                # corrupt config and take the ACP process down.
                atomic_write(path, json.dumps(spec, indent=2), mode=mode)
            except OSError:
                continue
            updated += 1
    return updated


def _patch_mcp_for_mode_unlocked() -> None:
    """Write the proxy entry matching the configured browse mode.

    Delegates the mode decision to :func:`_proxy_entry_for_mode` so this path and
    the boot-time heal cannot disagree about which mode is in effect.

    Caller MUST hold ``_kiro_mcp_locked``.
    """
    _write_proxy_entry_unlocked(_proxy_entry_for_mode())


def patch_mcp_extension(token: str) -> None:
    """Update MCP config to use proxy with --extension and token env var.

    Takes the shared mcp.json lock. Blocking — do not call on the event loop.
    """
    with _kiro_mcp_locked():
        _patch_mcp_extension_unlocked(token)


def patch_mcp_headless() -> None:
    """Update MCP config to use proxy with headless mode config.

    Takes the shared mcp.json lock. Blocking — do not call on the event loop.
    """
    with _kiro_mcp_locked():
        _patch_mcp_headless_unlocked()


def check_playwright_launchable() -> tuple[bool, str]:
    """Best-effort check that a Playwright MCP launcher is resolvable.

    Reuses the proxy's own resolution order (``KIROCREW_PLAYWRIGHT_CMD`` →
    a ``mcp-server-playwright``/``playwright-mcp`` binary → ``npx``), so the
    check agrees with what the proxy would actually spawn. Returns
    ``(ok, detail)`` where ``detail`` is the resolved launcher, or an install
    hint when nothing is resolvable (e.g. Node/npm absent).
    """
    cmd = _resolve_playwright_cmd()
    if cmd is None:
        return (
            False,
            "not found — install Node.js then `npm i -g @playwright/mcp` "
            "(or ensure `npx` is on PATH)",
        )
    return True, cmd


def register_playwright_proxy() -> tuple[Path, str]:
    """Register KiroCrew's Playwright proxy in kiro's ``mcp.json``.

    Unlike the boot-time converge helpers, this is the explicit ``browse setup``
    entry point: it CREATES ``~/.kiro/settings/mcp.json`` when absent (so a fresh
    user gets a wired server from one command) and then writes the canonical
    proxy entry via the mode-appropriate patch (extension vs headless config).

    Returns ``(mcp_json_path, status)`` where ``status`` is ``"registered"``
    (KiroCrew's proxy was written/refreshed) or ``"kept-user-entry"`` (a
    user-authored NON-proxy server already holds the canonical ``playwright-mcp``
    key, so we left it untouched rather than clobber their config — authorship is
    by launch target, not key name, mirroring the boot-time migration guard).
    """
    mcp_json = _kiro_mcp_json_path()
    canonical = mcp_server_alias(_PLAYWRIGHT_MCP_PACKAGE)
    # Serialize with the other writers of this SAME file — see _kiro_mcp_locked.
    # The lock spans our read + create + write so a concurrent gateway/bridge
    # update can't be clobbered (which would drop its server entries).
    with _kiro_mcp_locked():
        if mcp_json.exists():
            try:
                existing = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
            servers = existing.get("mcpServers") if isinstance(existing, dict) else None
            canon = servers.get(canonical) if isinstance(servers, dict) else None
            # A user may hand-author their OWN direct (non-proxy) server under
            # the canonical key. The patch helpers would overwrite it, silently
            # losing their config — so leave it untouched and report back.
            if canon is not None and not _spec_is_proxy(canon):
                return mcp_json, "kept-user-entry"
        else:
            mcp_json.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
        _patch_mcp_for_mode_unlocked()
    return mcp_json, "registered"


def inject_cookies_via_playwright(cookie_file: str | None = None) -> dict[str, Any]:
    """Parse the browser cookie file and return cookies in Playwright format.

    Args:
        cookie_file: Path to Netscape cookie file. Defaults to the resolved
            browser cookie path (``_cookie_path()``).

    Returns:
        Dict with "cookies" list and "count" integer.
    """
    path = Path(cookie_file) if cookie_file is not None else _cookie_path()
    cookies = parse_netscape_cookies(path)
    return {"cookies": cookies, "count": len(cookies)}
