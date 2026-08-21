"""``agent.extra_path_dirs`` — the user-extensible MCP binary search path (#3030).

``_EXTRA_PATH_DIRS`` is a fixed tuple, so before this key a gateway that did not
inherit a login shell's ``PATH`` could not resolve an MCP binary installed
anywhere else, with no supported way to fix it.

The key is RESOLUTION-ONLY: it decides what a bare command name binds to, and
never what a launched child's ``PATH`` contains. These tests pin the capability
and the four properties that make it safe:

* it reaches MCP spec resolution ONLY, never the shared ``augmented_path()``
  that locates the ACP harness (the config file is writable outside the
  file-edit gate, so harness selection must not be fed from it);
* it is never EMITTED into a config file, because every such file is a
  read-modify-write source on the next rebuild -- persisting a revocable
  setting there would make it permanent, and it would return as a *declared*
  entry, which ranks first;
* entries land at the lowest precedence, so nothing that resolves today can be
  rebound;
* a malformed or absent value is a strict no-op.

Path fixtures are built through :func:`_abs` and ``os.pathsep`` rather than
written as POSIX literals: on Windows ``os.pathsep`` is ``;`` (so a colon is not
separator-smuggling), ``ntpath.isabs("/opt/x")`` is True, and ``normpath``
returns backslashes -- a hardcoded ``"/opt/a:/opt/b"`` would silently test
something different there.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from kiro_crew import env as env_mod
from kiro_crew.env import augmented_path, describe_search_path, emit_env, spec_env_path


def _abs(*parts: str) -> str:
    """A platform-correct absolute directory, normalized as the code normalizes."""
    root = "C:\\" if os.name == "nt" else "/"
    return os.path.normpath(os.path.join(root, *parts))


VENDOR = _abs("opt", "vendor", "mcp", "bin")
OK = _abs("opt", "ok", "bin")
ONE = _abs("opt", "one", "bin")
TWO = _abs("opt", "two", "bin")
USR_BIN = _abs("usr", "bin")
BIN = _abs("bin")
BASE_PATH = os.pathsep.join([USR_BIN, BIN])


@pytest.fixture(autouse=True)
def _clear_caches():
    """The helper is cached for the process, so every test must start cold."""
    env_mod._configured_extra_path_dirs.cache_clear()
    yield
    env_mod._configured_extra_path_dirs.cache_clear()


def _configure(monkeypatch, value):
    """Point the on-disk read at *value* without touching a real config file."""
    monkeypatch.setattr(env_mod, "_raw_extra_path_dirs", lambda: value)


def _entries(path: str) -> list[str]:
    return [p for p in path.split(os.pathsep) if p]


# ── the capability ────────────────────────────────────────────────────────────


def test_configured_dir_becomes_searchable(monkeypatch):
    """The reported gap: a binary outside the built-in list must resolve."""
    _configure(monkeypatch, [VENDOR])
    assert VENDOR in _entries(spec_env_path(""))


@pytest.mark.skipif(
    os.name == "nt",
    reason="a shebang script is not PATHEXT-executable on Windows; the ordering "
    "and isolation properties below cover this platform",
)
def test_a_binary_only_in_a_configured_dir_is_found(monkeypatch, tmp_path):
    """End to end through ``shutil.which``, not just string membership."""
    bindir = tmp_path / "vendorbin"
    bindir.mkdir()
    tool = bindir / "vendor-mcp-server"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    assert shutil.which("vendor-mcp-server", path=BASE_PATH) is None
    _configure(monkeypatch, [str(bindir)])
    assert shutil.which("vendor-mcp-server", path=spec_env_path("")) == str(tool)


def test_a_spec_declared_path_still_wins(monkeypatch, tmp_path):
    """The key must not disturb a spec that pins its own PATH first."""
    _configure(monkeypatch, [VENDOR])
    entries = _entries(spec_env_path(str(tmp_path)))
    assert entries[0] == str(tmp_path)
    assert entries.index(str(tmp_path)) < entries.index(VENDOR)


# ── resolution-only: never persisted into a config file ───────────────────────


def test_emitted_env_never_carries_configured_dirs(monkeypatch):
    """The key must not reach ANY emitted config.

    Every file ``emit_env`` feeds is a read-modify-write source on the next
    rebuild -- ``install_agent`` loads the existing agent config to "preserve
    user customizations", and ``~/.kiro/settings/mcp.json`` is written
    create-only and read back. A configured dir persisted there could not be
    removed by clearing the config, and would return as a DECLARED entry, which
    ranks FIRST -- inverting the lowest-precedence property below.
    """
    _configure(monkeypatch, [VENDOR])
    out = emit_env({"PATH": OK})
    assert OK in _entries(out["PATH"])  # the declaration survives
    assert VENDOR not in _entries(out["PATH"])  # the revocable setting does not


def test_emission_stays_env_less_for_an_env_less_spec(monkeypatch):
    """Resolution-only means no spec gains an env it did not have."""
    _configure(monkeypatch, [VENDOR])
    assert emit_env({}) == {}
    assert emit_env({"TOKEN": "t"}) == {"TOKEN": "t"}


def test_clearing_the_key_fully_revokes(monkeypatch):
    """Nothing persisted, so re-expansion after removal cannot resurrect it."""
    _configure(monkeypatch, [VENDOR])
    persisted = emit_env({"PATH": OK})["PATH"]
    env_mod._configured_extra_path_dirs.cache_clear()
    _configure(monkeypatch, [])
    assert VENDOR not in _entries(spec_env_path(persisted))


def test_probe_spawn_path_matches_the_session(monkeypatch):
    """The probe child must not get a WIDER PATH than a session child.

    Resolution may use the configured dirs; the spawn must not, or a launcher
    needing a sibling from that directory passes the probe and dies in a session.
    """
    _configure(monkeypatch, [VENDOR])
    session_path = emit_env({"PATH": OK})["PATH"]
    probe_spawn_path = spec_env_path(OK, include_extra_dirs=False)
    assert probe_spawn_path == session_path
    assert VENDOR not in _entries(probe_spawn_path)


# ── the security property: harness resolution must NOT see this key ───────────


def test_shared_augmented_path_never_carries_configured_dirs(monkeypatch):
    """Regression pin for the escalation this key must not open.

    ``kiro_cli.known_kiro_cli_dirs`` locates the ACP HARNESS through
    ``augmented_path()``, and acp/client, acp/runtime, browser_cli and the
    agents handler build child environments from it. The config file is
    writable outside the file-edit gate, so a configured directory reaching
    this function would let a planted binary be selected as the harness.
    """
    _configure(monkeypatch, [VENDOR])
    assert VENDOR not in _entries(augmented_path(BASE_PATH))
    assert VENDOR not in _entries(augmented_path(""))


def test_kiro_cli_candidate_dirs_never_carry_configured_dirs(monkeypatch):
    """The same property asserted at the harness resolver itself."""
    from kiro_crew.kiro_cli import known_kiro_cli_dirs

    _configure(monkeypatch, [VENDOR])
    dirs = known_kiro_cli_dirs(
        "linux", Path(os.path.expanduser("~")), {"PATH": BASE_PATH}, include_inherited_path=True
    )
    assert VENDOR not in dirs


# ── the precedence property ───────────────────────────────────────────────────


def test_entries_rank_below_the_inherited_path(monkeypatch):
    """Strict addition: a user dir can never shadow a name PATH already resolves."""
    monkeypatch.setenv("PATH", BASE_PATH)
    _configure(monkeypatch, [VENDOR])
    entries = _entries(spec_env_path(""))
    assert entries.index(USR_BIN) < entries.index(VENDOR)
    assert entries.index(BIN) < entries.index(VENDOR)


def test_entries_rank_below_the_builtin_dirs(monkeypatch):
    _configure(monkeypatch, [VENDOR])
    entries = _entries(spec_env_path(""))
    home_local = os.path.join(os.path.expanduser("~"), ".local", "bin")
    if home_local in entries:  # built-ins are host-shaped; assert only when present
        assert entries.index(home_local) < entries.index(VENDOR)


def test_entries_rank_below_the_interpreter_fallback(monkeypatch):
    """Last of all: the key resolves only what nothing else can."""
    _configure(monkeypatch, [VENDOR])
    entries = _entries(spec_env_path(""))
    interp = str(Path(sys.executable).parent)
    if interp in entries:
        assert entries.index(interp) < entries.index(VENDOR)
    assert entries[-1] == VENDOR


def test_empty_config_is_byte_identical_to_no_key(monkeypatch):
    """An empty list must be a strict no-op, not a reordering."""
    _configure(monkeypatch, [])
    with_empty = spec_env_path("")
    env_mod._configured_extra_path_dirs.cache_clear()
    _configure(monkeypatch, [])
    assert with_empty == spec_env_path("")


def test_unreadable_config_does_not_break_resolution(monkeypatch):
    """A config that raises must not make a previously-resolvable binary vanish."""

    def _boom():
        raise RuntimeError("config on fire")

    monkeypatch.setattr(env_mod, "_raw_extra_path_dirs", _boom)
    monkeypatch.setenv("PATH", BASE_PATH)
    assert USR_BIN in _entries(spec_env_path(""))  # built, no exception escaped


# ── reading the key off disk ──────────────────────────────────────────────────


def test_reads_the_key_from_config_json(monkeypatch, tmp_path):
    """Covers the real on-disk read, not just the patched helper."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agent": {"extra_path_dirs": ["/opt/from/base"]}}))
    missing = tmp_path / "config.local.json"
    # Patch the SOURCE module: _raw_extra_path_dirs re-imports these inside its
    # body, so patching a copy held elsewhere would be silently ignored.
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: missing)
    assert env_mod._raw_extra_path_dirs() == ["/opt/from/base"]


def test_local_overlay_wins(monkeypatch, tmp_path):
    """Same precedence ``KiroCrewConfig.load()`` gives the overlay."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agent": {"extra_path_dirs": ["/opt/from/base"]}}))
    local = tmp_path / "config.local.json"
    local.write_text(json.dumps({"agent": {"extra_path_dirs": ["/opt/from/local"]}}))
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: local)
    assert env_mod._raw_extra_path_dirs() == ["/opt/from/local"]


def test_absent_and_malformed_files_yield_empty(monkeypatch, tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{ not json")
    missing = tmp_path / "nope.json"
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: bad)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: missing)
    assert env_mod._raw_extra_path_dirs() == []


def test_read_never_writes_the_config(monkeypatch, tmp_path):
    """The read must not migrate or rewrite config -- it runs on the event loop."""
    cfg = tmp_path / "config.json"
    body = json.dumps({"agent": {"extra_path_dirs": ["/opt/x/bin"]}})
    cfg.write_text(body)
    before = cfg.stat().st_mtime_ns
    missing = tmp_path / "nope.json"
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: missing)
    env_mod._raw_extra_path_dirs()
    assert cfg.read_text() == body
    assert cfg.stat().st_mtime_ns == before


def test_local_null_override_revokes_the_base_value(monkeypatch, tmp_path):
    """Presence wins: a malformed local override must not preserve the base.

    ``"extra_path_dirs": null`` in the local file is how a user turns the setting
    off; preserving the base value there would leave a directory active that they
    believe they revoked.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agent": {"extra_path_dirs": ["/opt/from/base"]}}))
    local = tmp_path / "config.local.json"
    local.write_text(json.dumps({"agent": {"extra_path_dirs": None}}))
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: local)
    assert env_mod._raw_extra_path_dirs() == []


def test_local_file_without_the_key_keeps_the_base_value(monkeypatch, tmp_path):
    """Absence is not an override -- only a PRESENT key replaces."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agent": {"extra_path_dirs": ["/opt/from/base"]}}))
    local = tmp_path / "config.local.json"
    local.write_text(json.dumps({"agent": {"bot_name": "x"}}))
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
    monkeypatch.setattr("kiro_crew.config.loader.config_local_path", lambda: local)
    assert env_mod._raw_extra_path_dirs() == ["/opt/from/base"]


def test_resync_guard_compares_against_what_was_emitted(monkeypatch):
    """``_envs_agree`` must not recompute with the extra dirs.

    ``emit_env`` stores a PATH without them, so recomputing with them makes any
    server declaring its own env.PATH look changed on every pass -- an endless
    rebuild plus an audit entry, the re-sync the guard exists to prevent.
    """
    from kiro_crew.mcp_discovery import _envs_agree

    _configure(monkeypatch, [VENDOR])
    stored = emit_env({"PATH": OK})["PATH"]
    assert VENDOR not in _entries(stored)
    # The guard must consider the stored value in agreement with its source.
    assert _envs_agree({"PATH": stored}, {"PATH": OK}) is True


# ── entry validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        os.path.join("relative", "bin"),  # re-resolved against the CHILD's cwd
        "",
        "   ",
        os.pathsep.join([_abs("opt", "a"), _abs("opt", "b")]),  # separator smuggling
        _abs("opt", "\0bin"),
    ],
)
def test_invalid_entries_are_rejected(monkeypatch, bad):
    _configure(monkeypatch, [bad])
    assert env_mod._configured_extra_path_dirs(os.path.expanduser("~")) == ()


def test_non_string_entries_are_skipped(monkeypatch):
    _configure(monkeypatch, [5, None, {"a": 1}, OK])
    assert env_mod._configured_extra_path_dirs(os.path.expanduser("~")) == (OK,)


def test_tilde_is_expanded(monkeypatch):
    _configure(monkeypatch, ["~/.deno/bin"])
    home = os.path.expanduser("~")
    assert env_mod._configured_extra_path_dirs(home) == (
        os.path.normpath(os.path.join(home, ".deno", "bin")),
    )


def test_dollar_vars_are_not_expanded(monkeypatch):
    """``$VAR`` must NOT expand -- that would reopen the env-var route this key
    declines, letting a lower-trust parent process inject a directory."""
    monkeypatch.setenv("EVIL_DIR", os.path.join("tmp", "evil"))
    _configure(monkeypatch, [os.path.join("$EVIL_DIR", "bin")])
    assert env_mod._configured_extra_path_dirs(os.path.expanduser("~")) == ()


def test_duplicates_collapse(monkeypatch):
    _configure(monkeypatch, [OK, OK, os.path.join(OK, ".", "")])
    assert env_mod._configured_extra_path_dirs(os.path.expanduser("~")) == (OK,)


def test_order_among_configured_entries_is_preserved(monkeypatch):
    _configure(monkeypatch, [ONE, TWO])
    entries = _entries(spec_env_path(""))
    assert entries.index(ONE) < entries.index(TWO)


# ── the message half of #3030 ─────────────────────────────────────────────────


def test_describe_search_path_names_the_directories():
    out = describe_search_path(BASE_PATH)
    assert USR_BIN in out and BIN in out
    assert "2" in out  # states how many were searched


def test_describe_search_path_truncates_but_states_the_total():
    limit = env_mod._SEARCH_PATH_REPORT_LIMIT
    total = limit + 20
    many = os.pathsep.join(_abs(f"d{i}") for i in range(total))
    out = describe_search_path(many)
    assert f"searched {total} directories" in out
    assert f"+{total - limit} more" in out
    assert _abs(f"d{total - 1}") not in out


def test_describe_search_path_handles_empty():
    assert "empty PATH" in describe_search_path("")


def test_probe_error_points_at_the_remedy():
    """The dashboard string must distinguish 'not installed' from 'not covered'."""
    from kiro_crew.mcp_discovery import _unresolved_error

    msg = _unresolved_error("vendor-mcp")
    assert "command not found: vendor-mcp" in msg  # prefix preserved for callers
    assert "agent.extra_path_dirs" in msg


def test_probe_warning_lists_the_searched_directories(caplog):
    from kiro_crew import mcp_discovery

    mcp_discovery._unresolvable_warned.clear()
    with caplog.at_level("WARNING"):
        mcp_discovery._warn_unresolvable_once("srv", "ghost", BASE_PATH)
    assert USR_BIN in caplog.text
    assert "ghost" in caplog.text
