"""Shared environment helpers for subprocess spawning."""

from __future__ import annotations

import functools
import os
from pathlib import Path

# Common directories where MCP server binaries may be installed.
# Order matters — earlier entries take precedence.
_EXTRA_PATH_DIRS = (
    "{home}/.aim/mcp-servers",
    "{home}/.local/bin",
    "{home}/.toolbox/bin",
    "{home}/.npm-packages/bin",
    "{home}/.local/share/mise/shims",
    "{home}/.volta/bin",
    "/opt/homebrew/bin",  # Apple Silicon Homebrew node / global npm bins
)


def _node_version_manager_bins(home: str) -> list[str]:
    """Return node bin dirs from version managers with dynamic version paths.

    nvm and fnm install each Node version under a versioned directory, so the
    bin path cannot be a static template in ``_EXTRA_PATH_DIRS``.  Glob the
    install roots and return every ``bin`` dir, newest version first.  A
    non-login gateway (launchd / systemd) does not inherit these on ``$PATH``,
    so adding them lets us find globally-installed MCP binaries such as
    ``claude-agent-acp`` that were installed via ``npm i -g`` under nvm/fnm.
    """
    bins: list[str] = []
    roots = (
        Path(home) / ".nvm" / "versions" / "node",
        Path(home) / ".fnm" / "node-versions",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for ver_dir in sorted(root.glob("*"), reverse=True):
            bin_dir = ver_dir / "bin"
            if bin_dir.is_dir():
                bins.append(str(bin_dir))
    return bins


@functools.lru_cache(maxsize=1)
def is_toolbox_install() -> bool:
    """Return True if the running kiroclaw binary was installed via Toolbox."""
    import sys

    exe = Path(sys.executable).resolve()
    toolbox_dir = (Path.home() / ".toolbox").resolve()
    try:
        exe.relative_to(toolbox_dir)
        return True
    except ValueError:
        return False


def augmented_path(base_path: str = "") -> str:
    """Return *base_path* prepended with well-known MCP binary directories.

    When KiroClaw runs under systemd or another non-login shell the
    inherited ``$PATH`` rarely includes directories like
    ``~/.aim/mcp-servers``.  Both the MCP-probe code and the kiro-cli
    spawn code need the same augmentation — this helper keeps them in
    sync.
    """
    home = os.path.expanduser("~")
    extra = [d.format(home=home) for d in _EXTRA_PATH_DIRS]
    extra += _node_version_manager_bins(home)
    parts = extra + ([base_path] if base_path else [])
    return os.pathsep.join(parts)
