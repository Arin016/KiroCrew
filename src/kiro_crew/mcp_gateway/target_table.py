"""The stubbed-server -> real-launch-command mapping, published as a file.

The daemon resolves a stubbed server's real command from
``KIROCREW_MCP_TARGET_<SERVER>``, and a live process's environment cannot be
changed. That makes the mapping immutable for the daemon's lifetime, so
changing WHICH servers are stubbed can only be applied by respawning it --
draining every pooled backend and every in-flight call for what is a one-bit
change to one server. This module carries the same mapping, in the same wire
format, in a file the daemon re-reads, so the stub set becomes mutable
in place.

Precedence is per TABLE, never per key. A table that loads is the whole
answer, so a server absent from it does not resolve; the environment is
consulted only when no trustworthy table exists at all. Merging the two
key-by-key would let the environment captured at spawn -- which still names
every server stubbed back then -- keep resolving a server the operator has
since unstubbed, so the removal would appear to do nothing.

Trust matters more here than for a cache: this mapping decides which
executables the daemon launches. The environment was safe to read precisely
because only whoever spawns the daemon can set it, and a file that is re-read
at runtime does not inherit that property. So a table is honoured only when
this process owns it and no other account can write it; anything else is
ignored in favour of the environment. Values are command lines only --
backend environment is assembled separately and must never travel here.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Mapping, Optional

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Sibling of the gateway socket, like the hot-keys file.
TARGET_TABLE_FILENAME = "targets.json"

#: Payload version. Bumped only for a change a reader cannot absorb; an
#: unrecognised version is treated as untrustworthy rather than guessed at.
TARGET_TABLE_VERSION = 1

#: The env-var prefix the mapping is keyed by, and the prefix older overlays
#: and daemons wrote. Both are accepted at lookup so a table written by this
#: version resolves for a pool key built by either.
_KEY_PREFIX = "KIROCREW_MCP_TARGET_"
_LEGACY_KEY_PREFIX = "MC_MCP_TARGET_"


def default_target_table_path(socket_path: Path | str) -> Path:
    """Target-table location derived from the gateway socket path."""
    return Path(socket_path).parent / TARGET_TABLE_FILENAME


def _env_key_base(server_name: str, prefix: str) -> str:
    """The un-hashed lookup key for *server_name* under *prefix*.

    Mirrors the rewriter's key construction; both sides must agree or a
    published target silently fails to resolve.
    """
    return prefix + server_name.replace("-", "_").upper()


def lookup_target(
    mapping: Mapping[str, str], server_name: str, command_args_hash: str
) -> Optional[str]:
    """Return the target spec for a server, or ``None`` when unmapped.

    The one implementation of the lookup ORDER, shared by both sources so they
    cannot disagree about which entry wins:

    1. the args-disambiguated key, so two agents that declare the same server
       name with different target args each reach their own command instead of
       whichever sorted first;
    2. the bare server-name key, the first-wins fallback for a pool key whose
       hash has no entry;
    3. and 4. the same two under the legacy prefix, for an overlay or daemon
       written before the rename.
    """
    for prefix in (_KEY_PREFIX, _LEGACY_KEY_PREFIX):
        base = _env_key_base(server_name, prefix)
        spec = mapping.get(base + "__" + command_args_hash) or mapping.get(base)
        if spec:
            return spec
    return None


def write_target_table(path: Path, targets: Mapping[str, str]) -> bool:
    """Publish *targets* to *path*, owner-only and atomically.

    Returns whether the write landed. A failure is logged and reported rather
    than raised: the caller decides what an unpublished mapping means, and for
    a running broker that is "the new routing is not live yet" rather than
    anything that should take the broker down.
    """
    payload = {
        "version": TARGET_TABLE_VERSION,
        "targets": {str(k): str(v) for k, v in targets.items()},
    }
    try:
        # mode on mkdir, not a chmod afterwards: the directory holds the
        # socket and the hot-keys file, and a window where it is readable is
        # a window in which this table can be read.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(path, json.dumps(payload), mode=0o600, restrict_to_owner=True)
    except OSError as exc:
        logger.warning("mcp-gateway: could not publish target table %s: %s", path, exc)
        return False
    return True


def _untrusted_reason(path: Path, st: os.stat_result) -> Optional[str]:
    """Why *path* must not be honoured, or ``None`` when it is trustworthy.

    POSIX-only by construction: on Windows ``st_uid`` is a constant and the
    mode bits carry no ACL information, so neither check can say anything and
    a refusal built on them would reject every table on that platform.
    """
    if not platform_compat.IS_POSIX:
        return None
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return f"owned by uid {st.st_uid}"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"writable beyond its owner (mode {stat.S_IMODE(st.st_mode):04o})"
    return None


class TargetTableReader:
    """Reads the published table, re-reading only when the file changes.

    The table is published whenever the mapping is built -- at broker startup
    and on every stub change -- so a table that exists is by construction at
    least as current as the daemon's own spawn environment. That is what lets
    this be a plain read with no freshness arithmetic: there is no "is my copy
    newer than the environment" question to get wrong, and so no clock,
    generation or process-start comparison to be skewed by an NTP step, a VM
    restore, or a supervisor respawn.

    Identity is (inode, size, mtime), used only to skip re-parsing an unchanged
    file. A rename-into-place gives a new inode, which an mtime-only check can
    miss when two writes land inside one filesystem timestamp tick.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fingerprint: Optional[tuple[int, int, int]] = None
        self._targets: Optional[dict[str, str]] = None
        # Rate-limits the log line, not the read: a resolver that logged every
        # spawn against a bad table would flood, and one that logged only once
        # per process would hide a table that went bad later.
        self._warned_for: Optional[tuple[int, int, int]] = None

    def load(self) -> Optional[dict[str, str]]:
        """Return the published mapping, or ``None`` to fall back to the env.

        BLOCKING: stats and may read a file. Callers must run it off the event
        loop, like the sibling filesystem reads on the spawn path.

        ``None`` covers every "no trustworthy table" case -- absent, owned by
        another account, group/world-writable, unparseable, wrong shape, unknown
        version -- because they all warrant the same answer, and distinguishing
        them at the call site would invite treating some as an empty table. An
        empty table is a real state that means "nothing is stubbed" and is
        reported as ``{}``, not ``None``.
        """
        try:
            st = self._path.stat()
        except OSError:
            self._fingerprint = None
            self._targets = None
            return None

        fingerprint = (st.st_ino, st.st_size, st.st_mtime_ns)

        if fingerprint == self._fingerprint:
            return self._targets

        reason = _untrusted_reason(self._path, st)
        if reason is None:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                reason = f"unreadable ({exc})"
            else:
                targets = payload.get("targets") if isinstance(payload, dict) else None
                if not isinstance(payload, dict) or payload.get(
                    "version"
                ) != TARGET_TABLE_VERSION:
                    reason = "unrecognised payload version"
                elif not isinstance(targets, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in targets.items()
                ):
                    reason = "targets is not a mapping of strings"
                else:
                    self._fingerprint = fingerprint
                    self._targets = dict(targets)
                    self._warned_for = None
                    return self._targets

        if self._warned_for != fingerprint:
            logger.warning(
                "mcp-gateway: ignoring target table %s (%s); resolving targets "
                "from the spawn environment instead",
                self._path,
                reason,
            )
            self._warned_for = fingerprint
        self._fingerprint = fingerprint
        self._targets = None
        return None


class TargetTableCache:
    """The last loaded table, held in memory for the spawn path.

    The split exists because the two sides have different constraints: loading
    touches the filesystem and so must happen off the event loop, while the
    resolver runs inside a backend spawn on the loop and must not block. So a
    refresh task owns :meth:`refresh` and the resolver only ever reads
    :meth:`current`.

    Not locked. A refresh publishes a finished dict by rebinding one attribute,
    which is atomic under the GIL, so a concurrent reader sees either the old
    table or the new one and never a half-populated one.
    """

    __slots__ = ("_reader", "_targets")

    def __init__(self, reader: TargetTableReader) -> None:
        self._reader = reader
        self._targets: Optional[dict[str, str]] = None

    def current(self) -> Optional[dict[str, str]]:
        """The last loaded mapping, or ``None`` for "use the environment".

        Non-blocking, so the resolver can call it on the event loop.
        """
        return self._targets

    def refresh(self) -> None:
        """Re-read the table. BLOCKING: run this off the event loop."""
        self._targets = self._reader.load()
