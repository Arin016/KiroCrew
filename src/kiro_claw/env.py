"""Shared environment helpers for subprocess spawning."""

from __future__ import annotations

import functools
import getpass
import logging
import os
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

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


def resolve_krb5_ccname(env: dict[str, str]) -> None:
    """Point *env* at a FILE: Kerberos ccache, mutating it in place.

    The gateway is a long-lived, non-login process.  On AL2023 the default
    ``krb5.conf`` uses ``KEYRING:persistent:<uid>`` for the ccache, and kernel
    keyrings are session-scoped — they are NOT visible to subprocesses spawned
    by a background daemon.  So a child (kiro-cli / claude / a pooled MCP
    backend) inheriting ``os.environ`` sees no usable ticket, and Kerberos-gated
    MCP servers (e.g. amazon-quick-mcp's kerberizer SSO) fail with
    "no Kerberos ticket" even though ``kinit`` succeeded in the user's shell.

    This mirrors :func:`_resolve_ssh_auth_sock` in ``acp.client``: repair the
    credential pointer at spawn time rather than trusting the daemon's stale
    env.  Resolution rules:

    * If ``KRB5CCNAME`` already names a non-default scheme (``FILE:`` operator
      override, or a platform-native ``KCM:`` / ``DIR:`` / ``API:`` cache),
      leave it — the caller already has a working, non-keyring ccache.
    * Only act on Linux: the ``/tmp/krb5cc_<uid>`` workaround targets the
      AL2023 ``KEYRING:persistent`` default.  On macOS the default is the
      ``KCM:`` daemon, so blindly pointing at a stale ``/tmp`` file (e.g. left
      by a prior Linux session or container mount) would hijack a working
      ccache — gate the whole thing on ``sys.platform == "linux"``.
    * Else, if ``/tmp/krb5cc_<uid>`` resolves to a regular file we own, point
      at it.
    * Else, do nothing — no ticket to find; let the MCP surface its own
      auth error rather than masking it.

    The candidate lives in ``/tmp`` (world-writable, sticky-bit), so we ``lstat``
    it first and require ownership by the current uid.  We do NOT reject a
    uid-owned symlink: sssd-krb5 / systemd-pam-krb5 legitimately ship
    ``/tmp/krb5cc_<uid>`` as a symlink into ``/run/user/<uid>/krb5cc/...`` — the
    exact keyring-default distros this fix targets.  For a uid-owned symlink we
    follow it (``os.stat``) and require the *resolved* target to be a regular
    file owned by the current uid.  A symlink or file owned by anyone else is
    rejected, which preserves the co-tenant defense (a foreign user cannot plant
    ``/tmp/krb5cc_<victim_uid>`` and have us trust it).

    ``KRB5CCNAME`` is intentionally absent from the MCP-gateway scrub list
    (``mcp_gateway.manager._SENSITIVE_ENV_PREFIXES``), so a value set here
    propagates to pooled backends as well.
    """
    current = env.get("KRB5CCNAME", "")
    # FILE: = explicit operator override; KCM:/DIR:/API: = platform-native
    # schemes (KCM: is the macOS default). Any of these is already a working,
    # subprocess-visible ccache — never override it.
    if current.startswith(("FILE:", "KCM:", "DIR:", "API:")):
        return
    # The /tmp/krb5cc_<uid> workaround only applies to the Linux kernel-keyring
    # default. On macOS/other platforms the keyring-isolation problem does not
    # exist and a stray /tmp file must not hijack the native ccache.
    if sys.platform != "linux":
        return
    # The kernel's default FILE ccache is named by numeric UID
    # (``/tmp/krb5cc_<uid>``) — this is also what the documented workaround
    # ``kinit -c /tmp/krb5cc_$(id -u)`` produces.  Some setups instead use the
    # login name, so check that as a fallback.  ``getpass.getuser()`` is only
    # evaluated for the fallback path.
    candidates = [f"/tmp/krb5cc_{os.getuid()}"]
    try:
        candidates.append(f"/tmp/krb5cc_{getpass.getuser()}")
    except Exception as exc:  # getuser() can raise without a passwd entry / env
        logger.debug("krb5 ccache username fallback skipped: %s", type(exc).__name__)
    rejected: list[str] = []
    for cache in candidates:
        reason = _reject_reason(cache)
        if reason is None:
            env["KRB5CCNAME"] = f"FILE:{cache}"
            logger.debug("resolved KRB5CCNAME to FILE:%s", cache)
            return
        if reason != "absent":
            # A candidate physically exists but failed the ownership/type gate.
            # Log it so this is distinguishable from the plain "no ccache" case —
            # otherwise it reproduces the silent-failure gap this resolver fixes.
            rejected.append(f"{cache} ({reason})")
    if rejected:
        logger.debug("KRB5CCNAME left unset; rejected ccache candidate(s): %s", ", ".join(rejected))


def _reject_reason(cache: str) -> str | None:
    """Return ``None`` if *cache* is a usable FILE ccache, else a rejection reason.

    Accepts a regular file owned by us, or a uid-owned symlink whose resolved
    target is a regular file owned by us (sssd/systemd ship the ccache as a
    symlink into ``/run/user/<uid>/krb5cc/...``).  Rejects anything owned by
    another uid — a co-tenant on a shared ``/tmp`` cannot make us trust a
    planted file or symlink.

    Reasons are coarse, log-only labels (``absent`` means the path does not
    exist, i.e. the ordinary no-op case — callers skip logging it).
    """
    uid = os.getuid()
    try:
        st = os.lstat(cache)  # lstat: inspect the link itself, do not follow yet
    except OSError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        # A foreign-owned symlink is an attack vector; a uid-owned one may
        # legitimately point at /run/user/<uid>/krb5cc/... — follow and validate.
        if st.st_uid != uid:
            return "foreign-owned-symlink"
        try:
            st = os.stat(cache)  # resolves the symlink to its target
        except OSError:
            return "dangling-symlink"
        if not stat.S_ISREG(st.st_mode):
            return "symlink-target-not-regular"
        if st.st_uid != uid:
            return "symlink-target-foreign-owned"
        return None
    if not stat.S_ISREG(st.st_mode):
        return "not-regular"
    if st.st_uid != uid:
        return "foreign-owned"
    return None
