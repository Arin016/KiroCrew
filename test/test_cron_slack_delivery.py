"""Tests for cron job Slack delivery error handling.

Verifies that Slack delivery failures do not mark the job as failed,
that dashboard notifications are redacted, and that failure-path Slack
errors are also guarded.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.cron import CronJob, CronSchedule


def _make_gateway():
    """Build a minimal GatewayOrchestrator with mocked dependencies."""
    from kiro_claw.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _make_job(**overrides):
    defaults = dict(
        id="j1",
        name="test-job",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        channel="C123",
    )
    defaults.update(overrides)
    return CronJob(**defaults)


def _run_callback(gw, job, stream_result="done", stream_side_effect=None):
    """Init cron on the gateway, capture the callback, and invoke it."""
    captured_cb = None

    async def fake_stream(client, msg, **kwargs):
        if stream_side_effect:
            raise stream_side_effect
        return stream_result

    with patch("kiro_claw.slack.gateway.stream_and_collect", fake_stream), patch(
        "kiro_claw.slack.gateway.CronService"
    ) as mock_cron_cls:

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.side_effect = capture_cron

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            return await captured_cb(job)

        return asyncio.run(_init_and_run())


class TestSlackDeliveryFailureDoesNotFailJob:
    """Slack post_blocks throwing should not mark the cron job as failed."""

    def test_job_returns_result_when_slack_throws(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(side_effect=Exception("not_in_channel"))
        job = _make_job()
        result = _run_callback(gw, job)
        assert result == "done"

    def test_dashboard_gets_slack_failure_notification(self) -> None:
        gw = _make_gateway()
        gw.slack.post_blocks = AsyncMock(side_effect=Exception("channel_not_found"))
        job = _make_job()
        _run_callback(gw, job)
        calls = gw.dashboard_state.notify.call_args_list
        # First call: success notification, second: Slack failure warning
        assert len(calls) == 2
        assert "⚠️" in calls[1].args[2]
        assert "channel_not_found" in calls[1].args[2]

    def test_job_succeeds_when_slack_is_none(self) -> None:
        gw = _make_gateway()
        gw.slack = None
        job = _make_job()
        result = _run_callback(gw, job)
        assert result == "done"


class TestDashboardNotificationRedaction:
    """Dashboard notify must redact result_text."""

    def test_dashboard_notify_calls_redaction(self) -> None:
        gw = _make_gateway()
        gw.slack = None  # skip Slack path
        job = _make_job()

        with patch("kiro_claw.slack.gateway.redact_exfiltration_urls") as mock_url, patch(
            "kiro_claw.slack.gateway.redact_credentials"
        ) as mock_cred:
            mock_url.return_value = ("redacted_url", False)
            mock_cred.return_value = ("fully_redacted", False)
            _run_callback(gw, job, stream_result="secret http://evil.com data")

        mock_url.assert_called()
        mock_cred.assert_called()
        body = gw.dashboard_state.notify.call_args.args[2]
        assert body == "fully_redacted"


class TestFailurePathSlackGuarded:
    """The except-block Slack notification should not raise if Slack throws."""

    def test_failure_path_slack_error_does_not_propagate(self) -> None:
        gw = _make_gateway()
        gw.slack.post_message = AsyncMock(side_effect=Exception("slack_down"))
        job = _make_job()
        with pytest.raises(RuntimeError, match="job broke"):
            _run_callback(gw, job, stream_side_effect=RuntimeError("job broke"))
        gw.slack.post_message.assert_awaited_once()
