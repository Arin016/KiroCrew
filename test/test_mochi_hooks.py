"""Tests for the Mochi builtin's lifecycle wiring (hooks.py).

What must hold: hooks are idempotent (the LifecycleDispatcher fires them at
gateway start/stop AND app enable/disable), disable leaks no task, the owner
loop survives a poll exception, and an unbound spawn seam degrades through
the ported failure paths instead of crashing the loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import hooks


class _Ctx:
    """Minimal AppContext stand-in (name, data_dir, events)."""

    def __init__(self, tmp_path: Path) -> None:
        self.name = "mochi"
        self.data_dir = tmp_path
        self.events = None
        self.config: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _fresh_module_state():
    hooks.set_spawn_fn(None)
    hooks._runtime = None
    yield
    hooks.set_spawn_fn(None)
    hooks._runtime = None


class TestLifecycleHookResolution:
    """Pins the framework fix: builtin hooks resolve via dotted package import
    (builtins ship no code in the data-home app dir), and — load-bearing —
    the hook and the routes share ONE module instance."""

    def test_builtin_hook_resolves_from_package(self):
        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        func = LifecycleDispatcher._resolve_hook("mochi", "hooks:on_startup")
        assert func is hooks.on_startup  # same object, not a parallel copy

    def test_builtin_hook_missing_callable_raises(self):
        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        with pytest.raises(ImportError, match="not found in builtin"):
            LifecycleDispatcher._resolve_hook("mochi", "hooks:nonexistent")

    def test_the_dispatcher_does_not_load_a_parallel_copy(self):
        """`_invoke` must resolve through `_resolve_hook`, not `load_app_module`.

        `load_app_module` is a file-path load that registers the module as
        `_kirocrew_app_<app>.<mod>` — a SECOND object. For a builtin whose routes
        read state its `on_startup` created, that means the hook populates the
        copy and the routes keep reading the empty original: no error anywhere,
        the feature is just inert. Upstream's launch-boundary change routed
        `_invoke` straight at `load_app_module`, which reintroduced exactly that.
        """
        import inspect

        from kiro_crew.apps.lifecycle import LifecycleDispatcher

        src = inspect.getsource(LifecycleDispatcher._invoke)
        assert "_resolve_hook(" in src
        # A CALL, not a mention — the explanatory comment names the function.
        assert "load_app_module(" not in src, (
            "a direct file-path load here gives a builtin's hook a different "
            "module object than its routes see"
        )

    @pytest.mark.asyncio
    async def test_dispatch_enable_starts_the_runtime(self, tmp_path, monkeypatch):
        """End-to-end through the real dispatcher: enable → on_startup runs."""
        from kiro_crew.apps import lifecycle as lc

        monkeypatch.setattr(lc, "app_dir", lambda name: tmp_path)
        dispatcher = lc.LifecycleDispatcher()
        app_info = {
            "name": "mochi",
            "manifest": {
                "backend": {"hooks": {"on_startup": "hooks:on_startup"}},
                "permissions": {},
            },
        }
        ok = await dispatcher.dispatch_enable(app_info)
        assert ok is True
        assert hooks._runtime is not None
        await hooks.on_shutdown(None)


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_startup_builds_runtime_and_shutdown_stops_it(self, tmp_path):
        ctx = _Ctx(tmp_path)
        hooks.on_startup(ctx)
        rt = hooks._runtime
        assert rt is not None
        assert rt._task is not None and not rt._task.done()
        # stats were loaded and the launch recorded
        assert rt.stats.get_stats()["streak"] == 1

        await hooks.on_shutdown(ctx)
        assert rt._task is None
        # stats were flushed to disk on shutdown
        assert (tmp_path / "stats.json").exists()

    @pytest.mark.asyncio
    async def test_hooks_are_idempotent(self, tmp_path):
        ctx = _Ctx(tmp_path)
        hooks.on_startup(ctx)
        first_task = hooks._runtime._task
        hooks.on_startup(ctx)  # double-enable: same task, no second loop
        assert hooks._runtime._task is first_task

        await hooks.on_shutdown(ctx)
        await hooks.on_shutdown(ctx)  # double-disable: no error

    @pytest.mark.asyncio
    async def test_reenable_after_disable_restarts_the_loop(self, tmp_path):
        ctx = _Ctx(tmp_path)
        hooks.on_startup(ctx)
        await hooks.on_shutdown(ctx)
        hooks.on_startup(ctx)
        rt = hooks._runtime
        assert rt._task is not None and not rt._task.done()
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_disable_leaves_no_pending_task(self, tmp_path):
        ctx = _Ctx(tmp_path)
        hooks.on_startup(ctx)
        task = hooks._runtime._task
        await hooks.on_shutdown(ctx)
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_unbound_spawn_degrades_not_crashes(self, tmp_path):
        """With no spawn fn bound, a poll that wants to spawn takes the ported
        failure path (lock backoff) and the loop keeps running."""
        ctx = _Ctx(tmp_path)
        # A due freestyle task would spawn — missing queue triggers plan,
        # which raises through the seam; poller must swallow it (try/except
        # around trigger_plan) and the loop must go on.
        hooks.on_startup(ctx)
        rt = hooks._runtime
        await rt.poller.poll()  # drive one poll directly: no exception escapes
        assert rt._task is not None and not rt._task.done()
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_bound_spawn_reaches_the_seam(self, tmp_path):
        calls: list[tuple[str, str]] = []

        async def spawn(prompt: str, agent: str) -> str:
            calls.append((prompt, agent))
            return "spawn-1"

        hooks.set_spawn_fn(spawn)
        ctx = _Ctx(tmp_path)
        hooks.on_startup(ctx)
        rt = hooks._runtime
        await rt.poller.poll()  # missing queue → trigger_plan → seam
        assert calls and "mochi-plan" in calls[0][0]
        # Background work runs as mochi-bg, never the foreground chat agent.
        assert calls[0][1] == "mochi-bg"
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_deterministic_tasks_run_without_spawner(self, tmp_path):
        """Stats/pinned/deterministic queue tasks work with NO spawn fn —
        graceful degradation, not half-broken."""
        ctx = _Ctx(tmp_path)
        events: list[tuple[str, dict]] = []

        class Bus:
            def publish_to_app(self, event_type: str, data: dict) -> None:
                events.append((event_type, data))

        ctx.events = Bus()
        # execute_after must be RECENT: the ported stale-skip marks planned
        # move/mood tasks >30 min overdue as done UNEXECUTED (quirk 1 of the
        # poller port — this test originally tripped over it with a month-old
        # timestamp, which was the port working as specified).
        import datetime as _dt

        just_due = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        far_future = "2099-01-01T00:00:00.000Z"
        (tmp_path / "mochi-queue.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "planned_at": just_due,
                    "planned_until": far_future,
                    "needs_replan": False,
                    "tasks": [
                        {
                            "id": "m1",
                            "type": "mood",
                            "action": {"mood": "happy"},
                            "execute_after": just_due,
                            "done": False,
                            "source": "agent",
                        }
                    ],
                }
            )
        )
        hooks.on_startup(ctx)
        await hooks._runtime.poller.poll()
        mood_events = [e for e in events if e[0] == "mochi:mood"]
        assert len(mood_events) == 1
        # Mood now routes through the pet state manager (single source of truth),
        # which broadcasts via the {args:[value]} helper and stores current_mood.
        assert mood_events[0][1]["args"] == ["happy"]
        assert hooks._runtime.state_manager.current_mood == "happy"
        await hooks.on_shutdown(ctx)
