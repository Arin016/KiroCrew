"""Built-in security controls — deny list, sensitive path protection, and audit scanning."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from kiro_claw.executors import maintenance_executor
from kiro_claw.sel import SecurityEvent, SecurityEventLog

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ── Built-in Deny Patterns ──
# These are always enforced regardless of user config.
# Patterns use fnmatch (case-insensitive): * matches anything.

BUILTIN_DENY_PATTERNS: list[str] = [
    # Credential / secret access — only explicit secret-fetching tool names.
    # Credential file access is handled by the OS-level sandbox (sandbox.py)
    # which bind-mounts empty dirs over ~/.aws, ~/.gnupg, etc., and by
    # deniedCommands in the kiro-cli agent config.  Broad "*credential*"
    # patterns caused false positives on package names (e.g.
    # CredentialValidatorServiceCDK, credential-rotation-service).
    "get_secret*",
    "read_secret*",
    # Destructive AWS operations
    "*delete_stack*",
    "*terminate_instance*",
    "*drop_table*",
    "*delete_bucket*",
    # NOTE: ``git push`` is NOT a glob here — a broad ``*git*push*`` substring
    # glob over-blocked any command whose text merely contained "push" (e.g. a
    # ``git commit -m`` message mentioning push, or an ``ssh host '...'`` whose
    # remote command did).  It is now matched by the verb-anchored
    # ``_GIT_PUBLISH_*_RE`` regexes below (see ``_is_git_publish``).
]

# Exceptions keyed by the deny pattern they apply to. If an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
# Exceptions are NOT applied when the input contains command separators
# (;, &&, ||, |, newlines) to prevent chaining bypasses.
#
# Currently empty: the only former entry (``git stash push`` excepted from
# ``*git*push*``) is obsolete now that git-publish is detected by a
# verb-anchored regex that never matches ``git stash push`` in the first
# place. The two-pass exception machinery in ``is_denied`` is retained as a
# general mechanism for any future pattern that needs a scoped carve-out.
_DENY_EXCEPTIONS: dict[str, list[str]] = {}

# Used to *split* a command into independently-evaluatable segments.
# Splits on every shell separator that can chain commands or carve out a
# subshell:
#   ;  - sequential
#   |  - pipe (single)
#   || - OR
#   && - AND
#   &  - background operator (when not part of `&&`)
#   $( - subshell open
#   )  - subshell close
#   `  - backtick subshell (open AND close)
#   \n - statement separator in scripts / heredoc bodies
# The alternation is ordered so the multi-character forms (`&&`, `||`) are
# tried before their single-character counterparts (`&`, `|`).  The
# negative lookahead on `&(?!&)` is defensive — it ensures a lone `&`
# doesn't accidentally consume the leading `&` of a literal `&&` if the
# regex engine chose this branch first under some future reordering.
# Literal whitespace is NOT a separator — flag values (e.g. `-C /path`)
# must stay attached to their flag token.
_CMD_SPLIT_RE = re.compile(r"[;\n`]|\|\|?|&&|&(?!&)|\$\(|\)")

# ── Git publish detection (verb-anchored) ──
# ``git push`` must be blocked, but ``push`` appearing anywhere in arbitrary
# command text (a commit message, a branch name, a grep pattern, an ssh remote
# payload) must NOT trip the deny.  We therefore require ``push`` to be the git
# *subcommand* — i.e. the first non-flag/non-option token after ``git`` — rather
# than a substring.  Mirrors the anchored regex in
# ``config/defaults.json`` deniedCommands.
#
# ``git [<-c k=v>...] [<-C path>...] push ...`` is a publish.  Intervening
# tokens may only be options (``-x``) or option-with-value pairs
# (``-C /path``, ``-c core.x=y``) — a bare non-flag token before ``push``
# (e.g. ``stash``) means ``push`` is NOT the subcommand, so ``git stash push``
# is correctly allowed.  Anchored to a segment start (optionally preceded by a
# command separator) so ``git log --grep push`` is not matched.
#
# The trailing terminator is a lookahead that accepts whitespace, end-of-string,
# OR a shell metacharacter that closes/terminates the segment — so a bare
# ``git push`` (no remote/branch, valid: pushes current branch to the default
# remote) is still caught inside ``$(git push)``, `` `git push` ``, ``git push|cat``,
# ``git push&``, etc., not just when followed by a space.
_GIT_PUBLISH_RE = re.compile(
    # ``[^-\s]`` (not ``[^-]``): the optional non-flag arg after a flag must
    # NOT start with whitespace, otherwise inter-token whitespace could be
    # matched either by the preceding ``\s+`` or by this group's leading char —
    # an ambiguity that backtracks exponentially (ReDoS) on whitespace-laden
    # flag runs when the trailing ``push`` is absent.
    r"(?:^|[;&|`\n]|\$\()\s*git\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*push(?=\s|[)`;&|]|$)"
)

# Glue-evasion guard: bash command-substitution / quoting tricks that evaluate
# to ``git push`` but break the token sequence above, e.g.
# ``git$(echo ' ')push``, ``git`echo`push``, ``git$()push``.  After stripping
# empty substitutions/backticks the residue is ``gitpush``; we also match a
# literal ``git_push`` (kiro-cli historically denied that form).
_GIT_PUBLISH_GLUE_RE = re.compile(r"git(?:\$\([^)]*\)|`[^`]*`)+push|git_push")

# Human-readable label recorded in the denial reason + SEL audit event when
# a git-publish invocation is blocked (the regexes above are the mechanism).
_GIT_PUBLISH_DENY_LABEL = "git push"


def _is_git_publish(text_lower: str) -> bool:
    """Return True if *text_lower* invokes ``git push`` (verb-anchored).

    Operates on an already-lowercased string.  Detects both a normal
    ``git ... push`` invocation (where ``push`` is the subcommand) and the
    command-substitution glue-evasion forms.  Does NOT match ``git stash
    push``, ``git commit -m '...push...'``, ``git log --grep push``, etc.
    """
    return bool(_GIT_PUBLISH_RE.search(text_lower) or _GIT_PUBLISH_GLUE_RE.search(text_lower))


# ── Feature-branch push gate ──
# ``_is_git_publish`` only detects that a command IS a ``git push``.  The
# decision of whether to ALLOW it is made by ``_is_push_to_protected_branch``
# at the single enforcement point in ``is_denied``.  The push detector is a
# pure predicate (no side effects); the deny audit (``_emit_deny_event``) and
# the allow audit (``_schedule_push_allow_audit``) are emitted by the caller so
# the SEL trail always reflects the FINAL outcome (never an allow for a command
# that is ultimately denied by a later glob pattern).

# Protected branch names that ``git push`` must never target directly.  A push
# to any of these (or a bare push, which may resolve to one) is blocked so the
# change goes through the normal PR/code-review flow.  KiroClaw (OSS) uses
# ``main``; ``mainline``/``master`` are covered for internal/mirror clones.
_PROTECTED_BRANCHES = {"main", "mainline", "master"}

# Push flags that push EVERY local branch (protected ones included) regardless
# of any explicit refspec, so a per-branch target check cannot vouch for them.
# Presence of any of these denies the push outright (kept in lockstep with the
# ``--(mirror|all)`` regex in config/defaults.json).
_PUSH_ALL_BRANCHES_FLAGS = {"--mirror", "--all"}

# Symbolic refs that resolve at runtime — cannot statically verify safety.
# If the agent is on main and pushes HEAD, it pushes to main on the remote.
_AMBIGUOUS_REFS = {"head", "@", "fetch_head"}

# Refspecs containing shell expansion or git-revision syntax cannot be
# statically verified — deny them as ambiguous.
_AMBIGUOUS_REFSPEC_RE = re.compile(r"[$`]|@\{")

# TRUE shell command separators (NOT command-substitution boundaries). Used to
# scan the PRE-SPLIT text for substitution glued into a push target — see
# ``_is_push_to_protected_branch``.
_CMD_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")

# Shell expansions that fuse text INTO a word, so the literal command hides the
# real push target. Any of these inside a git-publish command is unverifiable
# -> deny (fail closed):
#   - command substitution   $(...)   and backticks  `...`
#   - parameter expansion     ${...}
#   - BRACE expansion         {a,b} / {1..5}  -- bash expands ``ma{i,i}n`` to
#     ``main`` and ``{main,x}`` to ``main x`` BEFORE git sees the token, so a
#     brace group containing a comma or ``..`` must be treated as ambiguous.
_AMBIGUOUS_EXPANSION_RE = re.compile(r"\$\(|\$\{|`|\{[^{}]*(?:,|\.\.)[^{}]*\}")


def _dequote_token(token: str) -> str:
    """Collapse shell quoting/escaping to the literal the shell passes to git.

    bash merges adjacent quoted/unquoted fragments into ONE word, so
    ``ma"in"``, ``m''ain`` and ``ma\\in`` all reach git as the literal
    ``main``. ``str.strip`` removes only the OUTERMOST quotes, leaving interior
    quote/backslash characters that make the token compare unequal to a
    protected name — an evasion of this gate. Remove ALL single/double quotes
    and backslash escapes so the comparison sees the shell-resolved word.
    """
    return token.replace("'", "").replace('"', "").replace("\\", "")


def _git_push_args(segment: str) -> list[str] | None:
    """Return the tokens AFTER the ``push`` subcommand if *segment* is a git push.

    Pure-Python (no regex backtracking — CodeQL ReDoS-safe) replacement for a
    ``\\bpush\\b`` scan. It anchors ``push`` as the git subcommand — the first
    non-flag token after ``git`` — so a segment that merely contains the word
    "push" (e.g. ``echo remember-to-push``) is NOT treated as a push and
    returns None. Skips leading flags, and a single non-flag value that a flag
    may take (e.g. ``-C <path>``) — but never swallows ``push`` itself.
    """
    tokens = segment.split()
    if "git" not in tokens:
        return None
    i = tokens.index("git") + 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 1  # skip the flag
        # A flag may take one separate non-flag value (e.g. ``-C <path>``);
        # never consume the ``push`` subcommand as a flag value.
        if i < len(tokens) and not tokens[i].startswith("-") and tokens[i] != "push":
            i += 1
    if i < len(tokens) and tokens[i] == "push":
        return tokens[i + 1:]
    return None


def _is_protected_branch_name(name: str) -> bool:
    """Return True if *name* is a protected branch or an ambiguous ref."""
    return name in _PROTECTED_BRANCHES or name in _AMBIGUOUS_REFS


def _normalize_ref(ref: str) -> str:
    """Reduce a push destination ref to the bare branch name git resolves it to.

    Git accepts several destination-side spellings that all resolve to the same
    branch server-side: ``main``, ``heads/main``, ``refs/heads/main``,
    ``remotes/<remote>/main``, ``refs/remotes/<remote>/main``. Stripping only
    ``refs/heads/`` let ``heads/main`` and the ``remotes/`` forms dodge the
    protected-name check (they still resolve to a protected branch on the
    server). Normalize every spelling to the bare name so the comparison cannot
    be evaded by ref-path spelling.
    """
    ref = ref.removeprefix("refs/")
    if ref.startswith("remotes/"):
        parts = ref.split("/", 2)  # remotes/<remote>/<branch>
        if len(parts) == 3:
            return parts[2]
    return ref.removeprefix("heads/")


def _push_segment_targets_protected(arg_tokens: list[str]) -> bool:
    """Return True if a single push's argument tokens target protected/bare.

    *arg_tokens* are the tokens following the ``push`` subcommand within ONE
    shell segment (separators already removed).  A bare push (no explicit
    branch) is treated as protected because the current branch might be a
    protected one.  Force flags (``--force``/``-f``/``--force-with-lease``)
    do NOT by themselves make a feature-branch push protected — force-push to
    a feature branch is a normal PR/rebase workflow — but a force-push to a
    protected branch is still blocked, because the target check below fires
    regardless of any flags (force flags are stripped before the check).
    """
    tokens = [_dequote_token(t) for t in arg_tokens]
    # Deny-by-default: flags that push ALL local branches (protected ones
    # included) bypass any per-branch target check. Detect them BEFORE
    # stripping flags and deny outright, so the always-on gate never relies on
    # the secondary regex layer for this case.
    if any(tok in _PUSH_ALL_BRANCHES_FLAGS for tok in tokens):
        return True
    # Skip flags (tokens starting with -); non_flags[0] is the remote and
    # non_flags[1:] are the refspecs/branches.
    non_flags = [t for t in tokens if t and not t.startswith("-")]
    if len(non_flags) < 2:
        # Bare ``push`` or ``push <remote>`` with no explicit branch — the
        # current branch might be protected, so deny.
        return True
    for refspec in non_flags[1:]:
        # Refspecs with shell expansion ($, `) or git-revision syntax
        # (@{upstream}, @{u}) cannot be statically verified — deny.
        if _AMBIGUOUS_REFSPEC_RE.search(refspec):
            return True
        clean = refspec.lstrip("+")  # strip force-push '+' ref prefix
        # Wildcard refspec (refs/heads/*:refs/heads/*, *:*, feat*) expands to
        # MANY refs — like --mirror/--all it can include a protected branch and
        # cannot be statically verified. Deny.
        if "*" in clean:
            return True
        # Handle "local:remote" refspec format — the remote side is the target.
        target_branch = clean.split(":")[-1] if ":" in clean else clean
        # Normalize every ref spelling git resolves server-side (heads/main,
        # remotes/<remote>/main, refs/... ) to the bare name so the path form
        # cannot dodge the protected-name check.
        if _is_protected_branch_name(_normalize_ref(target_branch)):
            return True
    return False


def _is_push_to_protected_branch(text_lower: str) -> bool:
    """Return True if ANY ``git push`` in the command targets a protected branch.

    A bare ``git push`` (no explicit branch) is BLOCKED because the current
    branch might be main/mainline. Only explicit non-protected branch targets
    are allowed. ALL refspecs of ALL push sub-invocations are checked: git
    accepts multiple refspecs, and a shell command can chain multiple pushes
    (``push origin feat && push origin main``). Force pushes to feature
    branches are allowed (normal PR workflow); force pushes to protected
    branches are blocked by the target check.

    Iterates the command's TRUE shell segments (split only on ``;`` / ``&&`` /
    ``||`` / ``|`` / newline — NOT on ``$(`` / backtick, which are glued into a
    single word by the shell). Each segment that is a git-publish (detected via
    ``_is_git_publish``, so glue-evasion like ``git$(echo ' ')push`` is seen) is
    validated and FAILS CLOSED:

    * any command-substitution / brace-expansion / backtick glue in the segment
      — in the verb OR the target (``origin ma$(echo)in`` -> ``main``) — is
      unverifiable -> deny;
    * a segment that ``_is_git_publish`` flags as a push but ``_git_push_args``
      cannot cleanly parse (obfuscated) -> deny;
    * a bare push, ambiguous ref, or explicit protected target -> deny.

    Only an explicit non-protected branch target is allowed. EVERY push segment
    is checked (a benign feature push cannot vouch for a sibling protected one).
    Force pushes to feature branches stay allowed (normal PR workflow). If a
    push was detected upstream but no segment here parses as one, denies.
    """
    saw_push = False
    for command in _CMD_SEPARATOR_RE.split(text_lower):
        # ``_is_git_publish`` (not ``_git_push_args``) gates the checks so that
        # glue-evasion forms — which do NOT tokenize to a clean ``git`` token —
        # are still recognized as pushes and cannot slip past the ambiguity /
        # fail-closed guards below.
        if not _is_git_publish(command):
            continue
        saw_push = True
        # Substitution / expansion glue anywhere in a push command makes it
        # unverifiable (the shell fuses it into the verb or the target word).
        if _AMBIGUOUS_EXPANSION_RE.search(command):
            return True
        args = _git_push_args(command)
        if args is None:
            # Detected as a push but not cleanly parseable (obfuscated) — deny.
            return True
        if _push_segment_targets_protected(args):
            return True
    if not saw_push:
        # A push was detected upstream (e.g. glue-evasion ``git_push``) but no
        # clean ``push`` segment survived splitting — deny to be safe.
        return True
    return False


def _schedule_push_allow_audit(command: str) -> None:
    """Fire-and-forget audit write offloaded to the maintenance executor.

    Avoids blocking the event loop on file I/O (same concern as
    ``_emit_deny_event`` — both should be offloaded per the
    no-blocking-call-on-event-loop guideline).  Falls back to an inline
    synchronous write when no event loop is running (sync/test contexts).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(maintenance_executor(), _emit_push_allow_event, command)
    except RuntimeError:
        # No running loop (called from a sync test or non-async context) —
        # fall back to inline write (acceptable: no event loop to block).
        _emit_push_allow_event(command)


def _emit_push_allow_event(command: str) -> None:
    """Emit a SEL audit event when a feature-branch push is allowed through.

    Best-effort: an audit failure is logged at WARNING and does not affect the
    allow decision (the push already passed the protected-branch gate).
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="push_allowed",
                caller_identity="",
                agent="kiroclaw",
                source="security",
                operation="git_push",
                outcome="allowed",
                resources="feature_branch_push",
                metadata={
                    "command": command[:200],
                    "mechanism": "BRANCH_GATE",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for push_allowed (allow stands)",
            exc_info=True,
        )


# ── Sensitive Paths ──
# Directories and files that must never be read by the agent.
# Patterns are resolved relative to $HOME at check time.

_SENSITIVE_HOME_DIRS: list[str] = [
    ".aws",
    ".ssh",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker/config.json",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".kiroclaw/.env",
    # Governance trust-root files (KEYSTONE of the two-level governance model).
    # Under "secure by default, not by mandate" the ONLY thing preventing a
    # prompt-injected agent from rewriting its own ceiling is that it cannot
    # WRITE these files — and is_sensitive_path() is the shared read+write gate
    # (hooks.on_tool_call, validate_file_path, artifacts) across every surface.
    # The agent (the governed subject) is blocked; the operator (trust root)
    # edits them out-of-band.  admission_policy.json is the existing plugin
    # trust root; security_policy.json + profiles/ are the new governance ones.
    ".kiroclaw/security_policy.json",
    ".kiroclaw/profiles",
    ".kiroclaw/admission_policy.json",
]

# ── Write-protected paths (block modification, allow reads) ──
# Runtime config files carry security-relevant resource ceilings (concurrent
# subagents, per-agent turn budget, warm-pool size). A prompt-injected agent
# with file-write access must not be able to rewrite these to inflate its own
# limits and drive host resource exhaustion (pentest — config-loader bound
# bypass, recommendation: block agent tools from modifying config files).
#
# They are DELIBERATELY NOT in ``_SENSITIVE_HOME_DIRS`` above: that list is the
# shared read+write gate, and reading config.json is routine and intended (the
# dashboard file viewer, ``cat``, and knowledge indexing all read it). We
# instead block only WRITES, at the agent file-edit tool gate
# (hooks.on_tool_call), via ``is_sensitive_write_path``. This is defense in
# depth on top of the loader's load-time clamp, which already neutralizes any
# inflated on-disk value no matter how it was written. The operator edits config
# out-of-band (dashboard config API / CLI), which do NOT route through this
# gate, so legitimate config changes still work.
_WRITE_PROTECTED_HOME_PATHS: list[str] = [
    ".kiroclaw/config.json",
    ".kiroclaw/config.local.json",
]

# Regex for bash commands that read sensitive paths.
# Matches: cat, head, tail, less, more, strings, xxd, base64, cp, scp, open
# followed by a path containing any sensitive dir.
_READ_CMDS = r"(?:cat|head|tail|less|more|strings|xxd|base64|cp|scp|open|vi|vim|nano|code)\s"

# Regex for bash commands that WRITE/MODIFY a path argument.  Reads alone were
# not enough: a prompt-injected agent could rewrite the governance trust-root
# (or plant a credential) with a write verb that carries no redirect char and
# is not a read verb — e.g. ``tee ~/.kiroclaw/security_policy.json``,
# ``mv evil ~/.kiroclaw/profiles/x.json``, ``sed -i ... ~/.aws/credentials``,
# ``dd of=...``, ``truncate``, ``ln -sf``, ``install``, plus archive-extraction
# and VCS-checkout verbs that materialise a file at a destination
# (``tar -xf … -C``, ``unzip -d``, ``git checkout/restore -- <path>``).  This
# list is defense-in-depth; the verb-independent catch-all below is the real
# backstop, so a write verb we forgot is still caught when it names a
# sensitive path as an argument.
# NOTE: ``git`` is narrowed to the verbs that actually MATERIALISE a file —
# a bare ``git`` would over-block read-only inspection (``git log/status/diff/
# show/blame/grep -- <sensitive path>``) that operators run during incident
# triage. The verb-independent catch-all still flags a sensitive-path token
# regardless of git verb, so this only trims false positives (CR-284272012).
_WRITE_CMDS = (
    r"(?:tee|mv|dd|truncate|ln|install|sed|chmod|chown|rm|rmdir|touch|mkdir|rsync"
    r"|tar|unzip|gunzip|gzip|cpio|patch"
    r"|git\s+(?:checkout|restore|reset|apply|clean|rm|mv|stash))\s"
)

# Matches python/ruby/perl one-liners that open sensitive paths
_SCRIPT_OPEN = r"(?:python|ruby|perl)\S*\s.*open\s*\("


def _build_sensitive_regex() -> re.Pattern[str]:
    """Build a compiled regex matching bash reads OR writes of sensitive paths.

    Three matching strategies, OR'd:
      1. a READ verb / WRITE verb / script-open / shell-redirect followed by a
         sensitive path (the original verb-anchored form);
      2. a verb-INDEPENDENT catch-all: a sensitive path appearing ANYWHERE in
         the command as an argument token.  This is the real backstop — a write
         verb the allowlist forgot (or a novel one) is still blocked because the
         destination path is sensitive.  Reading a sensitive path is itself
         already blocked by is_sensitive_path on the file-read title, so flagging
         any command that *names* the trust-root/credential path is correct and
         fail-safe.
    The home anchor accepts ``~`` / ``$HOME`` / the literal ``Path.home()`` AND a
    generic ``/home/<user>`` / ``/Users/<user>`` literal so an unexpanded
    ``/home/$USER/...`` or another user's literal path is still caught.
    """
    home = re.escape(str(Path.home()))
    tilde = re.escape("~")
    home_var = re.escape("$HOME")
    # Generic home roots so a literal "/home/<user>" or "/Users/<user>" token
    # (not just the running user's resolved home) is anchored too.
    generic_home = r"/home/[^/\s]+|/Users/[^/\s]+"
    home_alts = f"(?:{home}|{tilde}|{home_var}|{generic_home})"
    escaped_dirs = [re.escape(d) for d in _SENSITIVE_HOME_DIRS]
    dirs_pattern = "|".join(escaped_dirs)
    sensitive_path = rf"{home_alts}/(?:{dirs_pattern})(?:/|\s|$|['\"])"
    return re.compile(
        # (1) verb/redirect-anchored, OR (2) verb-independent: the sensitive path
        # appears anywhere as a token.  The token anchor accepts start-of-string
        # plus the separators that precede a path argument: whitespace, quote,
        # ``=`` (VAR=path), AND ``:``/``,``/``;`` (option:path, PATH-style
        # colon lists, comma/semicolon-joined args) — without the latter a
        # ``FOO=bar:~/.aws/credentials`` or ``PATH=/x:~/.ssh/id_rsa`` token slips
        # past the backstop while no verb branch fires either (CR-284272012).
        rf"(?:(?:{_READ_CMDS}.*|{_WRITE_CMDS}.*|{_SCRIPT_OPEN}.*|.*[<>|]\s*)"
        rf"{sensitive_path}"
        rf"|(?:^|.*[\s'\"=:,;]){sensitive_path})",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


def _path_in_home_dirs(
    path_str: str, home_dirs: list[str], base_dir: str | None = None
) -> bool:
    """Return True if *path_str* resolves under any of *home_dirs* (``$HOME``-relative).

    Shared matching core for :func:`is_sensitive_path` (read+write gate,
    ``_SENSITIVE_HOME_DIRS``) and :func:`is_sensitive_write_path` (write-only
    gate, the read+write set PLUS ``_WRITE_PROTECTED_HOME_PATHS``). Keeping one
    implementation means the symlink/casefold hardening below cannot drift
    between the two gates.

    ── Symlink robustness (pentest AWS-345 / AWS-62) ──
    A workspace symlink pointing at ``~/.aws/credentials`` (absolute OR relative
    ``../../.aws/credentials`` traversal) must NOT be readable through the link.
    We therefore check MULTIPLE candidate forms of the input and return True if
    ANY of them lands in a matched location:

      1. the fully symlink-RESOLVED canonical target (``realpath`` /
         ``Path.resolve`` — follows every symlink in the chain, including
         intermediate directories and the final component).  This is what
         defeats the symlink bypass: the resolved target of the link is
         ``~/.aws/credentials`` even though the link's own name is benign.
      2. the LEXICALLY-normalized path (no symlink following) and the raw
         expanded string — so a path that *textually* names a matched dir is
         still caught when resolution fails (dangling link, permission error).

    ``base_dir`` anchors a *relative* input against the caller's known working
    directory (e.g. the agent's workspace cwd) so a relative title like
    ``sub/cfg.ini`` resolves against the real directory rather than whatever CWD
    the gateway process happens to have.  Absolute inputs are unaffected;
    ``base_dir=None`` preserves the historical CWD-relative behavior.
    """
    if not path_str:
        return False

    # Expand ~ and $HOME
    expanded = os.path.expanduser(os.path.expandvars(path_str))

    # Anchor a relative input against the supplied workspace dir so it resolves
    # to the real file rather than the gateway's CWD.
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)

    # Build the candidate forms.  Symlink-resolved forms defeat a link bypass;
    # the lexical forms are the fail-safe fallback when resolution cannot
    # complete (over-matching a sensitive-looking path is the safe direction).
    candidates: set[str] = set()
    try:
        candidates.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        candidates.add(str(Path(expanded).resolve()))
    except (OSError, ValueError, RuntimeError):
        pass
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)

    try:
        home = str(Path.home().resolve())
    except (OSError, ValueError):
        home = str(Path.home())
    # Compare against the sensitive dirs anchored at BOTH the logical home and
    # its realpath.  On macOS the per-user temp/home prefix can itself be
    # reached via OS symlinks (``/var`` → ``/private/var``); folding both roots
    # in means a resolved candidate under either spelling is still matched.
    sensitive_targets: set[str] = {
        os.path.join(home, d).casefold() for d in home_dirs
    }
    home_real = os.path.realpath(home)
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {
            os.path.join(home_real, d).casefold() for d in home_dirs
        }

    # Case-fold both sides for the membership test.  On a case-insensitive
    # filesystem (macOS APFS/HFS+ default — a supported platform) the OS opens
    # ``~/.kiroclaw/Security_Policy.json`` and ``~/.kiroclaw/security_policy.json``
    # as the SAME file, so a byte-exact comparison would let the agent write its
    # own governance ceiling via an alternate-case path. Folding is strictly more
    # protective (it can only ever over-match an alternate-case variant of an
    # already-sensitive path, which is itself suspicious), so it is safe on
    # case-sensitive Linux too — matching the IGNORECASE bash-read matcher.
    for cand in candidates:
        cand_cf = cand.casefold()
        for sensitive_path in sensitive_targets:
            if cand_cf == sensitive_path or cand_cf.startswith(sensitive_path + os.sep):
                return True
    return False


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.
    """
    return _path_in_home_dirs(path_str, _SENSITIVE_HOME_DIRS, base_dir)


def is_sensitive_write_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path must not be MODIFIED by an agent tool.

    Superset of :func:`is_sensitive_path`: everything that is read+write blocked
    PLUS the write-only-protected runtime config files
    (:data:`_WRITE_PROTECTED_HOME_PATHS`), which stay readable but must not be
    written by the agent. Enforced at the file-edit tool gate
    (``hooks.on_tool_call`` on the ACP ``edit`` kind) — see
    :data:`_WRITE_PROTECTED_HOME_PATHS` for the rationale.
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir
    )


# Archive/extraction destination flags (tar -C, unzip -d, rsync dest) pointing
# INTO the governance trust-root parent ``~/.kiroclaw`` — an extraction there can
# drop/overwrite ``security_policy.json`` or a ``profiles/`` entry even though the
# bare ``~/.kiroclaw`` dir is not itself a sensitive-path entry.  Match the
# destination-dir form specifically so normal ``~/.kiroclaw`` access (sessions.db,
# config.json) is not over-blocked.
_EXTRACT_INTO_TRUST_ROOT_RE = re.compile(
    r"-(?:C|d)\s+(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|"
    + re.escape(str(Path.home()))
    + r")/\.kiroclaw(?:/[^\s]*)?(?:\s|$|['\"])",
    re.IGNORECASE,
)

# ── Symlink-staging to a sensitive target via RELATIVE traversal ──
# The home-anchored ~/$HOME/absolute forms of ``ln -sf ~/.aws/credentials link``
# are already caught by _build_sensitive_regex (the sensitive path appears as an
# argument token).  What that matcher CANNOT see is a sensitive dir named through
# pure relative traversal — ``ln -sf ../../../.aws/credentials link`` — because
# it has no home anchor.  Creating such a symlink is the staging step of the
# pentest attack chain (AWS-345 / AWS-62, recommendation item 3): a pre-existing
# link to a credential file lets a later in-workspace read follow it.  We block
# the CREATION verbs (``ln``, ``cp -s``/``--symbolic-link``) when any token
# names a sensitive dir via dot-slash traversal.
_SENSITIVE_SEGMENT_ALT = "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS)
_RELATIVE_SENSITIVE_RE = re.compile(
    rf"(?:^|[\s'\"=:,;])(?:\.\.?/)+(?:{_SENSITIVE_SEGMENT_ALT})(?:/|\s|$|['\"])",
    re.IGNORECASE,
)
# Segment-anchored symlink-creation verbs: ``ln`` (any flags) or ``cp`` (the
# ``-s``/``--symbolic-link`` form is what makes cp create a link; we accept any
# ``cp`` here since it is paired with the relative-sensitive match, so a plain
# ``cp`` of an unrelated file never trips this).
_SYMLINK_CREATE_VERB_RE = re.compile(r"(?:^|[;&|`\n]|\$\()\s*(?:ln|cp)(?:\s|$)", re.IGNORECASE)


def is_sensitive_bash_command(command: str) -> str | None:
    """Check if a bash command reads OR writes sensitive paths.

    Returns denial reason string, or None if clean.
    """
    if _get_sensitive_re().search(command):
        return "Blocked: command accesses sensitive credential path"
    if _EXTRACT_INTO_TRUST_ROOT_RE.search(command):
        return "Blocked: command extracts into the governance trust-root directory"
    # Staging a symlink to a credential file via relative traversal (the
    # home-anchored/absolute forms are already blocked by the matcher above).
    if _SYMLINK_CREATE_VERB_RE.search(command) and _RELATIVE_SENSITIVE_RE.search(command):
        return "Blocked: command stages a symlink to a sensitive credential path"
    return None


# ── URL Exfiltration Detection ──
# Detects URLs whose query strings contain credential-like data.
# Domain-agnostic: we flag the PAYLOAD, not the destination.
# Any URL with secrets in query params is suspicious regardless of domain.

_URL_RE = re.compile(r"https?://([a-zA-Z0-9._-]+\.[a-zA-Z]{2,})(:\d+)?(/[^\s)\"'>]*)?")

# Query string length threshold — normal URLs rarely exceed this
_EXFIL_QUERY_MIN_LEN = 200

# Patterns that indicate secrets or encoded data in query params
_EXFIL_PATTERNS = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/=]{40,}"  # base64-like blob (40+ chars)
    r"|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}"  # heavy URL-encoding (20+ encoded chars)
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    r".*X-Amz-Credential=(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)"
    r".*X-Amz-Expires=\d{1,6}"
    r".*X-Amz-Signature=[0-9a-f]{64}",
    re.IGNORECASE,
)

# Only these parameter keys are allowed in a presigned URL.  Any extra
# keys cause the fast-path to reject, falling through to normal checks.
_S3_PRESIGNED_PARAMS = frozenset({
    "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
    "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature",
    "X-Amz-Security-Token",
})


# Structural validators for presigned param values that would otherwise
# false-positive against _EXFIL_PATTERNS.  Each value is validated rather
# than exempted, so attacker-controlled data cannot be smuggled through.
_STS_TOKEN_RE = re.compile(
    r"^(?:FwoGZX|IQoJb3JpZ2lu)[A-Za-z0-9+/=%]{1,2000}$"
)
_CREDENTIAL_RE = re.compile(
    r"^(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)[0-9]{8}"
    r"(?:%2F|/)[a-z0-9-]+(?:%2F|/)s3(?:%2F|/)aws4_request$"
)
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

_STRUCTURAL_VALIDATORS = {
    "X-Amz-Credential": _CREDENTIAL_RE,
    "X-Amz-Signature": _SIGNATURE_RE,
    "X-Amz-Security-Token": _STS_TOKEN_RE,
}


def _is_safe_presigned(domain: str, query: str) -> bool:
    """Return True if the URL is a valid S3 presigned URL with no extra parameters."""
    if not domain.endswith(".amazonaws.com"):
        return False
    if not _S3_PRESIGNED_RE.search(query):
        return False
    params = parse_qs(query, keep_blank_values=True)
    if not _S3_PRESIGNED_PARAMS.issuperset(params.keys()):
        return False
    # Structurally validate params that would false-positive against
    # _EXFIL_PATTERNS.  No values are fully exempt — each is checked.
    for key, values in params.items():
        validator = _STRUCTURAL_VALIDATORS.get(key)
        if validator:
            for val in values:
                if not validator.match(val):
                    return False
        else:
            for val in values:
                if _EXFIL_PATTERNS.search(val):
                    return False
    return True


def scan_exfiltration_urls(text: str) -> list[str]:
    """Scan text for URLs that may be exfiltrating data via query params.

    Domain-agnostic — only inspects query string content for secret patterns.
    Returns list of warning strings, empty if clean.
    """
    warnings: list[str] = []
    for match in _URL_RE.finditer(text):
        domain = match.group(1)
        path_and_query = match.group(3) or ""
        qmark = path_and_query.find("?")
        if qmark == -1:
            continue

        query = path_and_query[qmark + 1 :]

        if len(query) >= _EXFIL_QUERY_MIN_LEN:
            # S3 presigned URLs on amazonaws.com have long queries but are safe
            if _is_safe_presigned(domain, query):
                continue
            warnings.append(
                f"Suspicious URL with long query params ({len(query)} chars): "
                f"{domain}{path_and_query[:60]}..."
            )
        elif _EXFIL_PATTERNS.search(query):
            # S3 presigned URLs on amazonaws.com match the blob pattern but are safe
            if _is_safe_presigned(domain, query):
                continue
            warnings.append(f"Suspicious URL with credential-like query data: {domain}")
    return warnings


def redact_exfiltration_urls(text: str) -> tuple[str, list[str]]:
    """Scan and redact suspicious exfiltration URLs from text.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings = scan_exfiltration_urls(text)
    if not warnings:
        return text, []

    result = text
    for match in _URL_RE.finditer(text):
        domain = match.group(1)
        full_url = match.group(0)
        path_and_query = match.group(3) or ""
        qmark = path_and_query.find("?")
        if qmark == -1:
            continue

        query = path_and_query[qmark + 1 :]

        if len(query) >= _EXFIL_QUERY_MIN_LEN or _EXFIL_PATTERNS.search(query):
            # S3 presigned URLs on amazonaws.com are safe — don't redact
            if _is_safe_presigned(domain, query):
                continue
            result = result.replace(full_url, f"[REDACTED: suspicious URL to {domain}]")

    return result, warnings


# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().

_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    # key-value forms: tolerate an optional closing quote after the key name and an
    # optional opening quote before the value so JSON (`"aws_secret_access_key": "v"`)
    # is redacted, not just bare `key=v` / `key: v`. Without the `["']?` the closing
    # quote in JSON sits between the key and `:` and defeats the match → secret leaks.
    # The value class is [^\s"',}]+ (NOT \S+): \S+ is greedy and, in compact JSON
    # like {"aws_secret_access_key":"SECRET","region":"x"}, swallows everything
    # through the closing brace (`"`, `,`, `}` all match \S) — destroying adjacent
    # fields and consuming a following credential key so it's never matched/counted.
    # Stopping at JSON structural delimiters bounds the value while still matching
    # bare key=value forms.
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r"|BEGIN[\s](?:RSA|DSA|EC|OPENSSH)[\s]PRIVATE[\s]KEY"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    # Telegram bot token: ``<bot_id>:<secret>`` — bot_id is 6+ digits, secret is
    # ~35 URL-safe base64 chars. The ``{30,}`` floor sits deliberately below the
    # real length so shortened/rotated test tokens are still caught. Analogue to
    # the Slack token above. Telegram tokens can live in ``config.json``
    # (agent-readable), so an echoed config would otherwise leak a full
    # bot-control credential unredacted. The value class ``[A-Za-z0-9_-]`` stops
    # at structural delimiters (space, quote, comma, brace), so it can't swallow
    # adjacent fields; over-redacting a rare ``digits:token`` lookalike is the
    # safe direction.
    r"|[0-9]{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    # ── Third-party developer credentials (AWS-345 / AWS-59) ──
    # Distinctive, fixed-case prefixes → very low false-positive risk.  Minimum
    # lengths are kept slightly below the real token lengths so shortened test /
    # rotated variants are still redacted (over-redaction on a prefix match is the
    # safe direction).  Case-sensitive by design (these prefixes are issued in a
    # fixed case); do NOT fold — folding would broaden false positives.
    r"|gh[opsur]_[A-Za-z0-9]{30,255}"  # GitHub PAT (ghp_) + oauth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab PAT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # SendGrid API key
    r"|sk-proj-[A-Za-z0-9_-]{16,}"  # OpenAI project key
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic API key
    r"|npm_[A-Za-z0-9]{24,}"  # npm access token
    r"|pypi-[A-Za-z0-9_-]{16,}"  # PyPI API token
    r"|do[opr]_v1_[A-Za-z0-9]{40,}"  # DigitalOcean PAT/OAuth/refresh
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"  # Google OAuth client secret
    # DB connection URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here).
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?)"
    r"://[^\s:/@]+:[^\s/@]+@"
    r")",
)


def get_credential_patterns() -> list[re.Pattern[str]]:
    """Public accessor for the canonical credential regexes.

    Lets other modules (e.g. deploy-web's pre-publish content scan) reuse the
    same patterns without coupling to the private ``_CREDENTIAL_PATTERNS`` name,
    so a future rename here can't silently turn a downstream scan into a no-op.
    Returns a list so callers can iterate uniformly; the fork keeps a single
    combined compiled regex, so the list has one element.
    """
    return [_CREDENTIAL_PATTERNS]


# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''."""
    import base64

    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _CREDENTIAL_PATTERNS.search(decoded):
                return decoded
        except Exception:
            continue
    return ""


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings: list[str] = []
    result = text

    # 1. Redact plaintext credential patterns
    for m in _CREDENTIAL_PATTERNS.finditer(result):
        matched = m.group()
        tag = "[REDACTED: credential]"
        result = result.replace(matched, tag, 1)
        warnings.append(f"Redacted credential pattern: {matched[:20]}...")

    # 2. Detect and redact base64-encoded credentials
    for m in _B64_CHUNK_RE.finditer(text):
        chunk = m.group()
        decoded = _decode_b64_safe(chunk)
        if decoded:
            result = result.replace(chunk, "[REDACTED: encoded credential]", 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    return result, warnings


# Suspicious bash patterns to flag during audit
SUSPICIOUS_BASH_PATTERNS: list[str] = [
    "curl * | bash",
    "curl * | sh",
    "wget * | bash",
    "| bash",
    "| sh",
    "| python",
    "| perl",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "find * -delete",
    "find * -exec rm",
    "find * -exec shred",
    "xargs rm",
    "git clean -f",
    "shred ",
    "truncate ",
    "> /dev/sd",
    "mkfs.",
    "dd if=",
    "chmod 777",
    "chmod */usr/",
    "chmod */etc/",
    "chmod */sbin/",
    "chmod */boot/",
    "chmod */lib/",
    "chmod */lib64/",
    "chown */usr/",
    "chown */etc/",
    "chown */sbin/",
    "chown */boot/",
    "chown */lib/",
    "chown */lib64/",
    "eval $(",
    "base64 -d",
    "nc -e",
    "ncat -e",
    "/dev/tcp/",
    "xp_cmdshell",
    "GRANT ALL",
    "DROP DATABASE",
    "DROP TABLE",
    "TRUNCATE TABLE",
    "aws iam create-access-key",
    "aws sts assume-role",
    "export AWS_SECRET",
    "export AWS_ACCESS",
    "curl * -d @",
    "curl * --data @",
    "curl * -F file=@",
    "curl -d @",
    "curl --data @",
    "curl -F file=@",
    "wget --post-file",
    "nc * < ",
]

# ── Binary File MIME Allowlist ──
# Files whose UTF-8 decode fails are accepted by file_send / outbox only when
# their guessed MIME type is in this allowlist. Deny-by-default; expand only
# when a use case is reviewed for safe rendering on the dashboard origin.
# SVG is intentionally excluded (can carry inline scripts); served as
# attachment by the download handler for defense-in-depth.
BINARY_MIME_ALLOWLIST: frozenset[str] = frozenset({
    "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/flac",
    "audio/aac", "audio/mp4", "audio/webm", "audio/opus",
    "video/mp4", "video/webm", "video/ogg",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "application/pdf",
})


def redact(text: str) -> str:
    """Apply all redaction passes (exfiltration URLs + credentials)."""
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    return text


# ── Streaming redaction (pentest issue 3) ──
# Per-chunk redaction misses a credential split across token/streaming
# boundaries: a chunk ending ``...AKIA`` and the next starting ``IOSFODNN7...``
# each individually escape redact_credentials(), so the raw fragments reach
# WebSocket/SSE consumers even though the final assembled message is redacted.
# StreamRedactor withholds the trailing run of "credential-class" characters
# (which could be the start of a not-yet-complete credential) until a
# terminator arrives or the stream ends, redacting only the confirmed-safe
# prefix before it is emitted on the wire.

# Characters that can appear inside a credential token/pattern. A credential is
# a contiguous run of these; any byte OUTSIDE this set terminates an in-progress
# match, so text up to (and including) such a terminator is safe to redact and
# emit. Includes URL / base64 / connection-string punctuation so exfil URLs and
# DB URIs are also held intact across chunk boundaries. (The private-key HEADER
# phrase contains spaces and is the one pattern that can split on a terminator;
# it is a non-secret header string and the final full-text pass still redacts
# the persisted/displayed copy.)
_CRED_CLASS: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "_-+/=.:@%~"
)

# Upper bound on withheld trailing characters. Larger than the longest
# fixed-format credential so a split token is always rejoined before emission;
# bounds latency/memory for a pathologically long unbroken run (only affects a
# single >512-char secret with no delimiter, which no supported provider issues).
_STREAM_HOLDBACK_MAX = 512


class StreamRedactor:
    """Rolling-buffer redactor for streamed LLM output.

    Feed raw chunks in order; ``feed`` returns the redacted, safe-to-broadcast
    prefix (possibly empty while a partial credential is buffered). Call
    ``flush`` when the stream/segment ends to redact and return the remainder.
    Adds at most one chunk of latency. A credential is never split across a
    commit boundary because commits only ever end at a non-credential-class
    character, while a credential is a contiguous credential-class run.
    """

    __slots__ = ("_buf", "_redact")

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact

    def feed(self, chunk: str) -> str:
        """Accept a chunk; return the redacted prefix that is safe to emit now."""
        if not chunk:
            return ""
        self._buf += chunk
        # Start of the maximal trailing credential-class run.
        i = len(self._buf)
        while i > 0 and self._buf[i - 1] in _CRED_CLASS:
            i -= 1
        # Cap the withheld tail so an unbroken run can't grow without bound.
        if len(self._buf) - i > _STREAM_HOLDBACK_MAX:
            i = len(self._buf) - _STREAM_HOLDBACK_MAX
        if i <= 0:
            return ""  # whole buffer is a (possibly partial) credential run — hold
        commit, self._buf = self._buf[:i], self._buf[i:]
        return self._redact(commit)

    def flush(self) -> str:
        """Redact and return the buffered remainder; clears the buffer."""
        out = self._redact(self._buf) if self._buf else ""
        self._buf = ""
        return out

    def reset(self) -> None:
        """Discard the buffer without emitting (segment abandoned/cleared)."""
        self._buf = ""


def is_denied(tool_name: str, extra_patterns: list[str] | None = None) -> str | None:
    """Check tool name against built-in + extra deny patterns.

    Returns denial reason string, or None if allowed.

    ── Two-pass evaluation ──
    Pass 1 (whole-string): every deny pattern is matched against the
    full input.  If a pattern matches and **no exception pattern also
    matches the full input**, the input is denied immediately.  This
    closes evasion vectors where the deny string spans a separator
    boundary that per-segment splitting would erase, e.g.
    ``git$(echo ' ')push origin main`` (which bash evaluates to
    ``git push origin main``): the whole string contains both ``git`` and
    ``push`` so the broad ``*git*push*`` glob matches, and there is no
    matching exception, so the command is denied at this stage even
    though splitting on ``$(`` / ``)`` would otherwise produce no
    segment containing both substrings.

    Pass 2 (per-segment) only runs if pass 1 found a deny match **and**
    the full input also matched at least one exception for that pattern.
    The input is split on shell command separators (``;``, ``&&``,
    ``||``, ``|``, newlines) and command-substitution boundaries
    (``$(``, ``)``, backticks) into segments, and each segment is
    re-evaluated independently.  This preserves the chaining-bypass
    protection (any embedded real
    publish lives in its own segment and matches the deny pattern in its
    own right) while allowing the legitimate stash-in-pipeline case
    that the prior whole-string design over-blocked.

    Edge cases & limitations:
      - Pass-1 deny is conservative: anything matching a deny glob with
        no exception is blocked, even if the input is structurally
        contorted.
      - Pass-2 splitting is purely textual; quoted strings and escaped
        separators are split anyway (over-blocking is the safer
        direction).
      - Heredoc bodies, ``eval``, ``bash -c``, etc., are not parsed
        specially.  If those become evasion vectors in practice, add
        explicit deny patterns for them.

    Audit:
      - Every denial path emits a ``deny_event`` SEL event via
        ``_emit_deny_event``.
      - Every granted exception emits a ``deny_exception`` SEL event via
        ``_emit_deny_exception_event`` (fail-closed: if SEL logging
        fails the exception is not granted).

    Args:
        tool_name: The full command line / tool invocation to evaluate.
        extra_patterns: Optional fnmatch glob patterns to append to the
            built-in deny list (typically from user config).

    Returns:
        Denial reason string (mentioning the matched pattern), or
        ``None`` if the input is allowed.
    """
    lower = tool_name.lower()
    all_glob_patterns = BUILTIN_DENY_PATTERNS + (extra_patterns or [])

    # ── Git publish (verb-anchored, not a glob) ──
    # Checked on the whole string first so command-substitution glue-evasion
    # (e.g. ``git$(echo ' ')push``) is caught even though splitting on ``$(``
    # / ``)`` would otherwise scatter the ``git``/``push`` tokens across
    # segments.  ``_is_git_publish`` is verb-anchored, so a commit message or
    # branch name merely containing "push" does not match.
    #
    # A push to a PROTECTED branch (or a bare/ambiguous push) is denied here;
    # an explicit FEATURE-branch push is allowed to fall through to the normal
    # glob passes (so any other deny pattern in a compound command still
    # applies), and we record the allow INTENT now — the ``push_allowed`` audit
    # is emitted only at a SUCCESS return path below, so the SEL trail reflects
    # the FINAL outcome (never an allow for a command ultimately denied).
    push_allow_pending = False
    if _is_git_publish(lower):
        if _is_push_to_protected_branch(lower):
            _emit_deny_event(tool_name, _GIT_PUBLISH_DENY_LABEL, lower)
            return f"Blocked by security policy: {_GIT_PUBLISH_DENY_LABEL}"
        push_allow_pending = True

    # ── Pass 1: whole-string deny ──
    # If any pattern matches the full input AND no exception matches the
    # full input, deny outright.  Otherwise note the first pattern that
    # matched (and has at least one exception that matched) — that's the
    # candidate for per-segment exception evaluation in Pass 2.
    pass2_candidate_pattern: str | None = None
    for pattern in all_glob_patterns:
        if fnmatch.fnmatch(lower, pattern.lower()):
            exceptions = _DENY_EXCEPTIONS.get(pattern, [])
            whole_string_exception_match = exceptions and any(
                fnmatch.fnmatch(lower, e.lower()) for e in exceptions
            )
            if not whole_string_exception_match:
                _emit_deny_event(tool_name, pattern, lower)
                return f"Blocked by security policy: {pattern}"
            # Exception candidate — record and continue checking the
            # remaining patterns (a later pattern with no exception
            # match must still trigger an outright deny in pass 1).
            if pass2_candidate_pattern is None:
                pass2_candidate_pattern = pattern

    if pass2_candidate_pattern is None:
        # No deny match at all on the whole string.
        if push_allow_pending:
            _schedule_push_allow_audit(lower)
        return None

    # ── Pass 2: per-segment exception evaluation ──
    # Split into segments and re-check each.  Any segment that matches a
    # deny pattern without a matching exception denies the whole input —
    # this preserves chaining-bypass protection because an embedded real
    # publish (e.g. after ``;`` / ``&&`` / inside ``$(...)``) is its own
    # segment and matches the deny pattern.  Segments that match a deny
    # pattern AND an exception are allowed with a SEL audit event.
    segments = _split_segments(lower)
    for segment in segments:
        seg_lower = segment.strip()
        if not seg_lower:
            continue
        for pattern in all_glob_patterns:
            if fnmatch.fnmatch(seg_lower, pattern.lower()):
                exceptions = _DENY_EXCEPTIONS.get(pattern, [])
                if exceptions and any(
                    fnmatch.fnmatch(seg_lower, e.lower()) for e in exceptions
                ):
                    if not _emit_deny_exception_event(tool_name, pattern):
                        _emit_deny_event(tool_name, pattern, seg_lower)
                        return f"Blocked by security policy: {pattern}"
                    # Exception granted for this pattern on this segment;
                    # continue to evaluate any remaining patterns against
                    # the same segment (a different pattern without an
                    # exception must still cause a deny).
                    continue
                _emit_deny_event(tool_name, pattern, seg_lower)
                return f"Blocked by security policy: {pattern}"
    # All segments cleared the glob passes — the input is allowed.  If it was a
    # feature-branch push, emit the deferred allow audit now (final outcome).
    if push_allow_pending:
        _schedule_push_allow_audit(lower)
    return None


def _split_segments(command_lower: str) -> list[str]:
    """Split a command into independently-evaluatable segments.

    Splits on shell separators and command-substitution boundaries.
    Returns the list of segments (which may include the empty string for
    adjacent separators; callers should skip empties).
    """
    return _CMD_SPLIT_RE.split(command_lower)


def _emit_deny_event(tool_name: str, deny_pattern: str, segment: str) -> None:
    """Emit a SEL audit event when a command is denied.

    Records the operation, matched pattern, and (for pass-2 denials) the
    specific segment that triggered the block.  This satisfies the
    security-controls guideline that every permission decision — both
    grants and denials — must produce an audit trail.

    Best-effort: SEL logging failures are logged at WARNING and do not
    affect the deny decision (denials are inherently fail-closed; the
    block stands regardless of audit success).
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_event",
                caller_identity="",
                agent="kiroclaw",
                source="security",
                operation=tool_name,
                outcome="denied",
                resources=f"deny_pattern={deny_pattern}",
                metadata={
                    "deny_pattern": deny_pattern,
                    "segment": segment[:200] if segment else "",
                    "mechanism": "BUILTIN_DENY_PATTERNS",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for deny_event on %r (deny stands)",
            tool_name,
            exc_info=True,
        )


def _emit_deny_exception_event(tool_name: str, deny_pattern: str) -> bool:
    """Emit an SEL audit event when a deny exception is applied.

    Returns True if the event was logged successfully, False otherwise.
    The caller must NOT grant the exception if this returns False.
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_exception",
                caller_identity="",
                agent="kiroclaw",
                source="security",
                operation=tool_name,
                outcome="allowed",
                resources=f"deny_pattern={deny_pattern}",
                metadata={"deny_pattern": deny_pattern, "mechanism": "_DENY_EXCEPTIONS"},
            )
        )
        return True
    except Exception:
        logger.warning(
            "SEL audit failed for deny_exception — denying %r (fail-closed)",
            tool_name,
            exc_info=True,
        )
        return False


def audit_bash_command(command: str) -> str | None:
    """Check a bash command against suspicious patterns.

    Returns warning string, or None if clean.
    Patterns with ``*`` are matched as globs via fnmatch.
    """
    lower = command.lower()
    for pattern in SUSPICIOUS_BASH_PATTERNS:
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Suspicious command detected: matches '{pattern}'"
        elif pat in lower:
            return f"Suspicious command detected: matches '{pattern}'"
    return None


def scan_history(history_dir: Path, last_n: int = 100) -> list[dict]:
    """Scan recent conversation history for suspicious tool usage.

    Returns list of findings: [{file, line, tool, command, warning}]
    """
    findings: list[dict] = []
    if not history_dir.is_dir():
        return findings

    files = sorted(history_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    for f in files:
        try:
            for line in f.read_text().splitlines():
                if checked >= last_n:
                    return findings
                checked += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("content", "")
                role = entry.get("role", "")
                if role != "assistant" or not isinstance(content, str):
                    continue
                # Check for bash commands in tool calls
                warning = audit_bash_command(content)
                if warning:
                    findings.append(
                        {
                            "file": f.name,
                            "warning": warning,
                            "snippet": content[:200],
                        }
                    )
        except OSError:
            continue
    return findings


def scan_memory() -> list[dict]:
    """Scan vector memory for suspicious content. Returns list of findings."""
    from kiro_claw.vector_memory import VectorMemoryStore, _contains_injection

    findings: list[dict] = []
    try:
        store = VectorMemoryStore()
        store.init()
    except Exception:
        return findings

    # Scan semantic values
    for entry in store.get_all_semantic():
        val = entry.get("value_json", "")
        if _contains_injection(val):
            findings.append(
                {
                    "type": "semantic",
                    "key": entry["key"],
                    "value": val[:200],
                    "warning": "Injection pattern detected",
                }
            )

    # Scan episodic texts
    for entry in store.get_episodic_list(limit=1000):
        text = entry.get("text", "")
        if _contains_injection(text):
            findings.append(
                {
                    "type": "episodic",
                    "key": entry["id"],
                    "value": text[:200],
                    "warning": "Injection pattern detected",
                }
            )

    store.close()
    return findings


def should_record_observe_history(
    channel_history: object | None,
    user_authorized: bool,
) -> bool:
    """Return True if an observe-mode message should be recorded.

    Only authorized users' messages are recorded to prevent non-owner
    prompt injection via shared channel traffic (Shepherd bdd39e84).
    """
    return channel_history is not None and user_authorized


def redact_and_truncate(text: str, max_chars: int = 4000) -> str:
    """Truncate, then redact credentials and exfiltration URLs."""
    return redact_credentials(redact_exfiltration_urls((text or "")[:max_chars])[0])[0]
