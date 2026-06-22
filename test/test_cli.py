"""Tests for CLI module."""

import argparse
import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_claw.cli_commands import _cron
from kiro_claw.cli_doctor import _doctor
from kiro_claw.cli_server import _update


async def _noop_probe_server(server):
    """Default probe stub for tests that call ``_doctor()`` but aren't
    specifically exercising the MCP handshake. Marks the target healthy
    so doctor renders the MCP section cleanly without spawning a real
    child process.

    Tests that care about specific probe outcomes (success with tool
    count, failure with stderr, etc.) build their own probe mocks via
    ``TestDoctorMcpTools._mock_probe``.
    """
    server.status = "ok"
    server.tools = []
    return server


def _write_agent_config(
    path: Path, *, tools: list[str], allowed: list[str], servers: dict
) -> None:
    """Write a ``kiroclaw.json`` agent config with the given managed
    servers + tool references. Typed keyword arguments make it obvious
    which fields each test cares about.
    """
    path.write_text(
        json.dumps(
            {
                "name": "kiroclaw",
                "tools": tools,
                "allowedTools": allowed,
                "mcpServers": servers,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _healthy_agent_file(path: Path) -> None:
    """Write a ``kiroclaw.json`` whose managed MCP servers are all present
    so ``_doctor()``'s MCP section passes its static config check and only
    the (mocked) live probe remains. Used by doctor tests that exercise an
    unrelated section and must not trip the MCP exit path on an empty config.
    """
    _write_agent_config(
        path,
        tools=["@kiroclaw-core", "@kiroclaw-cron"],
        allowed=["@kiroclaw-core", "@kiroclaw-cron"],
        servers={
            "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
            "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
        },
    )


def _pin_default_config(monkeypatch) -> None:
    """Make ``_doctor()``'s config read hermetic for doctor tests.

    ``_doctor()`` calls the real ``KiroClawConfig.load()`` / ``load_credentials()``,
    which read the shared ``~/.kiroclaw`` config at runtime. ``KiroClawConfig.save()``
    writes that same shared path non-atomically, so under ``pytest -n auto`` a
    concurrent worker's config write races these reads: a polluted/foreign config
    flips a check and ``_doctor()`` exits 1. xdist worker interleaving differs per
    interpreter, so the flake surfaced only on python3.10. Pin both to a pristine
    default (Slack-less, STT disabled) so doctor runs are deterministic and isolated.
    """
    from kiro_claw.config.loader import KiroClawConfig

    monkeypatch.setattr(KiroClawConfig, "load", classmethod(lambda cls: cls()))
    monkeypatch.setattr(KiroClawConfig, "load_credentials", lambda self: {})


class TestDoctor:
    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    def test_doctor_with_kiro(self, tmp_path):
        agent_file = tmp_path / "kiroclaw.json"
        # A minimally healthy agent config so doctor walks the whole MCP
        # section cleanly and doesn't exit on "missing from mcpServers".
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()

    def test_doctor_without_kiro(self, tmp_path):
        with (
            patch("kiro_claw.cli_doctor.shutil.which", return_value=None),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
        ):
            try:
                _doctor()
            except SystemExit as e:
                assert e.code == 1

    @patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 1234 5.6.7.8 22"})
    def test_doctor_remote_shows_ssh_tunnel_hint(self, tmp_path, capsys):
        agent_file = tmp_path / "kiroclaw.json"
        agent_file.write_text("{}")
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=False),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.machine_hostname", return_value="myhost"),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "ssh -NL" in out

    def test_doctor_slack_workspace_allowed_ok(self, tmp_path, capsys):
        """Slack configured + the bot token in the configured workspace
        allowlist -> doctor reports the workspace OK. validate_enterprise is
        mocked True so no live slack_sdk auth.test fires (its own logic is
        covered by test_enterprise.py); this covers the doctor-side success
        branch."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        slack_creds = {
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "KIROCLAW_OWNER_ID": "U123",
        }
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load_credentials", return_value=slack_creds),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True) as mock_ve,
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "✅ configured" in out
        assert "  workspace:   ✅ allowed" in out
        mock_ve.assert_called_once()

    def test_doctor_slack_workspace_not_allowed_flags_issue(self, tmp_path, capsys):
        """Slack configured but the bot token NOT in the configured workspace
        allowlist is a blocking issue: doctor prints the warning and exits 1.
        validate_enterprise is mocked False so no live auth.test fires; covers
        the doctor-side failure branch + the resulting sys.exit(1)."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        slack_creds = {"SLACK_APP_TOKEN": "xapp-test", "SLACK_BOT_TOKEN": "xoxb-test"}
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load_credentials", return_value=slack_creds),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc:
                _doctor()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "❌ not in configured workspace allowlist" in out


class TestSetupWorkspaceDir:
    """Tests for _setup_workspace_dir prompt default and label logic."""

    def test_uses_saved_path_as_default(self, tmp_path, monkeypatch):
        ws_file = tmp_path / "workspace_dir"
        ws_file.write_text("/custom/workspace\n")
        custom_dir = tmp_path / "custom"
        monkeypatch.setattr(
            "kiro_claw.cli_setup._workspace_dir_file", lambda: ws_file
        )
        with patch("builtins.input", return_value=str(custom_dir)) as mock_input:
            from kiro_claw.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        prompt = mock_input.call_args[0][0]
        assert "/custom/workspace" in prompt

    def test_shows_configured_label_when_saved(self, tmp_path, monkeypatch, capsys):
        ws_file = tmp_path / "workspace_dir"
        ws_file.write_text("/custom/workspace\n")
        custom_dir = tmp_path / "custom"
        monkeypatch.setattr(
            "kiro_claw.cli_setup._workspace_dir_file", lambda: ws_file
        )
        with patch("builtins.input", return_value=str(custom_dir)):
            from kiro_claw.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        output = capsys.readouterr().out
        assert "Configured:" in output

    def test_shows_default_label_when_no_saved(self, tmp_path, monkeypatch, capsys):
        ws_file = tmp_path / "no_such_file"
        custom_dir = tmp_path / "ws"
        monkeypatch.setattr(
            "kiro_claw.cli_setup._workspace_dir_file", lambda: ws_file
        )
        with patch("builtins.input", return_value=str(custom_dir)):
            from kiro_claw.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        output = capsys.readouterr().out
        assert "Default:" in output


# Common patches for _update tests — simulate a source tree with a git pull that has changes
_UPDATE_PATCHES = {
    "KIROCLAW_PROJECT_DIR": "/fake/proj",
}


def _patch_path():
    """Mock Path so .git check passes, .install-method is absent, and .brazil dir exists."""
    mock_git_dir = MagicMock(is_dir=MagicMock(return_value=True))
    mock_install_method = MagicMock(is_file=MagicMock(return_value=False))
    mock_brazil_dir = MagicMock(is_dir=MagicMock(return_value=True))

    def _truediv(self, key):
        if key == ".install-method":
            return mock_install_method
        if key == ".brazil":
            return mock_brazil_dir
        return mock_git_dir

    mock_path_inst = MagicMock()
    mock_path_inst.__truediv__ = _truediv
    mock_path_inst.parent.parent = MagicMock()
    mock_path_inst.parent.parent.__truediv__ = _truediv
    mock_path_inst.parent.parent.__str__ = lambda self: "/fake/ws"
    return patch("kiro_claw.cli_server.Path", return_value=mock_path_inst)


class TestUpdateFailures:
    """Tests for _update build-step failure handling (public pip/git flow).

    The Brazil ``brazil-build`` step was removed during de-Amazoning; the
    public update flow is git fetch/reset + npm build + ``pip install -e .``.
    A non-zero return code from a critical step exits with code 1.
    """

    @patch.dict("os.environ", _UPDATE_PATCHES)
    def test_git_fetch_failure_exits(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            if cmd and "fetch" in cmd:
                m.returncode = 1
                m.stderr = "network error"
            return m

        with _patch_path(), patch("subprocess.run", side_effect=_side_effect):
            try:
                _update()
                assert False, "Expected SystemExit"
            except SystemExit as e:
                assert e.code == 1

    @patch.dict("os.environ", _UPDATE_PATCHES)
    def test_pip_install_failure_exits(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            # git diff --quiet returns 1 when there ARE new commits
            if cmd and "diff" in cmd and "--quiet" in cmd:
                m.returncode = 1
            # pip install -e . fails
            if cmd and "pip" in cmd and "install" in cmd:
                m.returncode = 1
                m.stderr = "build failed"
            return m

        with _patch_path(), \
             patch("kiro_claw.cli_server.shutil.which", return_value=None), \
             patch("kiro_claw.cli_server.build_frontend_sync"), \
             patch("kiro_claw.cli._ensure_node"), \
             patch("subprocess.run", side_effect=_side_effect):
            try:
                _update()
                assert False, "Expected SystemExit"
            except SystemExit as e:
                assert e.code == 1


class TestCronCli:
    def test_cron_add_with_channel(self, tmp_path):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc"
            mock_job.name = "test"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 300
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="ops",
                message="check",
                every=300,
                cron_expr=None,
                channel="C0AP77JJSN6",
                approval_mode="",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="ops", message="check", every_secs=300,
                channel="C0AP77JJSN6", approval_mode="",
            )

    def test_cron_add_with_cron_expr_and_channel(self, tmp_path):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "def"
            mock_job.name = "daily"
            mock_job.schedule.kind = "cron"
            mock_job.schedule.every_secs = None
            mock_job.schedule.cron_expr = "0 9 * * 1-5"
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="daily",
                message="brief",
                every=None,
                cron_expr="0 9 * * 1-5",
                channel="C0APAPQ5GSY",
                approval_mode="",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="daily", message="brief", cron_expr="0 9 * * 1-5",
                channel="C0APAPQ5GSY", approval_mode="",
            )

    def test_cron_add_with_approval_mode(self, tmp_path):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel") as mock_sel:
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "ghi"
            mock_job.name = "auto-job"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 600
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="auto-job",
                message="run unattended",
                every=600,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="auto-job", message="run unattended", every_secs=600,
                channel=None, approval_mode="auto",
            )
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli", operation="cron.add",
                outcome="allowed", source="cli",
                resources="job_id=ghi approval_mode=auto agent=default silent=False",
            )

    def test_cron_add_with_silent(self):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "mno"
            mock_job.name = "quiet-job"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 300
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="quiet-job",
                message="shh",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
                silent=True,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="quiet-job", message="shh", every_secs=300,
                channel=None, approval_mode="",
            )
            # silent is set via post-create mutation, mirroring agent_id
            assert mock_job.silent is True
            mock_svc._save.assert_called_once()

    def test_cron_update_approval_mode(self, tmp_path):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel") as mock_sel:
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", approval_mode="auto")
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli", operation="cron.update",
                outcome="allowed", source="cli",
                resources="job_id=abc123 fields=approval_mode",
            )

    def test_cron_update_whitespace_channel_skipped(self, tmp_path, capsys):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.update_job.return_value = None
            args = argparse.Namespace(
                cron_action="update",
                job_id="job1",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel="   ",
                approval_mode=None,
            )
            _cron(args)
            out = capsys.readouterr().out
            assert "at least one field" in out

    def test_cron_update_every_and_cron_exclusive(self, tmp_path, capsys):
        with patch("kiro_claw.cli_commands.CronService"):
            args = argparse.Namespace(
                cron_action="update",
                job_id="job1",
                name=None,
                message=None,
                every_secs=300,
                cron_expr="0 9 * * *",
                channel=None,
                approval_mode=None,
            )
            _cron(args)
            out = capsys.readouterr().out
            assert "not both" in out

    def test_cron_update_not_found(self, tmp_path, capsys):
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel") as mock_sel:
            mock_svc = mock_svc_cls.return_value
            mock_svc.update_job.return_value = None
            args = argparse.Namespace(
                cron_action="update",
                job_id="nonexist",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
            )
            _cron(args)
            assert "nonexist" in capsys.readouterr().out
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli", operation="cron.update",
                outcome="not_found", source="cli",
                resources="job_id=nonexist reason=not_found",
            )

    # ── --agent flag on cron add and update ──

    def _make_add_job_mock(self, *, job_id: str = "ag1", every_secs: int | None = 600,
                           cron_expr: str | None = None) -> MagicMock:
        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.name = "test"
        mock_job.schedule.kind = "cron" if cron_expr else "every"
        mock_job.schedule.every_secs = every_secs
        mock_job.schedule.cron_expr = cron_expr
        mock_job.schedule.at_ts = None
        mock_job.agent_id = ""
        return mock_job

    def test_cron_add_with_agent_every(self, tmp_path):
        """--agent on `cron add` with --every sets job.agent_id, persists, and audits."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel") as mock_sel:
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag1", every_secs=600)
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="c360",
                message="check pipeline",
                every=600,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="customer360-code-agent",
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="c360", message="check pipeline", every_secs=600,
                channel=None, approval_mode="",
            )
            assert mock_job.agent_id == "customer360-code-agent"
            mock_svc._save.assert_called_once()
            # Audit log includes agent (permission-relevant: picks
            # which sandboxed subprocess executes the job).
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli", operation="cron.add",
                outcome="allowed", source="cli",
                resources="job_id=ag1 approval_mode=default agent=customer360-code-agent silent=False",
            )

    def test_cron_add_with_agent_cron_expr(self, tmp_path):
        """--agent on `cron add` with --cron sets job.agent_id and persists."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(
                job_id="ag2", every_secs=None, cron_expr="0 9 * * 1-5"
            )
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="briefing",
                message="run briefing",
                every=None,
                cron_expr="0 9 * * 1-5",
                channel=None,
                approval_mode="",
                agent="ea-briefing",
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="briefing", message="run briefing", cron_expr="0 9 * * 1-5",
                channel=None, approval_mode="",
            )
            assert mock_job.agent_id == "ea-briefing"
            mock_svc._save.assert_called_once()

    def test_cron_add_without_agent_does_not_save(self, tmp_path):
        """Empty/omitted --agent leaves job.agent_id untouched, no extra _save."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag3", every_secs=300)
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="basic",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
            )
            _cron(args)
            assert mock_job.agent_id == ""
            mock_svc._save.assert_not_called()

    def test_cron_add_agent_whitespace_stripped(self, tmp_path):
        """Whitespace-only --agent is treated as omitted (no agent_id set)."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag4", every_secs=300)
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="basic",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="   ",
            )
            _cron(args)
            assert mock_job.agent_id == ""
            mock_svc._save.assert_not_called()

    def test_cron_update_with_agent(self, tmp_path):
        """--agent on `cron update` passes agent_id kwarg to update_job."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel") as mock_sel:
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="oncall-agent",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="oncall-agent")
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli", operation="cron.update",
                outcome="allowed", source="cli",
                resources="job_id=abc123 fields=agent_id agent=oncall-agent",
            )

    def test_cron_update_agent_empty_resets(self, tmp_path):
        """--agent '' on update resets agent_id to default (mirrors MCP cron_update)."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="")

    def test_cron_update_agent_omitted_skipped(self, tmp_path, capsys):
        """When --agent is omitted (None), agent_id is not in update_job kwargs."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name="renamed",
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent=None,
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", name="renamed")
            assert "agent_id" not in mock_svc.update_job.call_args.kwargs

    def test_cron_add_invalid_agent_name_rejected(self, tmp_path, capsys):
        """Bad-format --agent on add is rejected with sys.exit(1) before any add_job call."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            args = argparse.Namespace(
                cron_action="add",
                name="bad",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="bad name!",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.add_job.assert_not_called()
            mock_svc._save.assert_not_called()
            assert "invalid agent name" in capsys.readouterr().err.lower()

    def test_cron_update_invalid_agent_name_rejected(self, tmp_path, capsys):
        """Bad-format --agent on update is rejected with sys.exit(1) before any update_job call."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="bad name!",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.update_job.assert_not_called()
            assert "invalid agent name" in capsys.readouterr().err.lower()

    def test_cron_update_agent_whitespace_stripped(self, tmp_path):
        """Whitespace around --agent on update is stripped before forwarding to update_job."""
        with patch("kiro_claw.cli_commands.CronService") as mock_svc_cls, \
             patch("kiro_claw.cli_commands.sel"):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="  oncall-agent  ",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="oncall-agent")

    def test_cli_argparse_cron_add_agent_flag(self) -> None:
        """`kiroclaw cron add ... --agent NAME` parses into args.agent."""
        import sys

        argv = [
            "kiroclaw", "cron", "add", "daily-briefing",
            "Run my morning briefing",
            "--cron", "0 9 * * 1-5",
            "--agent", "ea-briefing",
        ]
        with patch.object(sys, "argv", argv), \
             patch("kiro_claw.cli._cron") as mock_cron:
            from kiro_claw.cli import main

            main()
            mock_cron.assert_called_once()
            ns = mock_cron.call_args[0][0]
            assert ns.cron_action == "add"
            assert ns.name == "daily-briefing"
            assert ns.agent == "ea-briefing"

    def test_cli_argparse_cron_add_no_agent_default_empty(self) -> None:
        """Omitting --agent on `cron add` leaves args.agent as empty string."""
        import sys

        argv = [
            "kiroclaw", "cron", "add", "basic", "hello",
            "--every", "300",
        ]
        with patch.object(sys, "argv", argv), \
             patch("kiro_claw.cli._cron") as mock_cron:
            from kiro_claw.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.agent == ""

    def test_cli_argparse_cron_update_agent_flag(self) -> None:
        """`kiroclaw cron update <id> --agent NAME` parses into args.agent."""
        import sys

        argv = [
            "kiroclaw", "cron", "update", "abc123",
            "--agent", "oncall-agent",
        ]
        with patch.object(sys, "argv", argv), \
             patch("kiro_claw.cli._cron") as mock_cron:
            from kiro_claw.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.cron_action == "update"
            assert ns.job_id == "abc123"
            assert ns.agent == "oncall-agent"

    def test_cli_argparse_cron_update_no_agent_default_none(self) -> None:
        """Omitting --agent on `cron update` leaves args.agent as None (skip)."""
        import sys

        argv = [
            "kiroclaw", "cron", "update", "abc123",
            "--name", "renamed",
        ]
        with patch.object(sys, "argv", argv), \
             patch("kiro_claw.cli._cron") as mock_cron:
            from kiro_claw.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.agent is None


class TestSetupTimezone:
    def test_auto_detect_from_tz_env(self, monkeypatch):
        """TZ env var is checked before /etc/localtime."""
        from kiro_claw.cli_setup import _detect_system_timezone

        monkeypatch.setenv("TZ", "Europe/London")
        assert _detect_system_timezone() == "Europe/London"

    def test_auto_detect_tz_env_with_colon(self, monkeypatch):
        """TZ env var with glibc colon prefix is handled."""
        from kiro_claw.cli_setup import _detect_system_timezone

        monkeypatch.setenv("TZ", ":America/Chicago")
        assert _detect_system_timezone() == "America/Chicago"

    def test_auto_detect_from_symlink(self, tmp_path, monkeypatch):
        """When /etc/localtime is a symlink, timezone is auto-detected."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="") as mock_input:
            with patch(
                "kiro_claw.cli_setup._detect_system_timezone",
                return_value="America/Los_Angeles",
            ):
                _setup_timezone()

        prompt = mock_input.call_args[0][0]
        assert "America/Los_Angeles" in prompt
        data = json.loads(cfg_file.read_text())
        assert data["timezone"] == "America/Los_Angeles"

    def test_manual_entry(self, tmp_path, monkeypatch):
        """When no auto-detect, user types timezone manually."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="America/New_York"):
            with patch("kiro_claw.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text())
        assert data["timezone"] == "America/New_York"

    def test_skip_on_empty_input(self, tmp_path, monkeypatch):
        """Empty input skips timezone setup."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        with patch("builtins.input", return_value=""):
            with patch("kiro_claw.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text())
        assert "timezone" not in data

    def test_invalid_timezone_rejected(self, tmp_path, monkeypatch, capsys):
        """Invalid timezone is rejected, not saved."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="Invalid/Timezone"):
            with patch("kiro_claw.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text())
        assert "timezone" not in data
        output = capsys.readouterr().out
        assert "Unknown timezone" in output

    def test_keeps_existing_on_enter(self, tmp_path, monkeypatch):
        """Re-running setup with existing timezone keeps it on Enter."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"timezone": "America/Chicago"}))
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        with patch("builtins.input", return_value=""):
            _setup_timezone()

        data = json.loads(cfg_file.read_text())
        assert data["timezone"] == "America/Chicago"

    def test_corrupted_config_not_overwritten(self, tmp_path, monkeypatch, capsys):
        """Corrupted config file is not overwritten."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json {{{")
        monkeypatch.setattr("kiro_claw.cli_setup.config_path", lambda: cfg_file)

        from kiro_claw.cli_setup import _setup_timezone

        _setup_timezone()

        # File should be unchanged
        assert cfg_file.read_text() == "not json {{{"
        output = capsys.readouterr().out
        assert "Could not read" in output


class TestGetAlias:
    """Tests for _get_alias."""

    def test_returns_user_env(self, monkeypatch):
        monkeypatch.setenv("USER", "testuser")
        from kiro_claw.cli_setup import _get_alias

        assert _get_alias() == "testuser"

    def test_falls_back_to_getlogin(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with patch("os.getlogin", return_value="loginuser"):
            from kiro_claw.cli_setup import _get_alias

            assert _get_alias() == "loginuser"

    def test_falls_back_to_prompt(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with (
            patch("os.getlogin", side_effect=OSError("no tty")),
            patch("builtins.input", return_value="prompted"),
        ):
            from kiro_claw.cli_setup import _get_alias

            assert _get_alias() == "prompted"

    def test_exits_when_no_alias(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with (
            patch("os.getlogin", side_effect=OSError("no tty")),
            patch("builtins.input", return_value=""),
        ):
            from kiro_claw.cli_setup import _get_alias

            try:
                _get_alias()
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1


class TestManifest:
    """Tests for _manifest."""

    def _patch_template(
        self, content="name: KiroClaw-{{ALIAS}}\ndisplay_name: KiroClaw-{{ALIAS}}\n"
    ):
        """Patch importlib.resources.files to return a fake template."""
        mock_resource = MagicMock()
        mock_resource.joinpath.return_value.read_text.return_value = content
        return patch("kiro_claw.cli_setup._pkg_files", return_value=mock_resource)

    def test_renders_alias_to_stdout(self, capsys):
        with self._patch_template():
            from kiro_claw.cli_setup import _manifest

            _manifest(alias="alice")
        out = capsys.readouterr().out
        assert "KiroClaw-alice" in out
        assert "{{ALIAS}}" not in out

    def test_writes_to_output_file(self, tmp_path):
        out_file = tmp_path / "sub" / "out.yaml"
        with self._patch_template("name: KiroClaw-{{ALIAS}}\n"):
            from kiro_claw.cli_setup import _manifest

            _manifest(alias="bob", output=str(out_file))
        assert out_file.exists()
        assert "KiroClaw-bob" in out_file.read_text()

    def test_creates_parent_dirs(self, tmp_path):
        out_file = tmp_path / "deep" / "nested" / "out.yaml"
        with self._patch_template("name: KiroClaw-{{ALIAS}}\n"):
            from kiro_claw.cli_setup import _manifest

            _manifest(alias="carol", output=str(out_file))
        assert out_file.exists()

    def test_exits_when_template_missing(self):
        mock_resource = MagicMock()
        mock_resource.joinpath.return_value.read_text.side_effect = FileNotFoundError
        with patch("kiro_claw.cli_setup._pkg_files", return_value=mock_resource):
            from kiro_claw.cli_setup import _manifest

            try:
                _manifest(alias="dave")
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_rejects_invalid_alias(self):
        from kiro_claw.cli_setup import _manifest

        for bad in ["a\nb", "foo:bar", "x{{y}}", "hello world"]:
            try:
                _manifest(alias=bad)
                assert False, f"should have exited for alias={bad!r}"
            except SystemExit as e:
                assert e.code == 1

    def test_url_flag_prints_creation_link(self, capsys):
        with self._patch_template("# comment\nname: KiroClaw-{{ALIAS}}\n"):
            from kiro_claw.cli_setup import _manifest

            _manifest(alias="alice", url=True)
        out = capsys.readouterr().out
        assert "https://api.slack.com/apps?new_app=1&manifest_yaml=" in out
        assert "KiroClaw-alice" in out  # alias substituted
        assert "%0A" in out  # newlines are URL-encoded
        assert "\nname:" not in out  # raw YAML not printed
        assert "%23" not in out  # comments stripped from URL


class TestLogout:
    """Tests for _logout CLI function."""

    def test_logout_success(self, tmp_path, monkeypatch):
        """Successful logout prints success message."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            _logout(8765)  # Should not raise

    def test_logout_gateway_not_running(self, tmp_path, monkeypatch):
        """Missing secret file means gateway not running."""
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        try:
            _logout(8765)
            assert False, "should have exited"
        except SystemExit as e:
            assert e.code == 1

    def test_logout_http_error(self, tmp_path, monkeypatch):
        """HTTP error from gateway is handled."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(None, 403, "Forbidden", {}, None),
        ):
            try:
                _logout(8765)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_connection_error(self, tmp_path, monkeypatch):
        """Connection error means gateway not running."""
        secret_file = tmp_path / ".kiroclaw" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            try:
                _logout(8765)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_error_response(self, tmp_path, monkeypatch):
        """Error response from gateway is handled."""
        secret_file = tmp_path / ".kiroclaw" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": false, "error": "test error"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            try:
                _logout(8765)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1


class TestStatus:
    """Tests for _status() HTTP error handling."""

    def _make_args(self, port=8765):
        return argparse.Namespace(port=port)

    def test_status_auth_required(self, capsys):
        """401/403 should report gateway as running with token auth."""
        from kiro_claw.cli_server import _status

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:8765/api/status", 403, "Forbidden", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "token auth" in out

    def test_status_other_http_error(self, capsys):
        """Non-auth HTTP errors should report gateway as running with code."""
        from kiro_claw.cli_server import _status

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:8765/api/status", 500, "Internal Server Error", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "HTTP 500" in out

    def test_status_connection_refused(self, capsys):
        """Connection refused should report gateway as not running."""
        from kiro_claw.cli_server import _status

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "not running" in out

    def test_status_success(self, capsys):
        """200 OK should display stats."""
        from kiro_claw.cli_server import _status

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"uptime": "1h 0m", "sessions": 2, "messages": 10,
             "tool_calls": 5, "subagents": 0, "crons": 1, "lessons": 3}
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "1h 0m" in out
        assert "Sessions" in out or "sessions" in out.lower()

    def test_status_unexpected_exception(self, capsys):
        """Non-network exceptions should report gateway as running with unexpected response."""
        from kiro_claw.cli_server import _status

        with patch("urllib.request.urlopen", side_effect=RuntimeError("unexpected")):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "unexpected response" in out


class TestIsKiroclawProcess:
    """Tests for _is_kiroclaw_process helper."""

    def test_returns_true_for_kiroclaw(self):
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", return_value="python3 -m kiro_claw.dashboard\n"):
            assert _is_kiroclaw_process(1234) is True

    def test_returns_true_for_kiroclaw_binary(self):
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", return_value="/usr/bin/kiroclaw start\n"):
            assert _is_kiroclaw_process(1234) is True

    def test_returns_false_for_unrelated(self):
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", return_value="nginx: worker process\n"):
            assert _is_kiroclaw_process(1234) is False

    def test_returns_false_for_broad_match(self):
        """Editing a kiroclaw file should NOT match — only gateway entry points."""
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", return_value="vim /tmp/kiroclaw-notes.txt\n"):
            assert _is_kiroclaw_process(1234) is False

    def test_returns_false_on_process_exit(self):
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "ps")):
            assert _is_kiroclaw_process(1234) is False

    def test_raises_on_missing_ps(self):
        from kiro_claw.cli_server import _is_kiroclaw_process

        with patch("subprocess.check_output", side_effect=FileNotFoundError):
            with pytest.raises(FileNotFoundError):
                _is_kiroclaw_process(1234)


class TestStop:
    """Tests for _stop CLI function."""

    def _mock_sel(self):
        mock = MagicMock()
        return patch("kiro_claw.cli_commands.sel", return_value=mock)

    @pytest.fixture(autouse=True)
    def _no_service(self):
        # ``_stop`` short-circuits via ``service_controller.stop_service()``
        # when a systemd/launchd service is active on the host. Force the
        # SIGTERM-by-port path so tests don't flake based on whether the
        # test host happens to have ``kiroclaw.service`` installed.
        with patch(
            "kiro_claw.cli_server.service_controller.stop_service", return_value=False
        ):
            yield

    def test_lsof_not_found(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=FileNotFoundError
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "lsof" in capsys.readouterr().out

    def test_no_process_on_port(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "lsof")
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "No KiroClaw gateway" in capsys.readouterr().out

    def test_no_kiroclaw_process(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n",  # lsof returns a PID
                "nginx: worker\n",  # ps shows non-kiroclaw
            ]
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "No KiroClaw gateway" in capsys.readouterr().out

    def test_ps_not_found(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n",  # lsof returns a PID
                FileNotFoundError,  # ps not found
            ]
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "ps" in capsys.readouterr().out

    def test_successful_stop(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n",  # lsof
                "python3 -m kiro_claw.dashboard\n",  # ps
            ]
        ), patch("os.kill"), patch("time.sleep"):
            _stop(8765)
        assert "SIGTERM" in capsys.readouterr().out

    def test_permission_denied(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n",
                "python3 -m kiro_claw.dashboard\n",
            ]
        ), patch("os.kill", side_effect=PermissionError):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "No permission" in capsys.readouterr().out

    def test_process_already_exited(self, capsys):
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n",
                "python3 -m kiro_claw.dashboard\n",
            ]
        ), patch("os.kill", side_effect=ProcessLookupError):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        assert "already exited" in capsys.readouterr().out

    def test_partial_permission_denied(self, capsys):
        """One PID succeeds, another is denied — reports both."""
        from kiro_claw.cli_server import _stop

        def kill_side_effect(pid, sig):
            if pid == 5678:
                raise PermissionError

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\n5678\n",
                "python3 -m kiro_claw.dashboard\n",  # ps for 1234
                "python3 -m kiro_claw.dashboard\n",  # ps for 5678
            ]
        ), patch("os.kill", side_effect=kill_side_effect), patch("time.sleep"):
            with pytest.raises(SystemExit) as exc:
                _stop(8765)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "SIGTERM" in out
        assert "No permission" in out

    def test_lsof_with_warnings(self, capsys):
        """lsof sometimes emits warnings mixed with PIDs — non-digit lines are filtered."""
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "subprocess.check_output", side_effect=[
                "1234\nlsof: WARNING: can't stat() ...\n",
                "python3 -m kiro_claw.dashboard\n",
            ]
        ), patch("os.kill"), patch("time.sleep"):
            _stop(8765)
        assert "SIGTERM" in capsys.readouterr().out

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When --port is passed explicitly (cli_port is not None), the
        # systemd/launchd service short-circuit must be bypassed so the
        # SIGTERM-by-port path can target a non-default dev gateway.
        from kiro_claw.cli_server import _stop

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.stop_service",
            return_value=True,
        ) as mock_stop_service, patch(
            "subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "lsof")
        ):
            with pytest.raises(SystemExit):
                _stop(8089)
        # Service short-circuit must NOT have been called.
        mock_stop_service.assert_not_called()
        # And we should have fallen through to the SIGTERM path
        # (which exits 1 here because lsof finds nothing on 8089).
        assert "No KiroClaw gateway" in capsys.readouterr().out


class TestRestart:
    """Tests for the service-aware ``_restart`` CLI function.

    Mirrors :class:`TestStop` — restart re-uses the same service-detection
    plumbing, so we drive the same ``service_controller`` boundary with
    fakes and assert the two branches:

    1. service active → controller handles it, no SIGTERM/spawn
    2. no service → SIGTERM via ``_stop`` if a foreground gateway is
       listening, then detach a fresh gateway via Popen
    """

    def _mock_sel(self):
        return patch("kiro_claw.cli_server.sel", return_value=MagicMock())

    def test_service_active_restarts_via_controller(self, capsys):
        from kiro_claw.cli_server import _restart

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.restart_service",
            return_value=True,
        ) as mock_restart, patch(
            "kiro_claw.cli_server._spawn_detached_gateway"
        ) as mock_spawn, patch(
            "subprocess.check_output"
        ) as mock_lsof:
            _restart(None)
        mock_restart.assert_called_once()
        # Service path must NOT also spawn — that would race the supervisor.
        mock_spawn.assert_not_called()
        # And must not poke at lsof at all (no point — the supervisor owns the lifecycle).
        mock_lsof.assert_not_called()
        assert "Restarted" in capsys.readouterr().out

    def test_no_service_no_running_gateway_spawns_fresh(self, capsys):
        # Restart should be tolerant of a crashed gateway: if the user runs
        # ``kiroclaw restart`` after the gateway died, they should still
        # end up with a running gateway, not an error.
        from kiro_claw.cli_server import _restart

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.restart_service",
            return_value=False,
        ), patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "lsof"),
        ), patch(
            "kiro_claw.cli_server._spawn_detached_gateway", return_value=4321
        ) as mock_spawn, patch(
            "kiro_claw.cli_server._stop"
        ) as mock_stop:
            _restart(None)
        mock_stop.assert_not_called()
        mock_spawn.assert_called_once()
        out = capsys.readouterr().out
        assert "4321" in out
        assert "detached" in out.lower()

    def test_no_service_with_running_gateway_stops_then_spawns(self, capsys):
        from kiro_claw.cli_server import _restart

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.restart_service",
            return_value=False,
        ), patch(
            "subprocess.check_output", return_value="1234\n"
        ), patch(
            "kiro_claw.cli_server._stop"
        ) as mock_stop, patch(
            "kiro_claw.cli_server._spawn_detached_gateway", return_value=5678
        ) as mock_spawn:
            _restart(None)
        # Order matters: stop first, then spawn — otherwise the new
        # gateway would race the old one for the port and lose.
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "5678" in capsys.readouterr().out

    def test_toctou_stop_systemexit_is_swallowed_so_spawn_proceeds(self, capsys):
        # AutoSDE finding on rev 1: lsof can show a listener, then the
        # gateway exits before _stop() runs. _stop() then finds nothing
        # and calls sys.exit(1). For restart, that's the wrong behavior:
        # the user asked for a restart, not a stop, and an exit here would
        # leave them with no running gateway at all. Verify we swallow
        # SystemExit and still spawn the replacement.
        from kiro_claw.cli_server import _restart

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.restart_service",
            return_value=False,
        ), patch(
            "subprocess.check_output", return_value="1234\n"
        ), patch(
            "kiro_claw.cli_server._stop", side_effect=SystemExit(1)
        ) as mock_stop, patch(
            "kiro_claw.cli_server._spawn_detached_gateway", return_value=9999
        ) as mock_spawn:
            _restart(None)
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "9999" in capsys.readouterr().out

    def test_spawn_detached_gateway_uses_kiroclaw_bin(self, tmp_path, monkeypatch):
        # When ``kiroclaw`` is on PATH, the detached child must invoke it
        # directly (not via ``python -m``). This exercises the production
        # path on installed hosts.
        from kiro_claw.cli_server import _spawn_detached_gateway

        monkeypatch.setattr(
            "kiro_claw.cli_server.config_dir", lambda: tmp_path
        )
        proc = MagicMock(pid=9999)
        with patch(
            "shutil.which", return_value="/usr/local/bin/kiroclaw"
        ), patch(
            "kiro_claw.cli_server.subprocess.Popen", return_value=proc
        ) as mock_popen:
            pid = _spawn_detached_gateway()
        assert pid == 9999
        argv = mock_popen.call_args.args[0]
        assert argv == ["/usr/local/bin/kiroclaw", "gateway"]
        # Must detach from the controlling terminal — otherwise the
        # detached process would die when the calling shell exits.
        assert mock_popen.call_args.kwargs["start_new_session"] is True
        # Must not inherit stdin from the parent — otherwise reading from
        # a detached terminal would block the new gateway.
        assert mock_popen.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_spawn_detached_gateway_falls_back_to_python_m(self, tmp_path, monkeypatch):
        # Dev/Brazil-workspace installs may not have ``kiroclaw`` on
        # PATH globally. Fall back to ``python -m kiro_claw`` so the
        # command works regardless of install layout.
        from kiro_claw.cli_server import _spawn_detached_gateway

        monkeypatch.setattr(
            "kiro_claw.cli_server.config_dir", lambda: tmp_path
        )
        proc = MagicMock(pid=8888)
        with patch("shutil.which", return_value=None), patch(
            "kiro_claw.cli_server.subprocess.Popen", return_value=proc
        ) as mock_popen:
            _spawn_detached_gateway()
        argv = mock_popen.call_args.args[0]
        # First arg is sys.executable (path to current Python). Just check
        # the invocation form, not the absolute path.
        assert argv[1:] == ["-m", "kiro_claw", "gateway"]

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When cli_port is not None, bypass systemd: the service unit is not
        # bound to a specific port, so short-circuiting through it would
        # target the wrong gateway.
        from kiro_claw.cli_server import _restart

        with self._mock_sel(), patch(
            "kiro_claw.cli_server.service_controller.restart_service",
            return_value=True,
        ) as mock_restart_service, patch(
            "kiro_claw.cli_server._spawn_detached_gateway", return_value=4321
        ) as mock_spawn, patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "lsof"),
        ):
            _restart(8089)
        # Service short-circuit must NOT have been called.
        mock_restart_service.assert_not_called()
        # And we should have fallen through to the spawn path.
        mock_spawn.assert_called_once()
        assert "Started detached gateway" in capsys.readouterr().out


class TestResolveClientPort:
    """Tests for `resolve_client_port` — the port-resolution order used by
    `kiroclaw token` / `status` / `logout` / `stop` to find the gateway.

    Resolution order (see cli.resolve_client_port):
      1. explicit --port CLI arg (cli_port != None)
      2. KIROCLAW_PORT env var
      3. port parsed from dashboard.url in config
      4. default 8765
    """

    def test_cli_flag_wins(self, monkeypatch, tmp_path):
        """An explicit --port flag must override env and config."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCLAW_PORT", "9999")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            assert resolve_client_port(12345) == 12345

    def test_env_var_used_when_no_cli(self, monkeypatch):
        """KIROCLAW_PORT env var wins over config when no --port passed."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCLAW_PORT", "6777")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 6777

    def test_invalid_env_var_falls_through_to_config(self, monkeypatch):
        """A garbage KIROCLAW_PORT must not crash; the helper falls through."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCLAW_PORT", "not-a-number")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_used_when_no_cli_no_env(self, monkeypatch):
        """The port in dashboard.url must be honoured when env is unset."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCLAW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_hostname_only_falls_through_to_default(self, monkeypatch):
        """A dashboard.url without an explicit port must fall through to 8765."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCLAW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://my.host.example"
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            # parse_dashboard_url returns _DEFAULT_PORT when no port in URL,
            # which is the same as the final fallback — either way we land on 8765.
            assert resolve_client_port(None) == 8765

    def test_empty_config_falls_through_to_default(self, monkeypatch):
        """No env, empty dashboard.url → 8765."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCLAW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = ""
        with patch("kiro_claw.cli_server.KiroClawConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 8765

    def test_config_load_failure_falls_through_to_default(self, monkeypatch):
        """If config loading raises, the helper must still return a usable port."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCLAW_PORT", raising=False)
        with patch("kiro_claw.cli_server.KiroClawConfig.load", side_effect=RuntimeError("boom")):
            assert resolve_client_port(None) == 8765

    def test_cli_flag_zero_is_respected(self, monkeypatch):
        """Port 0 is weird but valid; it must not be coerced to None/default."""
        from kiro_claw.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCLAW_PORT", "9999")
        # cli_port=0 is explicit; the helper uses 'is not None' not truthiness.
        assert resolve_client_port(0) == 0


class TestEnsurePrerequisites:
    """Tests for _ensure_prerequisites return value."""

    def test_returns_true_when_all_satisfied(self):
        from kiro_claw.cli_setup import _ensure_prerequisites

        with (
            patch("kiro_claw.cli_setup.shutil.which", return_value="/usr/bin/kiro-cli"),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            assert _ensure_prerequisites() is True

    def test_returns_true_when_optional_kiro_absent(self):
        """kiro-cli's absence must not block setup.

        _ensure_prerequisites only prints guidance for missing tooling (it does
        no installs and imposes no login prerequisite) and always returns True so
        setup proceeds even when the kiro-cli backend is not yet on PATH.
        """
        from kiro_claw.cli_setup import _ensure_prerequisites

        with patch("kiro_claw.cli_setup.shutil.which", return_value=None):
            assert _ensure_prerequisites() is True


class TestDoctorStaleProjectDir:
    """Tests for doctor stale project_dir detection."""

    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    def test_doctor_detects_stale_project_dir(self, tmp_path, capsys):
        proj_file = tmp_path / "project_dir"
        proj_file.write_text("/nonexistent/deleted\n")
        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen"),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch.dict("os.environ", {"KIROCLAW_PROJECT_DIR": "", "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": ""}, clear=False),
        ):
            with pytest.raises(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "stale" in out
        assert "project dir: ⚠️  not set" not in out  # should NOT show fallback message


class TestDoctorMcpTools:
    """Tests for the `_doctor_mcp_tools` helper — the MCP section of doctor.

    The helper live-probes only the managed servers (`kiroclaw-core`,
    `kiroclaw-cron`) via `probe_server`; tests monkey-patch that call so
    no child processes are spawned.
    """

    def _mock_probe(self, results: dict[str, tuple[str, list[str], str]]):
        """Return a patch target for `probe_server` that yields per-name
        results. `results[name] = (status, tools, error)`."""
        from kiro_claw.mcp_discovery import McpServerInfo

        async def fake(target: McpServerInfo) -> McpServerInfo:
            status, tools, error = results.get(target.name, ("ok", [], ""))
            target.status = status
            target.tools = list(tools)
            target.error = error
            return target

        return patch("kiro_claw.cli_doctor.probe_server", side_effect=fake)

    def test_success_shows_tool_counts(self, tmp_path, capsys):
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=["@kiroclaw-core", "@kiroclaw-cron"],
            allowed=["@kiroclaw-core", "@kiroclaw-cron"],
            servers={
                "kiroclaw-core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with self._mock_probe(
            {
                "kiroclaw-core": ("ok", ["spawn_run", "learn_add", "task_run"], ""),
                "kiroclaw-cron": ("ok", ["cron_add"], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kiroclaw-core: ✅ 3 tools" in out
        assert "@kiroclaw-cron: ✅ 1 tool" in out
        assert issues == []

    def test_failure_shows_error_head_and_indented_stderr(self, tmp_path, capsys):
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=["@kiroclaw-core", "@kiroclaw-cron"],
            allowed=["@kiroclaw-core", "@kiroclaw-cron"],
            servers={
                "kiroclaw-core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        fail_err = (
            "no response\n"
            "stderr: Directory isn't within a workspace: '/home/u/.kiroclaw-app' "
            "(Amazon::Brazil::Cli::FindupException)"
        )
        with self._mock_probe(
            {
                "kiroclaw-core": ("error", [], fail_err),
                "kiroclaw-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        # First line of error becomes the head; subsequent lines indent.
        assert "@kiroclaw-core: ❌ no response" in out
        assert "      stderr: Directory isn't within a workspace" in out
        assert "FindupException" in out
        assert "@kiroclaw-cron: ✅ 0 tools" in out
        assert "@kiroclaw-core probe" in issues
        # Healthy server must not pollute the issue list.
        assert "@kiroclaw-cron probe" not in issues

    def test_missing_mcp_server_cannot_auto_fix(self, tmp_path, capsys):
        """A missing `mcpServers` entry is install-specific; doctor reports
        the user needs to re-run setup and does not attempt to probe."""
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={},
        )
        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kiroclaw-core: ❌ missing from mcpServers" in out
        assert "@kiroclaw-cron: ❌ missing from mcpServers" in out
        assert "re-run `kiroclaw setup`" in out
        assert "@kiroclaw-core config" in issues
        assert "@kiroclaw-cron config" in issues
        probe_mock.assert_not_called()

    def test_auto_fix_adds_missing_tools_and_allowed(self, tmp_path, capsys):
        """Missing `tools` / `allowedTools` entries are added to the agent
        config and persisted in-place."""
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={
                "kiroclaw-core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with self._mock_probe(
            {
                "kiroclaw-core": ("ok", [], ""),
                "kiroclaw-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "Auto-fixed agent config" in out
        updated = json.loads(agent_path.read_text())
        assert updated["tools"] == ["@kiroclaw-cron", "@kiroclaw-core"]
        assert updated["allowedTools"] == ["@kiroclaw-cron", "@kiroclaw-core"]

    def test_probe_exception_does_not_crash(self, tmp_path, capsys):
        """If `probe_server` itself raises (e.g. event-loop oddity), doctor
        prints a warning and returns cleanly instead of propagating."""
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=["@kiroclaw-core", "@kiroclaw-cron"],
            allowed=["@kiroclaw-core", "@kiroclaw-cron"],
            servers={
                "kiroclaw-core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with patch(
            "kiro_claw.cli_doctor.probe_server",
            side_effect=RuntimeError("asyncio is on fire"),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "probe failed: asyncio is on fire" in out

    def test_only_managed_servers_are_probed(self, tmp_path, capsys):
        """Third-party MCPs in the agent config must not be probed — this
        keeps doctor output focused on KiroClaw's own servers and avoids
        false negatives for optional MCPs."""
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        _write_agent_config(
            agent_path,
            tools=["@kiroclaw-core", "@kiroclaw-cron", "@builder-mcp"],
            allowed=["@kiroclaw-core", "@kiroclaw-cron", "@builder-mcp"],
            servers={
                "kiroclaw-core": {"command": "/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/bin/kiroclaw", "args": ["mcp-cron"]},
                "builder-mcp": {"command": "/bin/builder-mcp"},
            },
        )
        issues: list[str] = []
        probed_names: list[str] = []

        async def recording_probe(target):
            probed_names.append(target.name)
            target.status = "ok"
            target.tools = []
            return target

        with patch("kiro_claw.cli_doctor.probe_server", side_effect=recording_probe):
            _doctor_mcp_tools(agent_path, issues)
        assert probed_names == ["kiroclaw-cron", "kiroclaw-core"]
        out = capsys.readouterr().out
        assert "@builder-mcp" not in out

    def test_malformed_agent_config_does_not_crash(self, tmp_path, capsys):
        """If kiroclaw.json is truncated or otherwise unparseable, doctor
        must fall back to an empty config and surface missing-server
        errors cleanly rather than raising out of the MCP section."""
        from kiro_claw.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kiroclaw.json"
        # Truncated mid-write, half-written JSON, totally broken content —
        # the exact failure mode the atomic_write change is meant to
        # prevent from ever landing on disk, but we still need doctor to
        # cope if it encounters one (legacy installs, disk corruption).
        agent_path.write_text("{\"tools\": [\"@kiroclaw-c")

        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)

        out = capsys.readouterr().out
        # Empty config → both managed servers report missing from mcpServers.
        assert "@kiroclaw-core: ❌ missing from mcpServers" in out
        assert "@kiroclaw-cron: ❌ missing from mcpServers" in out
        # No probe attempted since no server spec survived the parse failure.
        probe_mock.assert_not_called()


class TestDoctorStt:
    """Tests for doctor Speech-to-Text section."""

    def test_doctor_stt_enabled_all_found(self, tmp_path, capsys):
        from kiro_claw.config.loader import KiroClawConfig, SttConfig

        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroClawConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="whisper")
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor._find_whisper", return_value="/usr/local/bin/whisper"),
            patch("kiro_claw.cli_doctor.ensure_ffmpeg_in_path"),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load", return_value=cfg),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "provider:    ✅ whisper" in out
        assert "whisper:     ✅" in out
        assert "ffmpeg:      ✅" in out

    def test_doctor_stt_disabled(self, tmp_path, capsys):
        from kiro_claw.config.loader import KiroClawConfig, SttConfig

        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroClawConfig.load()
        cfg.stt = SttConfig(enabled=False)
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load", return_value=cfg),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor._find_whisper", return_value=None),
            patch("kiro_claw.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "disabled" in out
        assert "not needed" in out

    def test_doctor_stt_transcribe_provider(self, tmp_path, capsys):
        from kiro_claw.config.loader import KiroClawConfig, SttConfig

        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroClawConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        fake_modules = {
            "amazon_transcribe": MagicMock(),
            "amazon_transcribe.client": MagicMock(),
        }
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load", return_value=cfg),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor._find_whisper", return_value=None),
            patch("kiro_claw.cli_doctor.ensure_ffmpeg_in_path"),
            patch.dict("sys.modules", fake_modules),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "transcribe" in out
        assert "whisper:     ⏭" in out
        assert "ffmpeg:      ✅" in out
        # Happy-path deps should report ✅ — guards against a regression
        # where the emission is silently dropped. (Cloud STT is optional now;
        # the AWS region is no longer printed on a public install.)
        assert "transcribe:  ✅" in out
        assert "boto3:       ✅" in out

    def test_doctor_stt_transcribe_amazon_transcribe_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and amazon_transcribe is not importable,
        doctor reports it as an OPTIONAL gap (public pip extra) and does NOT
        treat it as a hard failure."""
        from kiro_claw.config.loader import KiroClawConfig, SttConfig

        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroClawConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        # Force `import amazon_transcribe.client` inside _doctor() to raise
        # ImportError even though the package is already loaded at test time.
        # setitem(sys.modules, ..., None) is the documented hook for this.
        monkeypatch.setitem(sys.modules, "amazon_transcribe.client", None)
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load", return_value=cfg),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor._find_whisper", return_value=None),
            patch("kiro_claw.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            # Optional cloud STT missing is NOT a hard failure — _doctor may
            # still sys.exit on unrelated env checks, so tolerate either.
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "transcribe:  ⏹ optional cloud STT not installed" in out
        assert "pip install 'kiro-claw[voice]'" in out

    def test_doctor_stt_transcribe_boto3_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and boto3 is not importable, doctor
        reports it as an OPTIONAL gap (public pip extra), not a hard failure."""
        from kiro_claw.config.loader import KiroClawConfig, SttConfig

        agent_file = tmp_path / "kiroclaw.json"
        agent_data = {
            "tools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "allowedTools": ["@kiroclaw-core", "@kiroclaw-cron"],
            "mcpServers": {
                "kiroclaw-core": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-core"]},
                "kiroclaw-cron": {"command": "/usr/local/bin/kiroclaw", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroClawConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        # amazon_transcribe importable (isolate the boto3 gap), boto3 missing.
        monkeypatch.setitem(sys.modules, "amazon_transcribe", MagicMock())
        monkeypatch.setitem(sys.modules, "amazon_transcribe.client", MagicMock())
        # Force `import boto3` inside _doctor() to raise ImportError.
        monkeypatch.setitem(sys.modules, "boto3", None)
        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=lambda b: f"/usr/local/bin/{b}"),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.KiroClawConfig.load", return_value=cfg),
            patch("kiro_claw.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_claw.cli_doctor._find_whisper", return_value=None),
            patch("kiro_claw.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            # Optional AWS SDK missing is NOT a hard failure — tolerate either
            # a clean return or an unrelated env-driven sys.exit.
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "boto3:       ⏹ optional AWS SDK not installed" in out
        assert "pip install 'kiro-claw[aws]'" in out


class TestConfigDirOverride:
    """Tests that CLI functions respect KIROCLAW_HOME env var via config_dir()."""

    def test_project_dir_file_uses_config_dir(self, tmp_path, monkeypatch):
        """_project_dir_file() returns path under config_dir(), not hardcoded home."""
        monkeypatch.setattr("kiro_claw.cli.config_dir", lambda: tmp_path)

        from kiro_claw.cli import _project_dir_file

        assert _project_dir_file() == tmp_path / "project_dir"

    def test_detect_project_dir_reads_from_config_dir(self, tmp_path, monkeypatch):
        """_detect_project_dir reads saved path from config_dir()/project_dir."""
        proj = tmp_path / "my_project"
        proj.mkdir()
        (proj / "skills").mkdir()
        (proj / "src" / "kiro_claw").mkdir(parents=True)

        config_home = tmp_path / "custom_config"
        config_home.mkdir()
        (config_home / "project_dir").write_text(str(proj) + "\n")

        monkeypatch.setattr("kiro_claw.cli.config_dir", lambda: config_home)
        monkeypatch.chdir(tmp_path)  # CWD has no project markers

        from kiro_claw.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj)

    def test_detect_project_dir_no_agents_dir(self, tmp_path, monkeypatch):
        """Detection works without a project-level agents/ dir (removed in bbbc1f6e).

        Regression guard: agent config was consolidated into src/kiro_claw/config/
        and the root agents/ dir deleted, which silently broke detection (and the
        dashboard changelog) while the marker still required agents/ + skills/.
        """
        proj = tmp_path / "KiroClaw"
        (proj / "skills").mkdir(parents=True)
        (proj / "src" / "kiro_claw").mkdir(parents=True)
        assert not (proj / "agents").exists()

        monkeypatch.setattr("kiro_claw.cli.config_dir", lambda: tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        monkeypatch.chdir(proj)

        from kiro_claw.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj.resolve())

    def test_logout_reads_secret_from_config_dir(self, tmp_path, monkeypatch):
        """_logout reads .local_secret from config_dir(), not ~/.kiroclaw."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)

        from kiro_claw.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            _logout(8765)

    def test_setup_slack_tokens_writes_to_config_dir(self, tmp_path, monkeypatch):
        """_setup_slack_tokens writes .env to config_dir(), not ~/.kiroclaw."""
        monkeypatch.setattr("kiro_claw.cli_setup.env_path", lambda: tmp_path / ".env")

        from kiro_claw.cli_setup import _setup_slack_tokens

        # Simulate user providing all tokens
        inputs = iter(["y", "xapp-test", "xoxb-test", "U12345"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        _setup_slack_tokens()
        assert (tmp_path / ".env").exists()
        content = (tmp_path / ".env").read_text()
        assert "xapp-test" in content


class TestSpawnCliAuth:
    """``kiroclaw spawn`` attaches X-Internal-Secret on every gateway call.

    Regression coverage for Mesh-1474: the CLI helpers in ``cli_commands.py``
    used to open ``/api/spawn`` without the per-session IPC secret, which
    caused 403 ``"gateway not running"`` errors when ``dashboard.url`` was
    set to a non-loopback host (token_auth_middleware then required either
    a session cookie or the secret header on every request).
    """

    def test_internal_secret_reads_local_secret_file(self, tmp_path, monkeypatch):
        (tmp_path / ".local_secret").write_text("abc123\n")
        monkeypatch.setattr("kiro_claw.cli_commands.config_dir", lambda: tmp_path)

        from kiro_claw.cli_commands import _internal_secret

        assert _internal_secret() == "abc123"

    def test_internal_secret_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.cli_commands.config_dir", lambda: tmp_path)

        from kiro_claw.cli_commands import _internal_secret

        assert _internal_secret() == ""

    def test_spawn_list_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".local_secret").write_text("test-secret-xyz")
        monkeypatch.setattr("kiro_claw.cli_commands.config_dir", lambda: tmp_path)

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"agents": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        from kiro_claw.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=8765)
        _spawn(args)

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://localhost:8765/api/spawn"
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "test-secret-xyz"

    def test_spawn_run_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".local_secret").write_text("run-secret-abc")
        monkeypatch.setattr("kiro_claw.cli_commands.config_dir", lambda: tmp_path)

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "agent-1", "task": "hi"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        from kiro_claw.cli_commands import _spawn_run

        args = argparse.Namespace(task="do thing", fire_and_forget=True, port=8765)
        _spawn_run(args, "http://localhost:8765")

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://localhost:8765/api/spawn"
        assert req.data == b'{"task": "do thing"}'
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "run-secret-abc"
        assert headers_lower["content-type"] == "application/json"

    def test_spawn_list_403_prints_token_required(self, tmp_path, monkeypatch, capsys):
        """A bare 403 from the gateway is reported, not masked as 'not running'."""
        (tmp_path / ".local_secret").write_text("")
        monkeypatch.setattr("kiro_claw.cli_commands.config_dir", lambda: tmp_path)

        def fake_urlopen(*_args: object, **_kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "http://localhost:8765/api/spawn",
                403,
                "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        from kiro_claw.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=8765)
        with pytest.raises(SystemExit) as excinfo:
            _spawn(args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Error" in out
        assert "gateway not running" not in out


class TestArtifactCli:
    """CLI-side coverage for security-critical paths in `_artifact`.

    The bulk of artifact behavior is exercised via the HTTP handler tests; this
    class focuses on the CLI's own gates (e.g. `is_sensitive_path()` refusal on
    `--content-file`).
    """

    def test_save_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AUTOSDE security-controls: --content-file must be gated by
        # is_sensitive_path() before Path.read_text() so a user (or script)
        # cannot exfiltrate ~/.aws/credentials by piping it into an artifact.
        from kiro_claw.cli_commands import _artifact

        monkeypatch.setattr(
            "kiro_claw.cli_commands.is_sensitive_path", lambda _p: True
        )
        # Surface any HTTP call as a fatal so we can prove the function exited
        # at the security check, not at the network layer.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_kw: pytest.fail(
                "_artifact must refuse before opening any HTTP request"
            ),
        )

        args = argparse.Namespace(
            artifact_action="save",
            name="x",
            kind="widget",
            content=None,
            content_file="/tmp/should-be-refused",
            description="",
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err

    def test_update_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_claw.cli_commands import _artifact

        monkeypatch.setattr(
            "kiro_claw.cli_commands.is_sensitive_path", lambda _p: True
        )
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_kw: pytest.fail(
                "_artifact must refuse before opening any HTTP request"
            ),
        )

        args = argparse.Namespace(
            artifact_action="update",
            slug="x",
            content=None,
            content_file="/tmp/should-be-refused",
            name=None,
            description=None,
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err


class TestMcpBuiltinDispatch:
    """Tests for dynamic mcp-<builtin> dispatch (coverlay: cli.py L705-707)."""

    def test_mcp_builtin_dispatches_to_module(self, monkeypatch):
        """CLI 'mcp-<builtin>' dynamically imports and runs the builtin's mcp_server.

        No builtins ship publicly (BUILTIN_NAMES is empty after de-Amazoning),
        so register a synthetic builtin name to exercise the dispatch path
        (cli.py subparser registration + dynamic import).
        """
        import kiro_claw.cli as cli_mod

        builtin_name = "fakebuiltin"
        # Patch the registry the CLI reads when building subparsers and dispatching.
        monkeypatch.setattr(cli_mod, "_BUILTIN_NAMES", [builtin_name])
        mock_module = MagicMock()

        monkeypatch.setattr(sys, "argv", ["kiroclaw", f"mcp-{builtin_name}"])
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            cli_mod.main()

        mock_import.assert_called_once_with(
            f"kiro_claw.apps.builtins.{builtin_name}.mcp_server"
        )
        mock_module.run_mcp_server.assert_called_once()


class TestProjectDirFile:
    """Tests for _project_dir_file helper (coverlay: cli.py L59-61)."""

    def test_returns_config_dir_path(self, monkeypatch, tmp_path):
        """_project_dir_file should return config_dir() / 'project_dir'."""
        monkeypatch.setattr("kiro_claw.cli.config_dir", lambda: tmp_path)
        from kiro_claw.cli import _project_dir_file

        assert _project_dir_file() == tmp_path / "project_dir"


class TestSeedDispatch:
    """Tests for --seed dispatch before gateway startup (coverlay: cli.py L624-627)."""

    def test_seed_calls_seed_cmd(self, monkeypatch):
        """When --seed is provided, seed_cmd should be called before gateway."""
        monkeypatch.setattr(sys, "argv", ["kiroclaw", "gateway", "--seed", "demo"])
        mock_seed = MagicMock(return_value=0)
        with patch("kiro_claw.cli.seed_cmd", mock_seed), patch(
            "kiro_claw.cli._gateway"
        ), patch("kiro_claw.cli.asyncio.run"):
            from kiro_claw.cli import main

            main()
        mock_seed.assert_called_once()

    def test_seed_nonzero_exits(self, monkeypatch):
        """When seed_cmd returns non-zero, CLI should sys.exit with that code."""
        monkeypatch.setattr(sys, "argv", ["kiroclaw", "gateway", "--seed", "bad"])
        mock_seed = MagicMock(return_value=1)
        with patch("kiro_claw.cli.seed_cmd", mock_seed):
            from kiro_claw.cli import main

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_seed_skips_seed_cmd(self, monkeypatch):
        """When --seed is not provided, seed_cmd should not be called."""
        monkeypatch.setattr(sys, "argv", ["kiroclaw", "gateway"])
        mock_seed = MagicMock()
        with patch("kiro_claw.cli.seed_cmd", mock_seed), patch(
            "kiro_claw.cli._gateway"
        ), patch("asyncio.run"):
            from kiro_claw.cli import main

            main()
        mock_seed.assert_not_called()

    def test_seed_with_replace_flag(self, monkeypatch):
        """--seed with --seed-replace should call seed_cmd."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["kiroclaw", "gateway", "--seed", "demo", "--seed-replace"],
        )
        mock_seed = MagicMock(return_value=0)
        with patch("kiro_claw.cli.seed_cmd", mock_seed), patch(
            "kiro_claw.cli._gateway"
        ), patch("asyncio.run"):
            from kiro_claw.cli import main

            main()
        mock_seed.assert_called_once()


class TestDoctorOllamaDocker:
    """Tests for doctor detecting Ollama via Docker container."""

    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    def test_doctor_detects_ollama_docker(self, tmp_path, capsys):
        """When native ollama is missing but Docker container exists, report as installed."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)

        def which_side_effect(binary):
            if binary == "ollama":
                return None
            return f"/usr/local/bin/{binary}"

        docker_result = MagicMock(returncode=0, stdout="running", stderr="")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "/usr/local/bin/docker" and "inspect" in cmd:
                return docker_result
            return default_run

        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=which_side_effect),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", side_effect=run_side_effect),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "Docker (" in out
        assert "[running]" in out
        assert "not installed" not in out

    def test_doctor_no_ollama_no_docker(self, tmp_path, capsys):
        """When neither native ollama nor Docker container exists, report not installed."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)

        def which_side_effect(binary):
            if binary in ("ollama", "docker"):
                return None
            return f"/usr/local/bin/{binary}"

        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=which_side_effect),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "not installed" in out

    def test_doctor_docker_container_not_found(self, tmp_path, capsys):
        """When docker exists but container doesn't, fall through to not installed."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)

        def which_side_effect(binary):
            if binary == "ollama":
                return None
            return f"/usr/local/bin/{binary}"

        docker_not_found = MagicMock(returncode=1, stdout="", stderr="No such object")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "/usr/local/bin/docker" and "inspect" in cmd:
                return docker_not_found
            return default_run

        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=which_side_effect),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", side_effect=run_side_effect),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "not installed" in out

    def test_doctor_docker_inspect_timeout(self, tmp_path, capsys):
        """When docker inspect times out, fall through gracefully to not installed."""
        agent_file = tmp_path / "kiroclaw.json"
        _healthy_agent_file(agent_file)

        def which_side_effect(binary):
            if binary == "ollama":
                return None
            return f"/usr/local/bin/{binary}"

        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "/usr/local/bin/docker" and "inspect" in cmd:
                raise subprocess.TimeoutExpired(cmd, 5)
            return default_run

        with (
            patch("kiro_claw.cli_doctor.shutil.which", side_effect=which_side_effect),
            patch("kiro_claw.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_claw.cli_doctor.subprocess.run", side_effect=run_side_effect),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_claw.cli_doctor.is_local_only", return_value=True),
            patch("kiro_claw.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_claw.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "not installed" in out


class TestPrintTokenUrl:
    """Tests for _print_token_url (auto-token after restart)."""

    def test_prints_token_on_success(self, tmp_path, capsys, monkeypatch):
        from kiro_claw.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_claw.cli_server.KiroClawConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "abc123"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://localhost:7777?token=abc123" in out

    def test_prints_custom_origin(self, tmp_path, capsys, monkeypatch):
        from kiro_claw.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_claw.cli_server.KiroClawConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="http://kiroclaw.dev:7777")),
        )
        monkeypatch.setattr(
            "kiro_claw.cli_server.dashboard_origin", lambda u: "http://kiroclaw.dev:7777"
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "xyz789"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://kiroclaw.dev:7777/?token=xyz789" in out

    def test_fallback_on_timeout(self, tmp_path, capsys, monkeypatch):
        from kiro_claw.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.cli_server._RESTART_READY_TIMEOUT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kiroclaw token" in out

    def test_fallback_on_no_secret(self, tmp_path, capsys, monkeypatch):
        from kiro_claw.cli_server import _print_token_url

        monkeypatch.setattr("kiro_claw.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.cli_server._RESTART_READY_TIMEOUT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kiroclaw token" in out
