"""Built-in security controls — deny list, sensitive path protection, and audit scanning."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from kiro_claw.sel import SecurityEvent, SecurityEventLog

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
    r"(?:^|[;&|`\n]|\$\()\s*git\s+(?:-\S+\s+(?:[^-]\S*\s+)?)*push(?=\s|[)`;&|]|$)"
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
]

# Regex for bash commands that read sensitive paths.
# Matches: cat, head, tail, less, more, strings, xxd, base64, cp, scp, open
# followed by a path containing any sensitive dir.
_READ_CMDS = r"(?:cat|head|tail|less|more|strings|xxd|base64|cp|scp|open|vi|vim|nano|code)\s"

# Matches python/ruby/perl one-liners that open sensitive paths
_SCRIPT_OPEN = r"(?:python|ruby|perl)\S*\s.*open\s*\("


def _build_sensitive_regex() -> re.Pattern[str]:
    """Build a compiled regex matching bash reads of sensitive paths."""
    home = re.escape(str(Path.home()))
    tilde = re.escape("~")
    home_var = re.escape("$HOME")
    home_alts = f"(?:{home}|{tilde}|{home_var})"
    escaped_dirs = [re.escape(d) for d in _SENSITIVE_HOME_DIRS]
    dirs_pattern = "|".join(escaped_dirs)
    return re.compile(
        rf"(?:{_READ_CMDS}.*|{_SCRIPT_OPEN}.*|.*[<>|]\s*){home_alts}/(?:{dirs_pattern})(?:/|\s|$|['\"])",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


def is_sensitive_path(path_str: str) -> bool:
    """Return True if the path points to a sensitive location.

    Works for both absolute paths and ~/relative paths.
    Used by hooks to block fs_read/ReadFile of credential files.
    """
    # Expand ~ and $HOME
    expanded = os.path.expanduser(os.path.expandvars(path_str))
    try:
        resolved = str(Path(expanded).resolve())
    except (OSError, ValueError):
        resolved = expanded

    try:
        home = str(Path.home().resolve())
    except (OSError, ValueError):
        home = str(Path.home())
    for sensitive_dir in _SENSITIVE_HOME_DIRS:
        sensitive_path = os.path.join(home, sensitive_dir)
        if resolved == sensitive_path or resolved.startswith(sensitive_path + os.sep):
            return True
    return False


def is_sensitive_bash_command(command: str) -> str | None:
    """Check if a bash command reads sensitive paths.

    Returns denial reason string, or None if clean.
    """
    if _get_sensitive_re().search(command):
        return "Blocked: command accesses sensitive credential path"
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
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r"|(?:SecretAccessKey|aws_secret_access_key)\s*[:=]\s*\S+"
    r"|(?:SessionToken|aws_session_token)\s*[:=]\s*\S+"
    r"|(?:AccessKeyId|aws_access_key_id)\s*[:=]\s*\S+"
    r"|BEGIN[\s](?:RSA|DSA|EC|OPENSSH)[\s]PRIVATE[\s]KEY"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    r")",
)

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
    if _is_git_publish(lower):
        _emit_deny_event(tool_name, _GIT_PUBLISH_DENY_LABEL, lower)
        return f"Blocked by security policy: {_GIT_PUBLISH_DENY_LABEL}"

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
