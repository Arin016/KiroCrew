"""Translate a Kiro Crew agent config into KAS's injected-agent shape.

The KAS backend has no ``--agent`` flag, so what that flag carries for kiro-cli
has to travel in ``session/new`` instead. KAS accepts client-supplied agents in
``_meta.kiro.customAgents``, and those are the HIGHEST-precedence source in its
registry (above ``~/.kiro/agents``, ``.kiro/agents``, bundled and cloud profiles),
so the config is translated on the way out rather than migrated on disk — nothing
is written anywhere and a stale file cannot shadow the live config.

Sending the definition is not optional. KAS binds ``modeId`` only to an agent
already in its registry and **ignores an unresolvable name rather than rejecting
it**, so selecting without defining produces a completely successful
``session/new`` that runs KAS's own default mode. Measured: sending
``modeId: "kirocrew"`` alone came back with ``configOptions.currentValue ==
"vibe"``; sending it together with the definition came back ``"kirocrew"``. Every
log line looks healthy either way, which is what makes the failure worth guarding
in code rather than documenting.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from kiro_crew.config.paths import kiro_agents_dir

logger = logging.getLogger(__name__)

#: Prompt references Kiro Crew may write into an agent config. KAS rejects these
#: in its ``prompt`` field and documents resolution as the client's job, so they
#: are read here rather than forwarded and silently dropped.
_FILE_URI_SCHEME = "file://"

#: A Windows drive letter that ``urlparse`` mistook for a host, e.g. the ``C:`` in
#: ``file://C:\\Users\\...``. Matched rather than assumed so a genuine host in a
#: ``file://host/share`` UNC reference is left alone.
_DRIVE_NETLOC_RE = re.compile(r"[A-Za-z]:")

#: Keys the two formats share, copied when present and non-empty. Absent differs
#: from empty to KAS, so a falsy value is omitted rather than sent.
# Keys forwarded verbatim, because KAS reads them the same way kiro-cli does.
#
# ``resources`` is deliberately NOT here. KAS resolves those entries in its OWN
# process, so Kiro Crew's sensitive-path gate never sees the reads: an agent config
# carrying ``resources: ["file://~/.aws/credentials"]`` would put the file into
# model context. It cannot be validated host-side without reimplementing KAS's
# resolver — ``AgentResource`` is heterogeneous (a bare ``file://`` or ``skill://``
# URI, OR a ``knowledgeBase`` object whose ``include`` carries GLOB patterns), and
# its own schema states that scheme classification happens where the agent resolves
# them. A validator covering only the bare-URI form would read as a gate while the
# glob and knowledge-base paths walked straight through, which is worse than having
# no gate. So the capability is withheld until resources resolve through the
# sensitive-path check; a KAS agent silently losing its context files is a
# recoverable gap, a leaked credential is not.
_PASSTHROUGH_KEYS: tuple[str, ...] = ("description", "model", "mcpServers")


def _file_uri_to_path(uri: str, base_dir: Path | None) -> str | None:
    """Decompose a ``file:`` URI into one filesystem path, or None if unresolvable.

    THE INVARIANT: every authority form is decided EXACTLY ONCE, here, from the
    exhaustive table below — and anything that comes out relative resolves against
    *base_dir*, never against the process working directory. Deciding forms at the
    read site produced two defects in a row, the second caused by the first's fix:
    teaching the UNC branch to keep its host made ``.`` look like a host, so
    ``file://./p.md`` became ``//./p.md`` — the filesystem root on POSIX and the
    ``\\\\.\\`` DEVICE namespace on Windows.

    ``urlparse`` puts whatever follows ``//`` into ``netloc``, so the authority is
    the only part that varies:

    - ``""`` — no authority; ``path`` as written.
    - ``localhost`` — RFC 8089 spells "this machine" either way; same as empty.
    - drive-shaped (``C:``) — a Windows drive letter mis-parsed as a host; the
      drive is put back onto the path.
    - ``.`` / ``..`` — a relative prefix, NOT a host; kept as the leading path
      segment so the join below applies.
    - anything else — a real remote host (UNC); preserved as the leading ``//``,
      because dropping it retargets the read at a same-named LOCAL path.

    A relative result may escape *base_dir* (``file://../shared/p.md``): the agent
    config is the operator's own file, and the boundary that matters is the
    caller's ``safe_read_file_bytes``, which canonicalizes and re-checks the
    resolved target. Resolving against the gateway's working directory is the part
    that is never acceptable — it silently reads an unrelated file.
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        # urlparse RAISES on a bracket it reads as a malformed IPv6 authority
        # ("file://[bad/path" -> "Invalid IPv6 URL"). The URI comes from an agent
        # config the operator hand-writes, so a typo must degrade to "no prompt"
        # like every other unreadable reference — not abort session creation.
        logger.warning("KAS agent: prompt URI %s is malformed", uri)
        return None
    netloc, path = parsed.netloc, parsed.path
    if netloc in ("", "localhost"):
        raw = path
    elif _DRIVE_NETLOC_RE.fullmatch(netloc):
        raw = f"/{netloc}{path}"
    elif netloc in (".", ".."):
        raw = f"{netloc}{path}"
    else:
        raw = f"//{netloc}{path}"

    target = Path(url2pathname(raw))
    if target.is_absolute():
        return str(target)
    if base_dir is None:
        # Refuse rather than fall back to the CWD: the prompt is inlined into the
        # agent's system prompt, so reading the wrong file feeds the model content
        # the operator never pointed at.
        logger.warning("KAS agent: relative prompt URI %s has no base directory", uri)
        return None
    return str(base_dir / target)


def _read_file_uri(uri: str, base_dir: Path | None = None) -> str | None:
    """Resolve a ``file://`` prompt reference to its text, or None on failure.

    Read through ``hooks.safe_read_file_bytes``, NOT ``Path.read_text``: the URI
    comes from an agent config, so a value like ``file:///home/u/.aws/credentials``
    would otherwise put credential contents into the agent's system prompt and
    straight into the model. It canonicalizes first, re-checks the resolved target
    against ``is_sensitive_path`` (so a symlink into a blocked directory is refused
    through the link), and opens with ``O_NOFOLLOW``.

    URI shapes — the Windows spelling, UNC, and relative references — are decided
    by :func:`_file_uri_to_path`, never here.
    """
    resolved_path = _file_uri_to_path(uri, base_dir)
    if resolved_path is None:
        return None
    # Function-local: hooks pulls in the security/policy surface, and acp modules
    # are imported eagerly from acp/__init__, so a module-scope import here risks
    # the same cycle config.loader already documents.
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes

    try:
        blob = safe_read_file_bytes(resolved_path)
    except FileTooLargeError:
        # Size-capped on purpose: a prompt is inlined into session/new, so an
        # oversized file would be read whole into the gateway before the request
        # was even built. Refusing beats an OOM that takes every session with it.
        logger.warning("KAS agent: prompt too large at %s", uri)
        return None
    except (OSError, ValueError, PermissionError):
        logger.warning("KAS agent: prompt unreadable or refused at %s", uri, exc_info=True)
        return None
    if blob is None:
        logger.warning("KAS agent: prompt rejected or unreadable at %s", uri)
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("KAS agent: prompt is not UTF-8 at %s", uri)
        return None


def client_custom_agent(agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Shape one agent config as a KAS ClientCustomAgent.

    Two fields need real translation rather than a copy:

    ``prompt``
        Must be resolved content; KAS rejects a ``file://`` URI here.
    ``tools``
        ``"*"``-or-list in KAS, while kiro-cli splits the grant across ``tools``
        and ``allowedTools``. Only ``tools`` maps — ``allowedTools`` is an
        approval concern Kiro Crew's own PreToolUse gate owns, and forwarding it
        as tool ACCESS would widen what the agent can actually reach.
    """
    out: dict[str, Any] = {"id": agent_id, "prompt": str(config.get("prompt") or "")}
    tools = config.get("tools")
    # An EMPTY list is forwarded, not omitted: it means "no tools" and omitting it
    # would let KAS apply its own broader default access — turning a deliberately
    # narrow grant into a wider one. Only a missing/None value means "unspecified".
    if tools == "*" or isinstance(tools, list):
        out["tools"] = tools
    for key in _PASSTHROUGH_KEYS:
        value = config.get(key)
        if value:
            out[key] = value
    return out


def _resolve_agent_config(name: str, cwd: str | Path | None) -> Path | None:
    """Locate the config declaring *name*, using dispatch's own precedence.

    THE INVARIANT this function exists to hold: agent resolution is delegated to
    ``agent_discovery`` in full — never a path this module builds, never a
    filename this module compares. Three separate defects came from breaking it in
    three different ways (an unguarded open, a global-only lookup, and a stem
    comparison), and each point-fix left the next one reachable.

    Two consequences of the delegation:

    * The DECLARED name wins over the filename. ``project_agent_name`` is what
      kiro-cli itself lists and accepts for ``--agent``, so a renamed or
      package-prefixed agent has a filename that matches nothing.
    * Project-local wins over global, matching ``project_agent_files`` — which
      documents ``<project>/.kiro/agents/*.json`` as the only project location
      kiro-cli resolves ``--agent`` against.

    Precision matters more than it looks: a missing config now REFUSES the session
    rather than silently widening it, so a lookup that misses a legitimate agent
    turns a working setup into a hard failure.
    """
    from kiro_crew.agent_discovery import project_agent_files, project_agent_name

    if cwd:
        for spec in project_agent_files(cwd):
            if project_agent_name(spec) == name:
                return spec
    for spec in sorted(kiro_agents_dir().glob("*.json")):
        if project_agent_name(spec) == name:
            return spec
    return None


def load_client_custom_agent(
    agent: str, cwd: str | Path | None = None
) -> dict[str, Any] | None:
    """Read *agent*'s config and shape it for KAS, or None when unusable.

    Blocking (filesystem + JSON), so callers run it off the event loop.

    Returning None is FAIL-CLOSED at the caller: ``create_session`` raises before
    ``session/new`` rather than sending a request with no definition. Omitting it
    would make KAS fall back to its own default mode, which carries BROADER tool
    access than the configured agent — so "degrading politely" here would be a
    silent privilege widening, not resilience.
    """
    name = (agent or "").strip()
    if not name:
        return None
    path = _resolve_agent_config(name, cwd)
    if path is None:
        logger.warning("KAS agent: no config found for %r", name)
        return None
    # Read through hooks' hardened gate rather than opening the path here: the
    # agents directory is user-writable and shared with other tools, so a config
    # can be a symlink whose RESOLVED target is sensitive (`evil.json` ->
    # `~/.aws/credentials`) or a file large enough to matter. This is the same
    # reader agent_discovery._read_agent_spec uses for the identical reason; the
    # resolve+sensitive check is repeated here because that function is private
    # and has no cross-module callers to follow.
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
    from kiro_crew.security import is_sensitive_path

    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: broken link or permission. RuntimeError: pathlib's signal for a
        # symlink LOOP, one `ln -s` away in a user-writable directory.
        logger.warning("KAS agent: config for %r is not a readable path", name)
        return None
    if is_sensitive_path(str(real)):
        logger.warning("KAS agent: config for %r resolves to a protected path", name)
        return None
    try:
        blob = safe_read_file_bytes(str(real))
    except FileTooLargeError:
        logger.warning("KAS agent: config for %r is oversized", name)
        return None
    except (OSError, ValueError, PermissionError):
        logger.warning("KAS agent: config unreadable for %r", name, exc_info=True)
        return None
    if blob is None:
        logger.warning("KAS agent: config for %r rejected or unreadable", name)
        return None
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("KAS agent: config for %r is not readable JSON", name)
        return None
    if not isinstance(raw, dict):
        logger.warning("KAS agent: config for %r is not an object", name)
        return None
    prompt = raw.get("prompt")
    if isinstance(prompt, str) and prompt.startswith(_FILE_URI_SCHEME):
        # base_dir is the config's OWN directory, so a relative prompt reference
        # tracks the config it is written in rather than the gateway's cwd.
        resolved = _read_file_uri(prompt, real.parent)
        if resolved is None:
            return None
        raw["prompt"] = resolved
    if not str(raw.get("prompt") or "").strip():
        logger.warning("KAS agent: config for %r carries no prompt", name)
        return None
    return client_custom_agent(name, raw)
