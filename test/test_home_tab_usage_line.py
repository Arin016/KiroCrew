"""Unit tests for the Home Tab plan-usage status line renderer."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_claw.slack.events import _build_usage_status_line


def test_usage_line_under_plan():
    cache = {
        "credits_used": 4.3,
        "credits_plan": 10,
        "credits_covered": 4.3,
        "cost_usd": 0.85,
        "resets": "2026-05-15",
    }
    with patch(
        "kiro_claw.slack.events.get_usage_cache",
        return_value=cache,
    ):
        line = _build_usage_status_line()
    assert line.startswith("*Plan usage:*")
    assert "4.30 / 10 credits" in line
    assert "⚠️" not in line
    assert "$0.85" in line
    assert "resets 2026-05-15" in line


def test_usage_line_over_plan_renders_absolute_total():
    """Covered == plan → used is overage-only; total = plan + used."""
    cache = {
        "credits_used": 2249.0,
        "credits_plan": 10000,
        "credits_covered": 10000,
        "cost_usd": 89.95,
        "resets": "2026-06-01",
    }
    with patch(
        "kiro_claw.slack.events.get_usage_cache",
        return_value=cache,
    ):
        line = _build_usage_status_line()
    assert line.startswith("*Plan usage:*")
    assert "⚠️" in line
    assert "12249 / 10000 credits (over plan)" in line
    assert "$89.95" in line
    assert "resets 2026-06-01" in line


@pytest.mark.asyncio
async def test_usage_line_cold_cache_renders_loading():
    """Async so the bg-fetch ``create_task`` has a real running loop.

    ``_build_usage_status_line()`` invokes ``asyncio.create_task`` when
    cache is cold (see events.py); per the AUTOSDE
    async-test-for-event-loop rule, a sync test calling that code path
    would hit a RuntimeError that only gets masked by the broad except
    handler. Run under ``pytest.mark.asyncio`` so pytest-asyncio provides
    a loop and the scheduled task actually runs.
    """
    with patch(
        "kiro_claw.slack.events.get_usage_cache",
        return_value={},
    ), patch(
        "kiro_claw.dashboard.handlers.sessions._fetch_usage_bg",
        return_value=None,
    ):
        line = _build_usage_status_line()
    assert line.startswith("*Plan usage:*")
    assert "loading" in line.lower()
    # _fetch_usage_bg was scheduled as a task; give it one tick to let
    # the event loop schedule it so pytest-asyncio doesn't warn about
    # un-awaited coroutines.
    import asyncio as _asyncio
    await _asyncio.sleep(0)


def test_usage_line_malformed_values_degrades_gracefully():
    cache = {"credits_used": "not-a-number", "credits_plan": 10000}
    with patch(
        "kiro_claw.slack.events.get_usage_cache",
        return_value=cache,
    ):
        line = _build_usage_status_line()
    assert line.startswith("*Plan usage:*")
    assert "unavailable" in line.lower()


def test_usage_line_missing_covered_falls_back_to_under_plan_render():
    cache = {
        "credits_used": 2249.0,
        "credits_plan": 10000,
        # credits_covered omitted — older parser output
    }
    with patch(
        "kiro_claw.slack.events.get_usage_cache",
        return_value=cache,
    ):
        line = _build_usage_status_line()
    assert "⚠️" not in line
    assert "2249.00 / 10000 credits" in line
