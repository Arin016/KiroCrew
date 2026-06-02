"""Shared cleanup utilities for LLM provider session files.

Provides path safety validation used by all providers before deleting
session files on disk.  Also provides helpers for Claude Code session
path resolution.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from kiro_claw.cc_agent import cc_config_root

logger = logging.getLogger(__name__)


def _is_safe_path(target: Path, expected_root: Path) -> bool:
    """Validate target is strictly under expected_root (no traversal).

    Returns True only if the resolved target path is a proper child of
    the resolved expected_root (never equal to it).  Deleting the root
    directory itself is never correct during session cleanup.

    Returns False on any resolution error (broken symlinks, permission
    issues, etc.).
    """
    try:
        resolved = target.resolve()
        root = expected_root.resolve()
        return str(resolved).startswith(str(root) + os.sep)
    except (OSError, ValueError):
        return False


# ── Claude Code session helpers ──────────────────────────────────────

_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def _encode_cc_project_dir(cwd: str | Path) -> str:
    """Encode an absolute CWD into a Claude Code project directory name.

    Claude Code replaces every non-alphanumeric character in the
    absolute path with ``-``.  For example::

        /Users/me/proj  ->  -Users-me-proj
        /home/user/work-space  ->  -home-user-work-space

    Uses ``os.path.abspath`` (not ``Path.resolve``) so the literal CWD is
    absolutized *without* following symlinks — this matches the project-dir
    name Claude Code writes and keeps the encoding deterministic across
    platforms.  On macOS ``resolve()`` would rewrite ``/tmp`` -> ``/private/tmp``
    and ``/home`` -> ``/System/Volumes/Data/home``, breaking the mapping.
    """
    return _NON_ALNUM_RE.sub("-", os.path.abspath(str(cwd)))


def _cc_config_root() -> Path:
    """Resolve the CC config root (isolated dir or ~/.claude) for path building.

    Thin alias for :func:`cc_agent.cc_config_root` — the single source of truth
    shared with the spawn-env injection (``config/loader._claude_code``) and the
    resume guard (``acp/client``). Imported at module top: ``cc_agent`` has no
    module-level ``kiro_claw`` imports, so ``cleanup → cc_agent`` is acyclic.
    """
    return cc_config_root()


def _cc_session_paths(
    cwd: str | Path, sid: str, config_root: Path | None = None
) -> list[Path]:
    """Return all paths to delete for a given CC session.

    Includes (under the resolved CC config root — ``<config_dir>/cc-config``
    when isolation is enabled, else ``~/.claude``):
      - ``<root>/projects/<encoded-cwd>/<sid>.jsonl`` (transcript)
      - ``<root>/projects/<encoded-cwd>/<sid>/`` (subagents/ + tool-results/)
      - ``<root>/file-history/<sid>/`` (pre-edit snapshots)

    Excludes ``memory/`` which is session-shared and must never be deleted.

    ``config_root`` defaults to :func:`_cc_config_root` so live callers follow
    isolation automatically; tests pass an explicit root.
    """
    encoded = _encode_cc_project_dir(cwd)
    claude_root = Path(config_root) if config_root is not None else _cc_config_root()
    project_dir = claude_root / "projects" / encoded

    paths: list[Path] = [
        project_dir / f"{sid}.jsonl",
        project_dir / sid,  # subagents/ + tool-results/ live here
        claude_root / "file-history" / sid,
    ]
    return paths


def _cleanup_cc_session(
    cwd: str | Path, session_id: str, config_root: Path | None = None
) -> None:
    """Delete Claude Code session files for the given session ID.

    Defense-in-depth: validates every resolved path is under the CC config root
    before deletion.  Idempotent — missing files/dirs do not raise.
    Never deletes the memory/ directory.

    ``config_root`` defaults to :func:`_cc_config_root`; the SAME root is used
    for both path building and the ``_is_safe_path`` containment check, so an
    isolated-dir transcript is neither orphaned nor blocked as traversal.
    """
    if not session_id or session_id in (".", ".."):
        return

    claude_root = Path(config_root) if config_root is not None else _cc_config_root()
    paths = _cc_session_paths(cwd, session_id, config_root=claude_root)

    for target in paths:
        if not _is_safe_path(target, claude_root):
            logger.error(
                "_cleanup_cc_session: path traversal blocked for %s", target
            )
            continue
        try:
            if target.is_file():
                target.unlink(missing_ok=True)
                logger.info("Deleted CC session file: %s", target)
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                logger.info("Deleted CC session dir: %s", target)
        except OSError:
            logger.warning(
                "_cleanup_cc_session: failed to delete %s",
                target,
                exc_info=True,
            )
