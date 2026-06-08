"""Additional tests for kiro_claw.sandbox — wrap_argv, profiles, env scrubbing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_claw.sandbox import (
    _CC_FILES,
    _SENSITIVE_ENV_PREFIXES,
    _STRICT_DIRS,
    _build_launcher_script,
    _build_seatbelt_profile,
    _resolve_real_kiro_bin,
    _ssh_supports_accept_new,
    detect_backend,
    namespace_argv,
    reset_backend,
    sandbox_exec_argv,
    wrap_argv,
)


@pytest.fixture(autouse=True)
def clean_backend():
    """Reset cached backend between tests."""
    reset_backend()
    yield
    reset_backend()


class TestDetectBackend:
    def test_off_mode(self):
        result = detect_backend(config_mode="off")
        assert result == "none"

    @patch("kiro_claw.sandbox._probe_unshare", return_value=False)
    @patch("kiro_claw.sandbox._probe_sandbox_exec", return_value=False)
    def test_no_backend_available(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "none"

    @patch("kiro_claw.sandbox._probe_unshare", return_value=True)
    def test_linux_namespace(self, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "namespace"

    @patch("kiro_claw.sandbox._probe_unshare", return_value=False)
    @patch("kiro_claw.sandbox._probe_sandbox_exec", return_value=True)
    def test_macos_sandbox_exec(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "sandbox-exec"

    @patch("kiro_claw.sandbox._probe_unshare", return_value=True)
    def test_caches_result(self, mock_ns):
        detect_backend(config_mode="auto")
        detect_backend(config_mode="auto")
        # Only probed once due to caching
        assert mock_ns.call_count == 1

    @patch("kiro_claw.sandbox._probe_unshare", return_value=True)
    def test_invalidates_on_mode_change(self, mock_ns):
        detect_backend(config_mode="auto")
        detect_backend(config_mode="off")
        # Second call with different mode should re-evaluate
        assert mock_ns.call_count == 1  # off doesn't probe


class TestWrapArgv:
    @patch("kiro_claw.sandbox.detect_backend", return_value="none")
    def test_no_sandbox_returns_original(self, mock_detect):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="auto")
        assert result == argv
        assert cleanup is None

    def test_off_mode_returns_original(self):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="off")
        assert result == argv
        assert cleanup is None

    @patch("kiro_claw.sandbox.detect_backend", return_value="namespace")
    @patch("kiro_claw.sandbox.namespace_argv")
    def test_namespace_backend(self, mock_ns_argv, mock_detect):
        mock_ns_argv.return_value = [sys.executable, "/tmp/launcher.py", "kiro-cli"]
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_ns_argv.assert_called_once_with(["kiro-cli"], "strict")

    @patch("kiro_claw.sandbox.detect_backend", return_value="sandbox-exec")
    @patch("kiro_claw.sandbox.sandbox_exec_argv")
    def test_sandbox_exec_backend(self, mock_sb_argv, mock_detect):
        mock_sb_argv.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb")
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_sb_argv.assert_called_once_with(["kiro-cli"], "strict")


class TestBuildSeatbeltProfile:
    def test_strict_denies_all_dirs(self):
        profile = _build_seatbelt_profile("strict")
        assert "(version 1)" in profile
        assert "(deny file-read*" in profile
        home = str(Path.home())
        for d in _STRICT_DIRS:
            assert os.path.join(home, d) in profile

    def test_strict_denies_ssh_write(self):
        profile = _build_seatbelt_profile("strict")
        assert "(deny file-write*" in profile
        assert ".ssh" in profile

    def test_standard_does_not_deny_aws(self):
        profile = _build_seatbelt_profile("standard")
        home = str(Path.home())
        # Standard mode doesn't hide .aws
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_skips_aws_on_macos(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # CC mode on macOS doesn't hide .aws (credential_process needs it)
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_denies_individual_files(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        for f in _CC_FILES:
            assert os.path.join(home, f) in profile

    def test_cc_mode_skips_aws_dir(self):
        """CC mode does NOT deny .aws as a directory (credential_process needs it)."""
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # .aws should not appear as a subpath deny
        assert f'(subpath "{home}/.aws")' not in profile


class TestBuildLauncherScript:
    def test_strict_script_contains_dirs(self):
        script = _build_launcher_script("strict")
        assert "SENSITIVE_DIRS" in script
        assert ".aws" in script
        assert ".gnupg" in script

    def test_standard_script_excludes_aws(self):
        script = _build_launcher_script("standard")
        # Standard dirs don't include .aws
        assert "HIDE_SSH = False" in script

    def test_cc_script_exposes_aws_config(self):
        script = _build_launcher_script("cc")
        assert ".aws/config" in script
        assert "EXPOSE_FILES" in script

    def test_script_scrubs_env_vars(self):
        script = _build_launcher_script("strict")
        for prefix in _SENSITIVE_ENV_PREFIXES:
            assert prefix in script

    def test_strips_self_dir_before_ctypes_import(self):
        """The sys.path hardening must run before the first shadowable import.

        Regression guard for the /tmp/struct.py shadowing outage: ctypes does
        ``from struct import calcsize`` at import time, so the launcher dir must
        be removed from sys.path *before* ``import ctypes``.
        """
        script = _build_launcher_script("strict")
        assert "sys.path[:]" in script
        assert script.index("sys.path[:]") < script.index("import ctypes")
        # sys must be imported first (it is a builtin and cannot be shadowed).
        assert script.index("import sys") < script.index("sys.path[:]")


class TestLauncherStdlibShadowing:
    """End-to-end: a sibling /tmp/struct.py must NOT crash the launcher.

    Hermetic — every poison file lives in pytest's isolated tmp_path subdir,
    never bare /tmp, so the running gateway's launcher (sys.path[0] == /tmp) is
    never affected by these tests.
    """

    # A drop-in stdlib name that ctypes -> struct.calcsize depends on.
    _POISON = "def calcsize(*a, **k):\n    raise RuntimeError('shadowed!')\n"

    def _run_launcher(self, script_dir: Path) -> subprocess.CompletedProcess:
        """Write the launcher into script_dir and run it with no args.

        With no command argv the launcher exits immediately after its imports
        and the ``if not argv`` guard — it never forks/unshares/execs. So this
        exercises exactly the import path that the outage crashed on, and
        nothing else.
        """
        launcher = script_dir / "launcher.py"
        launcher.write_text(_build_launcher_script("standard"))
        return subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True, text=True, timeout=30,
        )

    def test_prelude_removes_script_dir_from_syspath(self, tmp_path):
        """Deterministic proof of the mechanism, independent of struct caching.

        Runs the launcher's real generated prelude (everything up to the first
        ``import ctypes``) from a tmp dir, then dumps sys.path. The script's own
        directory — which CPython puts at sys.path[0] — must be gone afterwards.
        Unlike the struct e2e below, this does not depend on whether the
        interpreter pre-imports ``struct``, so it always discriminates the fix.
        """
        script = _build_launcher_script("standard")
        prelude = script[: script.index("import ctypes")]
        probe = tmp_path / "launcher.py"
        probe.write_text(prelude + "import json\nprint(json.dumps(sys.path))\n")
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        import json
        paths = json.loads(result.stdout.strip().splitlines()[-1])
        assert str(tmp_path) not in paths, f"script dir not stripped: {paths}"
        assert "" not in paths, f"cwd entry not stripped: {paths}"

    def test_launcher_survives_sibling_struct_py(self, tmp_path):
        """With the fix, a sibling struct.py is ignored and imports succeed."""
        (tmp_path / "struct.py").write_text(self._POISON)
        result = self._run_launcher(tmp_path)
        # No-args launcher exits via sys.exit("...: no command given") AFTER all
        # imports succeed — so a clean "no command given" proves imports passed.
        assert "calcsize" not in result.stderr, result.stderr
        # The launcher binds Linux-only libc symbols (unshare) at module import
        # time; on non-Linux hosts it dies there, AFTER the shadowable stdlib
        # imports the fix guards, but BEFORE the argv guard. That still proves
        # the imports survived the poison; only the argv guard is unreachable.
        if "unshare" in result.stderr and "no command given" not in result.stderr:
            pytest.skip("launcher needs Linux-only libc unshare; not this host")
        assert "no command given" in result.stderr, (
            f"launcher did not reach the argv guard; stderr={result.stderr!r}"
        )

    def test_control_unstripped_launcher_would_crash(self, tmp_path):
        """Sanity: prove the poison is real — an un-hardened launcher DOES crash.

        Strips the hardening line so we don't silently ship a test that passes
        for the wrong reason. The poison only bites if the interpreter imports
        ``struct`` fresh (not already cached at startup); if a given build
        interpreter pre-caches ``struct``, the shadowing can't be demonstrated
        here, so we skip rather than red the build for an unrelated reason.
        """
        (tmp_path / "struct.py").write_text(self._POISON)
        hardened = _build_launcher_script("standard")
        unstripped = "\n".join(
            ln for ln in hardened.splitlines() if "sys.path[:]" not in ln
        )
        launcher = tmp_path / "launcher.py"
        launcher.write_text(unstripped)
        result = subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True, text=True, timeout=30,
        )
        if "no command given" in result.stderr:
            pytest.skip(
                "interpreter pre-caches 'struct'; sibling shadowing not "
                "reproducible here — positive test still guards the fix"
            )
        # Otherwise the shadowed struct broke the ctypes import -> launcher
        # died before reaching the argv guard, proving the poison is real.
        assert ("calcsize" in result.stderr) or ("shadowed!" in result.stderr), (
            f"expected a struct-shadowing import failure; stderr={result.stderr!r}"
        )


class TestSandboxExecArgv:
    @patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fake", "SSH_AUTH_SOCK": "/tmp/ssh"})
    def test_includes_env_unset_flags(self):
        argv, profile_path = sandbox_exec_argv(["kiro-cli", "acp"], "strict")
        try:
            assert "env" == argv[0]
            assert "-u" in argv
            assert "AWS_SECRET_ACCESS_KEY" in argv
            assert "SSH_AUTH_SOCK" in argv
            assert "sandbox-exec" in argv
            assert "-f" in argv
            assert profile_path is not None
            assert os.path.exists(profile_path)
        finally:
            if profile_path:
                os.unlink(profile_path)

    def test_creates_temp_profile(self):
        argv, profile_path = sandbox_exec_argv(["echo", "hi"], "strict")
        try:
            assert profile_path is not None
            content = Path(profile_path).read_text()
            assert "(version 1)" in content
        finally:
            if profile_path:
                os.unlink(profile_path)


class TestNamespaceArgv:
    @patch("kiro_claw.sandbox._resolve_real_kiro_bin", return_value="/usr/local/bin/kiro-cli")
    def test_wraps_with_python_launcher(self, mock_resolve):
        result = namespace_argv(["kiro-cli", "acp"], "strict")
        assert result[0] == sys.executable
        assert result[1].endswith(".py")
        assert result[2] == "/usr/local/bin/kiro-cli"
        assert result[3] == "acp"
        # Cleanup temp file
        os.unlink(result[1])

    @patch("kiro_claw.sandbox._resolve_real_kiro_bin", return_value="/usr/local/bin/kiro-cli")
    def test_launcher_script_is_executable(self, mock_resolve):
        result = namespace_argv(["kiro-cli"], "strict")
        launcher_path = result[1]
        mode = os.stat(launcher_path).st_mode
        assert mode & 0o700 == 0o700
        os.unlink(launcher_path)


class TestSshSupportsAcceptNew:
    def test_modern_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_9.2p1 Debian-2, OpenSSL 3.0.8")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is True
        _ssh_supports_accept_new.cache_clear()

    def test_old_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_7.4p1, OpenSSL 1.0.2k")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()

    def test_ssh_not_found(self):
        _ssh_supports_accept_new.cache_clear()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()


class TestResolveRealKiroBin:
    def test_non_kiro_binary_returns_unchanged(self):
        assert _resolve_real_kiro_bin("/usr/bin/python3") == "/usr/bin/python3"

    def test_kiro_cli_fallback_when_no_real_binary(self):
        with patch("subprocess.run", return_value=MagicMock(stdout=b"")):
            result = _resolve_real_kiro_bin("/usr/local/bin/kiro-cli")
        assert result == "/usr/local/bin/kiro-cli"
