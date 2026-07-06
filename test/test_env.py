"""Tests for kiro_claw.env."""

from __future__ import annotations

import json
import os
import stat
import subprocess

from kiro_claw.env import (
    _node_version_manager_bins,
    activate_mise,
    augmented_path,
    resolve_krb5_ccname,
)


def _fake_run(stdout="", returncode=0, stderr=""):
    """Return a subprocess.run replacement that yields a canned CompletedProcess."""

    def _run(argv, **kwargs):  # noqa: ANN001 - test shim
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return _run


def _fake_statfns(spec):
    """Build ``(lstat, stat)`` replacements for ccache-resolution tests.

    ``spec`` maps a path to a descriptor:
      * ``("reg", owner)``           regular file owned by uid ``owner``
      * ``("link", owner, target)``  symlink owned by uid ``owner``; on
                                     ``os.stat`` (follow) it resolves to a
                                     regular file owned by uid ``target``
                                     (``target=None`` = broken/dangling link).
    Any path absent from ``spec`` raises ``OSError`` from both functions.
    """

    def _result(mode, owner):
        return os.stat_result((mode | 0o600, 0, 0, 1, owner, 0, 0, 0, 0, 0))

    def _lstat(path):  # inspects the link itself, does NOT follow
        d = spec.get(path)
        if d is None:
            raise OSError("no such file")
        if d[0] == "reg":
            return _result(stat.S_IFREG, d[1])
        return _result(stat.S_IFLNK, d[1])  # "link"

    def _stat(path):  # follows symlinks
        d = spec.get(path)
        if d is None:
            raise OSError("no such file")
        if d[0] == "reg":
            return _result(stat.S_IFREG, d[1])
        if d[2] is None:  # "link" with dangling target
            raise OSError("dangling symlink")
        return _result(stat.S_IFREG, d[2])

    return _lstat, _stat


def _patch_statfns(monkeypatch, spec, *, uid=4242):
    """Patch os.getuid/os.lstat/os.stat in kiro_claw.env for a ccache test."""
    monkeypatch.setattr("kiro_claw.env.os.getuid", lambda: uid)
    lstat, stat_fn = _fake_statfns(spec)
    monkeypatch.setattr("kiro_claw.env.os.lstat", lstat)
    monkeypatch.setattr("kiro_claw.env.os.stat", stat_fn)


class TestAugmentedPath:
    def test_prepends_aim_mcp_servers(self) -> None:
        result = augmented_path("/usr/bin")
        dirs = result.split(os.pathsep)
        assert dirs[-1] == "/usr/bin"
        assert any(".aim/mcp-servers" in d for d in dirs)

    def test_aim_before_toolbox(self) -> None:
        result = augmented_path("")
        dirs = result.split(os.pathsep)
        aim_idx = next(i for i, d in enumerate(dirs) if ".aim/mcp-servers" in d)
        toolbox_idx = next(i for i, d in enumerate(dirs) if ".toolbox/bin" in d)
        assert aim_idx < toolbox_idx

    def test_empty_base(self) -> None:
        result = augmented_path("")
        assert result  # not empty
        assert not result.endswith(os.pathsep)  # no trailing separator

    def test_no_arg_defaults_empty(self) -> None:
        result = augmented_path()
        assert ".aim/mcp-servers" in result

    def test_includes_nvm_node_bins(self, tmp_path, monkeypatch) -> None:
        # Simulate a home with two nvm-installed node versions.
        nvm = tmp_path / ".nvm" / "versions" / "node"
        (nvm / "v18.0.0" / "bin").mkdir(parents=True)
        (nvm / "v22.5.0" / "bin").mkdir(parents=True)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        dirs = augmented_path("/usr/bin").split(os.pathsep)
        nvm_bins = [d for d in dirs if ".nvm/versions/node" in d]
        assert len(nvm_bins) == 2
        # Newest version first (reverse-sorted).
        assert "v22.5.0" in nvm_bins[0]
        assert "v18.0.0" in nvm_bins[1]


class TestNodeVersionManagerBins:
    def test_empty_when_no_managers(self, tmp_path) -> None:
        assert _node_version_manager_bins(str(tmp_path)) == []

    def test_skips_version_dir_without_bin(self, tmp_path) -> None:
        # A node version dir that has no bin/ subdir is ignored.
        (tmp_path / ".nvm" / "versions" / "node" / "v20.0.0").mkdir(parents=True)
        assert _node_version_manager_bins(str(tmp_path)) == []

    def test_returns_existing_bin(self, tmp_path) -> None:
        bin_dir = tmp_path / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        bin_dir.mkdir(parents=True)
        result = _node_version_manager_bins(str(tmp_path))
        assert result == [str(bin_dir)]


class TestResolveKrb5Ccname:
    def test_prefers_uid_ccache(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_falls_back_to_username_ccache(self, monkeypatch) -> None:
        import getpass

        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        monkeypatch.setattr(getpass, "getuser", lambda: "tuser")
        # uid path missing, username path present
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_tuser": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_tuser"

    def test_respects_existing_file_value(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "FILE:/custom/cc"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/custom/cc"  # operator override wins

    def test_overrides_keyring_value(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "KEYRING:persistent:1000"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_noop_when_no_cache_file(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_follows_uid_owned_symlink(self, monkeypatch) -> None:
        # sssd/systemd ship /tmp/krb5cc_<uid> as a uid-owned symlink into
        # /run/user/<uid>/krb5cc/... — follow it and trust the resolved target.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_rejects_foreign_owned_symlink(self, monkeypatch) -> None:
        # A symlink owned by another uid is the attack vector — reject without
        # following (a co-tenant could point it anywhere).
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 9999, 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_uid_symlink_to_foreign_target(self, monkeypatch) -> None:
        # uid-owned symlink whose resolved target is owned by someone else.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, 9999)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_dangling_uid_symlink(self, monkeypatch) -> None:
        # uid-owned symlink whose target does not exist.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, None)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_foreign_owned_ccache(self, monkeypatch) -> None:
        # A regular file owned by a different uid (planted by a co-tenant on a
        # shared /tmp) must NOT be trusted.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 9999)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_preserves_kcm_scheme(self, monkeypatch) -> None:
        # macOS default is KCM: — a stale /tmp file must NOT hijack it.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "darwin")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "KCM:"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "KCM:"

    def test_preserves_dir_scheme(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "DIR:/run/user/4242/krb5cc"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "DIR:/run/user/4242/krb5cc"

    def test_noop_on_non_linux(self, monkeypatch) -> None:
        # On macOS with empty KRB5CCNAME, a stale /tmp file must not be adopted.
        monkeypatch.setattr("kiro_claw.env.sys.platform", "darwin")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_logs_resolved_path_on_success(self, monkeypatch, caplog) -> None:
        import logging

        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_claw.env"):
            resolve_krb5_ccname(env)
        assert "FILE:/tmp/krb5cc_4242" in caplog.text

    def test_logs_rejection_reason(self, monkeypatch, caplog) -> None:
        # A present-but-rejected candidate must be logged with its reason so it
        # is distinguishable from the plain "no ccache" no-op.
        import logging

        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 9999)})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_claw.env"):
            resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env
        assert "foreign-owned" in caplog.text

    def test_no_log_when_no_candidate(self, monkeypatch, caplog) -> None:
        # The ordinary "no ccache present" case must NOT emit a rejection log.
        import logging

        monkeypatch.setattr("kiro_claw.env.sys.platform", "linux")
        _patch_statfns(monkeypatch, {})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_claw.env"):
            resolve_krb5_ccname(env)
        assert "rejected ccache candidate" not in caplog.text


class TestActivateMise:
    def test_noop_when_mise_absent(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: None)
        env: dict[str, str] = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}

    def test_noop_when_disabled_via_env(self, monkeypatch) -> None:
        # KIROCLAW_NO_MISE escape hatch short-circuits before mise is invoked.
        called = {"n": 0}
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: called.__setitem__("n", 1))
        env = {"PATH": "/usr/bin", "KIROCLAW_NO_MISE": "1"}
        assert activate_mise(env) == []
        assert called["n"] == 0  # _mise_bin never consulted

    def test_merges_path_and_added_vars(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/home/u/.local/bin/mise")
        payload = json.dumps(
            {
                "PATH": "/home/u/.local/share/mise/installs/node/24/bin:/usr/bin",
                "NODE_ENV": "production",
            }
        )
        monkeypatch.setattr("kiro_claw.env.subprocess.run", _fake_run(stdout=payload))
        env = {"PATH": "/usr/bin"}
        changed = activate_mise(env)
        assert changed == ["NODE_ENV", "PATH"]  # sorted
        assert env["PATH"].startswith("/home/u/.local/share/mise/installs/node/24/bin")
        assert env["NODE_ENV"] == "production"

    def test_skips_unchanged_vars(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")
        payload = json.dumps({"PATH": "/usr/bin"})  # identical to current
        monkeypatch.setattr("kiro_claw.env.subprocess.run", _fake_run(stdout=payload))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_nonzero_exit_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")
        monkeypatch.setattr(
            "kiro_claw.env.subprocess.run", _fake_run(returncode=1, stderr="boom")
        )
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}

    def test_unparsable_json_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")
        monkeypatch.setattr("kiro_claw.env.subprocess.run", _fake_run(stdout="not json{"))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_non_dict_json_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")
        monkeypatch.setattr("kiro_claw.env.subprocess.run", _fake_run(stdout="[1, 2, 3]"))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_skips_non_string_values(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")
        payload = json.dumps({"PATH": "/new", "BOGUS": 42, "ALSO": None})
        monkeypatch.setattr("kiro_claw.env.subprocess.run", _fake_run(stdout=payload))
        env: dict[str, str] = {}
        assert activate_mise(env) == ["PATH"]
        assert env == {"PATH": "/new"}

    def test_subprocess_failure_is_swallowed(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_claw.env._mise_bin", lambda: "/m")

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr("kiro_claw.env.subprocess.run", _boom)
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}


class TestNodeVersionManagerBinsCache:
    """Verify _node_version_manager_bins is cached (lru_cache) to prevent
    repeated filesystem I/O on the event-loop thread under GIL pressure."""

    def test_is_cached(self, tmp_path) -> None:
        """Second call with same arg returns cached result without re-globbing."""
        _node_version_manager_bins.cache_clear()
        nvm = tmp_path / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        nvm.mkdir(parents=True)
        result1 = _node_version_manager_bins(str(tmp_path))
        # Remove the dir -- a non-cached implementation would return [] now
        nvm.rmdir()
        result2 = _node_version_manager_bins(str(tmp_path))
        assert result1 == result2 == [str(nvm)]
        _node_version_manager_bins.cache_clear()

    def test_cache_info_exists(self) -> None:
        """lru_cache exposes cache_info -- confirms decorator is applied."""
        assert hasattr(_node_version_manager_bins, "cache_info")
        assert hasattr(_node_version_manager_bins, "cache_clear")
