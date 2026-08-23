"""Regression test for the diverged-checkout guard in ``_auto_apply_update``.

Issue #5163: ``GatewayOrchestrator._auto_apply_update`` is the mandatory
version-floor apply path. Its pre-reset preflight was only a content diff plus a
porcelain check that logs-and-proceeds, so a DIVERGED checkout (committed local
work both ahead of AND behind ``origin/<branch>``) passed both and had its local
commits ``git reset --hard`` away unattended.

The guard runs ``git rev-list --count --left-right HEAD...origin/<branch>`` after
the fetch and, when ``ahead>0 and behind>0``, refuses to touch the working tree.

This test drives ``_auto_apply_update`` against a REAL temp git repository whose
``mainline`` checkout is genuinely diverged from its ``origin`` and asserts that
the committed local commit survives (HEAD unchanged) and no reset occurred.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack.gateway import GatewayOrchestrator


def _git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd`` and return stripped stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_orchestrator() -> GatewayOrchestrator:
    """Build a GatewayOrchestrator with mocked credentials (no dashboard/crons)."""
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return GatewayOrchestrator(
            cfg,
            no_dashboard=True,
            no_crons=True,
            no_open=True,
            test_mode=True,
        )


def _build_diverged_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create origin + a diverged ``mainline`` working checkout.

    Returns ``(work_dir, local_head_sha)`` where the work_dir is on branch
    ``mainline`` carrying one local commit ahead of AND behind ``origin/mainline``.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=mainline")

    # Seed the shared base commit and push it to origin.
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=mainline")
    git(seed, "remote", "add", "origin", str(origin))
    (seed / "file.txt").write_text("base\n")
    git(seed, "add", "file.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", "mainline")

    # Working checkout, cloned from origin at the base.
    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), "work")
    git(work, "checkout", "mainline")

    # Advance origin by one commit (work is now BEHIND by 1).
    (seed / "file.txt").write_text("remote change\n")
    git(seed, "commit", "-am", "remote advance")
    git(seed, "push", "origin", "mainline")

    # Add a local commit on the working checkout (work is now AHEAD by 1 too).
    (work / "local.txt").write_text("local work\n")
    git(work, "add", "local.txt")
    git(work, "commit", "-m", "local committed work")

    # Update the remote-tracking ref so origin/mainline reflects the advance.
    # (The apply's own ``git fetch`` would do this too, but we assert divergence
    # in the test body before calling the apply.)
    git(work, "fetch", "origin", "mainline")

    local_head = git(work, "rev-parse", "HEAD")
    return work, local_head


@pytest.mark.asyncio
async def test_diverged_checkout_reset_is_refused_and_local_commit_survives(tmp_path):
    """A diverged ``mainline`` checkout is NOT reset; its local commit survives."""
    work, local_head = _build_diverged_repo(tmp_path)

    # Sanity: the checkout is genuinely diverged before we run the apply.
    counts = _git(work, "rev-list", "--count", "--left-right", "HEAD...origin/mainline")
    ahead, behind = (int(x) for x in counts.split())
    assert ahead > 0 and behind > 0, f"fixture not diverged: {ahead} ahead, {behind} behind"

    orch = _make_orchestrator()
    ds = MagicMock()
    ds.push_update_progress = MagicMock()
    ds.clear_update_progress = MagicMock()
    orch.dashboard_state = ds

    # Keep the source-pin preflight from short-circuiting: no block reason.
    with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(work)}):
        # These are imported inside the method from update_governance, so patch
        # them at their source module rather than the gateway namespace.
        with (
            patch(
                "kiro_crew.platform.update_governance.update_blocked_reason",
                return_value="",
            ),
            patch(
                "kiro_crew.platform.update_governance.resolve_remote_url",
                return_value="origin",
            ),
        ):
            # Reinstall / frontend build / restart must never be reached; fail
            # loudly if the guard lets execution fall through to them.
            with (
                patch(
                    "kiro_crew.slack.gateway.build_frontend_async",
                    new_callable=AsyncMock,
                ) as mock_build,
                patch(
                    "kiro_crew.dep_sync.sync_or_reinstall",
                    side_effect=AssertionError("reset path reached — guard failed"),
                ),
                patch("os.execv", side_effect=AssertionError("restart reached")),
            ):
                await orch._auto_apply_update()

    # HEAD is unchanged: the committed local work survived.
    assert _git(work, "rev-parse", "HEAD") == local_head
    assert (work / "local.txt").exists()

    # The reset path (frontend build) was never taken.
    mock_build.assert_not_awaited()

    # Non-compliance was surfaced through the update-status surface as a
    # "failed" refusal, and the build ("building") stage was never entered.
    statuses = [c.args[0] for c in ds.push_update_progress.call_args_list if c.args]
    assert "failed" in statuses
    assert "building" not in statuses
    assert statuses[-1] == "failed"


def _build_ahead_only_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create origin + a ``mainline`` checkout that is AHEAD only (behind==0).

    Models a developer checkout (or a detached HEAD inferred as ``mainline``)
    carrying a committed local commit that is not yet on origin. This work is
    still destroyed by a hard reset even though the checkout is not "diverged"
    in the ahead-AND-behind sense — so the guard must refuse it too.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=mainline")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=mainline")
    git(seed, "remote", "add", "origin", str(origin))
    (seed / "file.txt").write_text("base\n")
    git(seed, "add", "file.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", "mainline")

    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), "work")
    git(work, "checkout", "mainline")

    # Local commit only; origin does not advance -> ahead==1, behind==0.
    (work / "local.txt").write_text("local work\n")
    git(work, "add", "local.txt")
    git(work, "commit", "-m", "local committed work")
    git(work, "fetch", "origin", "mainline")

    return work, git(work, "rev-parse", "HEAD")


def _build_fast_forward_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create origin + a ``mainline`` checkout that is BEHIND only (ahead==0).

    A pure fast-forward: origin has advanced, the checkout has no local commits.
    The guard MUST still allow the reset here (this is the ordinary update case).
    Returns ``(work_dir, origin_head_sha)`` — the SHA the checkout should reset to.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=mainline")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=mainline")
    git(seed, "remote", "add", "origin", str(origin))
    (seed / "file.txt").write_text("base\n")
    git(seed, "add", "file.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", "mainline")

    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), "work")
    git(work, "checkout", "mainline")

    # Advance origin only -> work is behind==1, ahead==0.
    (seed / "file.txt").write_text("remote change\n")
    git(seed, "commit", "-am", "remote advance")
    git(seed, "push", "origin", "mainline")
    git(work, "fetch", "origin", "mainline")

    origin_head = git(work, "rev-parse", "origin/mainline")
    return work, origin_head


async def _apply_ctx(orch: GatewayOrchestrator, work: Path):
    """Drive ``_auto_apply_update`` against ``work`` with governance/reinstall
    mocked out (used by the refusal cases that never reach the reset path)."""
    with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(work)}):
        with (
            patch("kiro_crew.platform.update_governance.update_blocked_reason", return_value=""),
            patch("kiro_crew.platform.update_governance.resolve_remote_url", return_value="origin"),
            patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock),
            patch("kiro_crew.dep_sync.sync_or_reinstall", new_callable=MagicMock),
            patch("os.execv"),
            patch("shutil.which", return_value=None),
        ):
            await orch._auto_apply_update()


@pytest.mark.asyncio
async def test_ahead_only_checkout_reset_is_refused(tmp_path):
    """An ahead-only ``mainline`` checkout (local commits, not behind) is refused.

    The spec's reported case is ahead-AND-behind, but a hard reset destroys
    committed local work whenever ``ahead>0`` regardless of ``behind`` — the
    detached-HEAD-inferred-as-mainline variant lands here. The guard refuses it.
    """
    work, local_head = _build_ahead_only_repo(tmp_path)

    ahead, behind = (
        int(x)
        for x in _git(work, "rev-list", "--count", "--left-right", "HEAD...origin/mainline").split()
    )
    assert ahead > 0 and behind == 0, f"fixture not ahead-only: {ahead} ahead, {behind} behind"

    orch = _make_orchestrator()
    ds = MagicMock()
    orch.dashboard_state = ds
    await _apply_ctx(orch, work)

    # Local commit survives; refusal surfaced.
    assert _git(work, "rev-parse", "HEAD") == local_head
    assert (work / "local.txt").exists()
    statuses = [c.args[0] for c in ds.push_update_progress.call_args_list if c.args]
    assert statuses and statuses[-1] == "failed"
    assert "building" not in statuses


@pytest.mark.asyncio
async def test_unverifiable_divergence_fails_closed(tmp_path):
    """If the ahead/behind probe cannot be parsed, the reset is refused.

    A ``git rev-list`` failure or unparseable output must NOT fall through to
    ``git reset --hard`` — an unverifiable state is treated as unsafe.
    """
    work, local_head = _build_ahead_only_repo(tmp_path)
    orch = _make_orchestrator()
    ds = MagicMock()
    orch.dashboard_state = ds

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        # Corrupt only the divergence probe; leave every other git call real.
        if len(args) >= 4 and args[1] == "rev-list" and "--left-right" in args:
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"garbage-not-two-ints\n", b""))
            proc.wait = AsyncMock(return_value=0)
            return proc
        return await real_exec(*args, **kwargs)

    with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(work)}):
        with (
            patch("kiro_crew.platform.update_governance.update_blocked_reason", return_value=""),
            patch("kiro_crew.platform.update_governance.resolve_remote_url", return_value="origin"),
            patch("kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock),
            patch("os.execv", side_effect=AssertionError("restart reached — fail-open!")),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            await orch._auto_apply_update()

    # Nothing was reset; refusal surfaced.
    assert _git(work, "rev-parse", "HEAD") == local_head
    statuses = [c.args[0] for c in ds.push_update_progress.call_args_list if c.args]
    assert statuses and statuses[-1] == "failed"


@pytest.mark.asyncio
async def test_fast_forward_checkout_still_resets(tmp_path):
    """A pure fast-forward (behind-only, ahead==0) still resets — no over-refusal.

    Proves the guard does not break the ordinary update case it must leave intact.
    """
    work, origin_head = _build_fast_forward_repo(tmp_path)

    ahead, behind = (
        int(x)
        for x in _git(work, "rev-list", "--count", "--left-right", "HEAD...origin/mainline").split()
    )
    assert ahead == 0 and behind > 0, f"fixture not fast-forward: {ahead} ahead, {behind} behind"

    orch = _make_orchestrator()
    ds = MagicMock()
    orch.dashboard_state = ds

    with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": str(work)}):
        with (
            patch("kiro_crew.platform.update_governance.update_blocked_reason", return_value=""),
            patch("kiro_crew.platform.update_governance.resolve_remote_url", return_value="origin"),
            patch(
                "kiro_crew.slack.gateway.build_frontend_async", new_callable=AsyncMock
            ) as mock_build,
            patch("kiro_crew.dep_sync.sync_or_reinstall", new_callable=MagicMock),
            patch("os.execv"),
            patch("shutil.which", return_value=None),
        ):
            await orch._auto_apply_update()

    # The reset was allowed: HEAD advanced to origin, and the build stage ran.
    assert _git(work, "rev-parse", "HEAD") == origin_head
    mock_build.assert_awaited()
    statuses = [c.args[0] for c in ds.push_update_progress.call_args_list if c.args]
    assert "failed" not in statuses
