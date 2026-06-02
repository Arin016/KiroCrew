"""Git coordination for TaskRunner — per-step commits, worktree isolation, revert."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_claw.taskrunner import Project, Task

logger = logging.getLogger(__name__)


async def init_workspace(run: Project) -> None:
    """Set up git branch + worktree for task isolation."""
    orig_dir = run.work_dir
    branch = f"kiroclaw/task/{run.task_id}"

    if await _is_git_repo(orig_dir):
        run.base_branch = (await _git(orig_dir, "rev-parse", "--abbrev-ref", "HEAD")).strip()
        repo_root = (await _git(orig_dir, "rev-parse", "--show-toplevel")).strip()
        wt_dir = str(Path(repo_root).parent / ".kiroclaw-work" / run.task_id)
        await _git(orig_dir, "worktree", "add", wt_dir, "-b", branch)
        run.work_dir = wt_dir
        run.worktree_path = wt_dir
        run.repo_root = repo_root
    else:
        await _git(orig_dir, "init")
        await _git(orig_dir, "add", "-A")
        await _git(orig_dir, "commit", "-m", "initial", "--allow-empty")
        await _git(orig_dir, "add", "-A")  # capture files created by git hooks
        await _git(orig_dir, "commit", "--amend", "-m", "initial", "--allow-empty")
        run.base_branch = (await _git(orig_dir, "rev-parse", "--abbrev-ref", "HEAD")).strip()
        await _git(orig_dir, "checkout", "-b", branch)

    run.branch_name = branch


async def commit_step(run: Project, step: Task) -> str:
    """Stage all changes and commit. Returns sha or empty string."""
    await _git(run.work_dir, "add", "-A")
    head_tree = (await _git(run.work_dir, "rev-parse", "HEAD^{tree}")).strip()
    idx_tree = (await _git(run.work_dir, "write-tree")).strip()
    if head_tree == idx_tree:
        return ""
    msg = f"step {step.index}: {step.title}"
    await _git(run.work_dir, "commit", "-m", msg)
    sha = (await _git(run.work_dir, "rev-parse", "HEAD")).strip()
    run.commit_hashes.append(sha)
    return sha


async def revert_step(run: Project) -> None:
    """Revert the last commit (failed step). No-op if nothing to revert."""
    if not run.commit_hashes:
        return
    try:
        await _git(run.work_dir, "reset", "--hard", "HEAD~1")
        run.commit_hashes.pop()
    except Exception:
        logger.debug("git revert failed", exc_info=True)


async def get_state_summary(run: Project) -> str:
    """Build context from git log + diff stat."""
    try:
        log = await _git(run.work_dir, "log", "--oneline", f"{run.base_branch}..HEAD")
        stat = await _git(run.work_dir, "diff", "--stat", run.base_branch)
    except Exception:
        return ""
    parts = []
    if log.strip():
        parts.append(f"## Git Log (changes so far)\n```\n{log.strip()}\n```")
    if stat.strip():
        parts.append(f"## Files Changed\n```\n{stat.strip()}\n```")
    return "\n\n".join(parts)


async def get_step_diff(run: Project) -> str:
    """Get the diff of the last commit (for review)."""
    try:
        return await _git(run.work_dir, "diff", "HEAD~1")
    except Exception:
        return ""


async def finalize(run: Project) -> str:
    """Clean up worktree if used. Return branch name."""
    if run.worktree_path:
        try:
            await _git(run.repo_root, "worktree", "remove", run.worktree_path, "--force")
        except Exception:
            logger.debug("worktree cleanup failed", exc_info=True)
    return run.branch_name


async def _is_git_repo(path: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--is-inside-work-tree",
        cwd=path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def _git(work_dir: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode()}")
    return stdout.decode()
