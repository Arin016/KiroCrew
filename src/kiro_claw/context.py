"""Context builder — assembles memory, skills, and hooks into prompt context."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_claw.agent import _prompt_path
from kiro_claw.config.loader import KiroClawConfig, workspace_dir_for
from kiro_claw.cron import get_local_tz
from kiro_claw.hooks import (
    HOOK_INJECT_CONTEXT,
    HOOK_MODIFY,
    HookManager,
    HookResult,
    safe_read_file,
)
from kiro_claw.learn import LessonStore
from kiro_claw.memory import MemoryStore
from kiro_claw.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_claw.skills import SkillsLoader

if TYPE_CHECKING:
    from kiro_claw.channel_history import ChannelHistory
    from kiro_claw.history import ConversationLog
    from kiro_claw.session import SessionManager

logger = logging.getLogger(__name__)

# Lazy cache of MemoryStore instances keyed by workspace name.
_memory_stores: dict[str, MemoryStore] = {}
# Lazy cache of LessonStore instances keyed by workspace name.
_lesson_stores: dict[str, LessonStore] = {}

# Cap injected context to avoid blowing the context window on first turn
_MAX_CONTEXT_CHARS = 165_000  # ~55k tokens

# kiro-cli task_executor slices strings at fixed byte offsets (e.g. 4096).
# Multi-byte UTF-8 chars straddling the boundary cause a Rust panic:
#   "byte index 4096 is not a char boundary; it is inside '—'"
# Workaround: replace common multi-byte punctuation with ASCII equivalents.
# TODO: revert when kiro-cli PR #2034 merges (truncate_safe fix).
_MULTIBYTE_TABLE = str.maketrans({
    "\u2014": "--",  # em dash
    "\u2013": "-",   # en dash
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",   # non-breaking space
    "\u2022": "-",   # bullet
    "\u2192": "->",  # rightwards arrow (→) — caused 5 kiro-cli panics
    "\u2190": "<-",  # leftwards arrow (←)
    "\u2194": "<->",  # left right arrow (↔)
    "\u21d2": "=>",   # rightwards double arrow (⇒)
    "\u2713": "[x]",  # check mark (✓)
    "\u2717": "[ ]",  # ballot x (✗)
    "\u00d7": "x",   # multiplication sign (×)
    # Known gap: accented chars (e.g. \u00e9) and emoji are not replaced here.
    # They are legitimate content; stripping them would be lossy. The real fix
    # is kiro-cli PR #2034 (truncate_safe).
})

# Soft per-component caps: each component is individually truncated to its
# cap, then the assembled context is hard-truncated at _MAX_CONTEXT_CHARS.
# No single component may exceed 30% of the hard cap to prevent any one
# category from dominating. The sum of soft caps (~145k) is under the hard
# cap (165k), so all components can coexist without silent truncation.
_HISTORY_BUDGET_CHARS = 35_000   # thread history (fallback/truncated)
# Caps below absorbed the 6_000-char budget that the removed cross-tab block
# used to consume, distributed proportionally across the six memory components
# (per Bolin's review feedback on CR-273346177).
_MEMORY_PREFS_CAP = 4_250        # user preferences (+250)
_MEMORY_PROJECTS_CAP = 6_400     # active projects (+400)
_MEMORY_HISTORY_CAP = 26_600     # daily history (multi-tier decay) (+1_600)
_LESSONS_CAP = 37_250            # learned corrections (high priority) (+2_250)
_SEMANTIC_MEMORY_CAP = 12_750    # structured key-value facts (vector memory) (+750)
_EPISODIC_MEMORY_CAP = 12_750    # relevant past conversation fragments (vector memory) (+750)
_PER_MESSAGE_CAP = 8_000  # truncate individual messages on fallback path

# Strip Mode Identity blocks from injected context so cross-tab or history
# content from a different mode doesn't override the current prompt's identity.
_MODE_IDENTITY_RE = re.compile(
    r"## 🔒 Mode Identity.*?(?=\n## |\Z)", re.DOTALL
)
_COMPRESSED_HISTORY_CAP = 45_000  # budget for LLM-compressed thread summary


_STOP_EVENT_CAP = 3  # max recent stop events to inject into LLM context
_STOP_EVENT_RESOLVED_STATES = frozenset({"stopped", "stop_failed_reset"})


def _build_stop_event_notes(
    conversation_log: "ConversationLog", session_key: str
) -> str:
    """Render recent resolved stop_events as short system notes for LLM context."""
    # Bound the scan: only the last _STOP_EVENT_CAP stop events matter,
    # and stop events from hundreds of turns ago are not actionable context.
    # Matches the pattern used by ``build_cancelled_turn_preamble`` below.
    messages = conversation_log.recent(session_key, max_messages=20)
    notes: list[str] = []
    for m in reversed(messages):
        if len(notes) >= _STOP_EVENT_CAP:
            break
        if m.get("role") != "system":
            continue
        content = m.get("content", "")
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(data, dict)
            and data.get("kind") == "stop_event"
            and data.get("state") in _STOP_EVENT_RESOLVED_STATES
        ):
            notes.append("[User stopped the previous turn mid-execution.]")
    if not notes:
        return ""
    notes.reverse()
    return "\n".join(notes) + "\n\n"


# Budget tradeoff: 100 user+assistant msgs covers P90 of sessions.
# Role filtering excludes tool display titles, so the budget is spent
# on actual conversation content.
_COMPRESSION_MAX_MESSAGES = 100
_HEAD_TAIL_MESSAGES = 2  # verbatim head/tail kept around compressed middle

_COMPRESSION_PROMPT_PREFIX = """\
You are a conversation compressor. Given a chat transcript and the user's \
latest query, produce a compressed summary that preserves ALL of the following:

- File paths, URLs, branch names, package names (verbatim)
- Decisions made and their rationale
- Code snippets discussed or modified (abbreviated, keep key lines)
- Error messages and their resolutions
- Action items and status (done / in-progress / pending)
- Names, aliases, ticket IDs, CR numbers
- Any factual information the user or assistant stated

Drop:
- Greetings, filler, acknowledgments ("sure", "got it", "let me check")
- Redundant tool output (keep only the conclusion)
- Build logs (keep only pass/fail and error lines)
- Repeated explanations of the same concept

Format: dense paragraphs grouped by topic. Bullet points for lists of \
facts. File paths in backticks.

Respond with ONLY the compressed summary, no preamble."""

# Docs directory bundled inside the kiro_claw package
_BUNDLED_DOCS_DIR = Path(__file__).resolve().parent / "docs"

# Display names for runtime environments, keyed by the source tag from
# sel.py _infer_source().  Kept here so the mapping is close to the
# injection site and easy to extend.
_RUNTIME_DISPLAY = {
    "dashboard": "KiroClaw dashboard",
    "cron": "KiroClaw cron job",
    "subagent": "KiroClaw subagent",
    "taskrunner": "KiroClaw task runner",
    "background": "KiroClaw background",
    "cli": "CLI terminal",
    "slack": "Slack",
}


def _runtime_display_name(session_key: str) -> str:
    """Map a session_key to a human-readable runtime name.

    Uses the same prefix heuristic as ``sel.py:_infer_source()`` so both
    SEL audit logs and LLM context agree on the runtime.
    """
    if session_key.startswith("dashboard:") or session_key.startswith("dashboard_"):
        source = "dashboard"
    elif session_key.startswith("cron:") or session_key.startswith("cron_"):
        source = "cron"
    elif session_key.startswith("subagent:"):
        source = "subagent"
    elif session_key.startswith("taskrunner"):
        source = "taskrunner"
    elif session_key == "_bg":
        source = "background"
    elif session_key == "cli_chat":
        source = "cli"
    else:
        source = "slack"
    return _RUNTIME_DISPLAY.get(source, source)


def _build_docs_section() -> str:
    """Build a lightweight docs pointer for session context.

    Resolves the bundled docs path from the installed Python package.
    Returns empty string if the docs directory doesn't exist.
    """
    if not _BUNDLED_DOCS_DIR.is_dir():
        return ""
    return (
        "[DOCUMENTATION]\n"
        f"KiroClaw docs: {_BUNDLED_DOCS_DIR}\n"
        "\n"
        "For KiroClaw behavior, commands, config, or architecture: "
        "consult local docs first.\n"
        "When diagnosing issues, run `kiroclaw status` or "
        "`kiroclaw doctor` yourself when possible.\n"
        "[END DOCUMENTATION]\n\n"
    )


def _load_steering_resources() -> str:
    """Load steering files from the agent config's resources array.

    kiro-cli injects these automatically for its sessions; the dashboard
    must do it explicitly so that dashboard chat sessions also benefit
    from project-specific steering conventions.
    Only loads ``file://`` resources matching ``*.md``.
    """
    try:
        cfg_path = Path.home() / ".kiro" / "agents" / "kiroclaw.json"
        if not cfg_path.exists():
            return ""
        cfg = json.loads(safe_read_file(str(cfg_path)))
        resources = cfg.get("resources", [])
        parts: list[str] = []
        home_resolved = str(Path.home().resolve()) + os.sep
        for res in resources:
            if not isinstance(res, str) or not res.startswith("file://"):
                continue
            raw_pattern = res.removeprefix("file://")
            base = Path.home()
            for p in sorted(base.glob(raw_pattern)):
                resolved = p.resolve()
                if not str(resolved).startswith(home_resolved):
                    continue
                if resolved.is_file() and p.suffix == ".md" and not is_sensitive_path(str(resolved)):
                    try:
                        parts.append(safe_read_file(str(p)))
                    except PermissionError:
                        pass
        if parts:
            logger.debug("loaded %d steering bytes from %d files", sum(len(p) for p in parts), len(parts))
        return "\n".join(parts) if parts else ""
    except Exception as exc:
        logger.debug("steering load failed: %s", type(exc).__name__)
        return ""


# Critical rules reinforced every session (supplements the system prompt)
_CRITICAL_RULES = (
    "[CRITICAL RULES — always follow these]\n"
    "After ANY file change (create, edit, append, delete), you MUST show a "
    "```diff code block with the change using standard unified diff format "
    "including `--- old_path` / `+++ new_path` headers and an `@@` hunk line "
    "(use /dev/null for new files / deletions). The headers are required so "
    "the dashboard's diff viewer can link to the file. No exceptions — even "
    "single-line changes MUST get a diff block.\n"
    "When referencing file paths in your response, ALWAYS use the absolute path "
    "inside inline `code` backticks (e.g. `/home/user/project/src/main.py`). "
    "Never use relative paths or bare filenames. This enables the UI file viewer panel.\n"
    "When presenting choices or options to the user, you MUST end your response "
    "with [OPTIONS: Choice A | Choice B | Choice C] on its own line. "
    "This renders interactive buttons in the UI. Users can select multiple options before submitting.\n"
    "[END CRITICAL RULES]\n\n"
)


# Regex patterns for noise compression in assistant messages
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_JSON_BLOB_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _compress_assistant_message(text: str) -> str:
    """Reduce low-signal noise from assistant messages on the fallback path.

    Code blocks over 2K chars are replaced with a head/tail excerpt that
    preserves function signatures, imports, and structure.  JSON blobs
    over 1K chars are replaced with a truncation marker.
    """

    def _replace_code_block(m: re.Match[str]) -> str:
        body = m.group(1)
        if len(body) <= 2000:
            return m.group(0)
        lines = body.strip().splitlines()
        if len(lines) > 15:
            kept = lines[:10] + [f"  ... ({len(lines) - 15} lines omitted)"] + lines[-5:]
        else:
            # Few lines but still over 2K — apply character-level truncation
            truncated_body = body[:2000]
            kept = truncated_body.splitlines()
            kept.append(f"  ... ({len(body) - 2000} chars truncated)")
        lang_line = m.group(0).split("\n", 1)[0]  # ```lang
        return lang_line + "\n" + "\n".join(kept) + "\n```"

    result = _CODE_BLOCK_RE.sub(_replace_code_block, text)

    def _replace_json(m: re.Match[str]) -> str:
        if len(m.group(0)) <= 1000:
            return m.group(0)
        return "[tool output truncated]"

    result = _JSON_BLOB_RE.sub(_replace_json, result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


def build_cancelled_turn_preamble(
    conversation_log: "ConversationLog",
    session_key: str,
    *,
    user_cap: int = 2000,
    assist_cap: int = 2000,
) -> str:
    """Build a preamble describing the most recent cancelled turn, if any.

    kiro-cli does not persist cancelled turns to its ACP conversation log,
    so after a soft-stop the LLM has no memory of what the user asked or
    what it had started saying. Scan the persisted ``conversation_log``
    backwards for a ``stop_event`` marker, then find the user message
    immediately before it plus any assistant text in between. Return a
    short bracketed preamble. Returns "" if nothing to inject.

    Called by both dashboard and Slack callers after ``prev_turn_cancelled``
    is observed on the session.
    """
    try:
        recent = conversation_log.recent(session_key, max_messages=20)
    except Exception:
        return ""
    if not recent:
        return ""
    # Look for a stop_event marker (dashboard writes these; Slack does not).
    # If present, it bounds the cancelled turn. Otherwise fall back to "last
    # user turn" — safe because (a) ``prev_turn_cancelled`` is a one-shot
    # flag consumed right before this function runs, and (b) callers persist
    # the NEW user message to ``conversation_log`` only AFTER the preamble
    # is built (see handler.py save_conversation_turn / chat.py _flush_segment),
    # so ``recent()`` at this moment contains only prior turns and the most
    # recent user entry is the cancelled one.
    stop_idx = -1
    for i in range(len(recent) - 1, -1, -1):
        if recent[i].get("role") != "system":
            continue
        content = recent[i].get("content", "")
        if not isinstance(content, str) or not content:
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("kind") == "stop_event":
                stop_idx = i
                break
        except (ValueError, TypeError):
            continue
    # Find the most recent user message. If a stop_event was found, the user
    # message must precede it; otherwise just take the latest user entry.
    search_end = stop_idx if stop_idx >= 0 else len(recent)
    user_idx = -1
    for i in range(search_end - 1, -1, -1):
        if recent[i].get("role") == "user":
            user_idx = i
            break
    if user_idx < 0:
        return ""
    # Collect any assistant text between user_idx and the boundary.
    boundary = stop_idx if stop_idx >= 0 else len(recent)
    user_text = (recent[user_idx].get("content") or "").strip()
    assistant_parts: list[str] = []
    for i in range(user_idx + 1, boundary):
        if recent[i].get("role") == "assistant":
            t = (recent[i].get("content") or "").strip()
            if t:
                assistant_parts.append(t)
    assistant_text = "\n".join(assistant_parts)
    if len(user_text) > user_cap:
        user_text = user_text[:user_cap] + "… [truncated]"
    if len(assistant_text) > assist_cap:
        assistant_text = assistant_text[:assist_cap] + "… [truncated]"
    lines = [
        "[PREVIOUS TURN WAS CANCELLED BY THE USER — context restore]",
        "The following user request was interrupted mid-response. "
        "Acknowledge it only if the current request refers to it.",
        "",
        f"Cancelled user request:\n{user_text}",
    ]
    if assistant_text:
        lines += ["", f"Partial assistant response before cancel:\n{assistant_text}"]
    lines.append("[END PREVIOUS TURN]")
    return "\n".join(lines)


async def compress_thread_history(
    conversation_log: "ConversationLog",
    session_key: str,
    query: str,
    sessions: "SessionManager",
    *,
    exclude_last_n: int = 0,
) -> str | None:
    """Compress full thread history via background LLM call.

    ``is_new`` in callers means a new kiro-cli process (or dashboard tab)
    attached to an *existing* Slack thread — not a brand-new conversation.
    The thread already has history from prior processes, so we compress it
    to fit within the context window of the fresh session.

    Returns the compressed summary string, or None on failure (callers
    fall back to raw truncation).  This is the ONLY async function in
    this module — callers await it and pass the result into the sync
    ``build_session_context`` / ``build_message`` methods.

    The output uses a head/tail pattern: the first and last
    ``_HEAD_TAIL_MESSAGES`` are kept verbatim while the middle is
    LLM-compressed, preserving both conversation opening context and
    the most recent exchanges.

    *exclude_last_n* is forwarded to ``conversation_log.recent`` to drop
    the just-flushed current-turn user message from history (Mesh-1726).
    """
    from kiro_claw.llm_helpers import stream_and_collect  # circular import
    from kiro_claw.session import BACKGROUND_KEY  # circular import

    recent = conversation_log.recent(
        session_key,
        max_messages=_COMPRESSION_MAX_MESSAGES,
        roles={"user", "assistant"},
        exclude_last_n=exclude_last_n,
    )
    if not recent:
        return None

    lines: list[str] = []
    for m in recent:
        # Compression path: no per-message cap, no code stripping.
        # The LLM compressor sees full content and decides what to keep.
        lines.append(f"{m['role'].title()}: {m['content']}")
    transcript = "\n".join(lines)

    if len(transcript) <= _COMPRESSED_HISTORY_CAP:

        transcript, _ = redact_exfiltration_urls(transcript)
        transcript, _ = redact_credentials(transcript)
        return transcript.translate(_MULTIBYTE_TABLE)

    head_lines = lines[:_HEAD_TAIL_MESSAGES]
    tail_lines = lines[-_HEAD_TAIL_MESSAGES:] if len(lines) > _HEAD_TAIL_MESSAGES else []

    prompt = (
        _COMPRESSION_PROMPT_PREFIX
        + f"\n\nTarget {_COMPRESSED_HISTORY_CAP} characters max."
        + "\n\n## Latest user query (for relevance weighting)\n"
        + query
        + "\n\n## Transcript to compress\n"
        + transcript
    )

    acquired = False
    try:
        client, _is_new, _resumed = await sessions.get_or_create(
            BACKGROUND_KEY, agent="kiroclaw-lite"
        )
        acquired = True
        result = await stream_and_collect(client, prompt)
        if not result:
            return None

        parts: list[str] = []
        if head_lines:
            parts.append("## Thread start (verbatim)\n" + "\n".join(head_lines))
        parts.append("## Compressed history\n" + result[:_COMPRESSED_HISTORY_CAP])
        if tail_lines:
            parts.append("## Recent exchanges (verbatim)\n" + "\n".join(tail_lines))
        final = "\n\n".join(parts)
        final, _ = redact_exfiltration_urls(final)
        final, _ = redact_credentials(final)
        return final.translate(_MULTIBYTE_TABLE)
    except Exception:
        logger.warning("Thread history compression failed", exc_info=True)
        return None
    finally:
        if acquired:
            sessions.release(BACKGROUND_KEY)
            await sessions.recycle_background()


# ── Provider-Agnostic Session Replay ──


_REPLAY_BUDGET_CHARS = 80_000  # 80K chars ≈ 20K tokens — fits alongside system context in 200K window


def build_session_replay(
    conversation_log: "ConversationLog",
    session_key: str,
    *,
    exclude_last_n: int = 0,
) -> str | None:
    """Build session replay from KiroClaw's conversation_log.

    Keeps as many recent messages as fit within _REPLAY_BUDGET_CHARS,
    prioritizing the most recent exchanges (tail-heavy). Used when a
    session is picked up by a different provider or after process death.

    Same-provider resume uses native ACP session/load instead (full fidelity
    without needing this injection).

    *exclude_last_n* is forwarded to ``conversation_log.recent_chained`` to
    drop the just-flushed current-turn user message from replay (Mesh-1726).
    """
    messages = conversation_log.recent_chained(
        session_key,
        max_messages=500,
        roles={"user", "assistant"},
        exclude_last_n=exclude_last_n,
    )
    if not messages:
        return None

    # Build lines from most recent to oldest, stop when budget exhausted
    lines: list[str] = []
    total = 0
    for m in reversed(messages):
        role = m["role"].title()
        content = m.get("content", "")
        line = f"{role}: {content}"
        if total + len(line) > _REPLAY_BUDGET_CHARS and lines:
            break
        lines.append(line)
        total += len(line) + 2  # +2 for separator

    lines.reverse()
    replay = "\n\n".join(lines)
    replay, _ = redact_exfiltration_urls(replay)
    replay, _ = redact_credentials(replay)
    return replay.translate(_MULTIBYTE_TABLE)


class ContextBuilder:
    """Builds context for injection into ACP prompts.

    Assembles memory, skills, and hook-injected context into a single
    string that gets prepended to the user's message on the first turn
    of a session (or after a context reset).
    """

    @staticmethod
    def get_memory_for(workspace: str | None = None) -> MemoryStore:
        """Return a MemoryStore for the given workspace, creating lazily."""
        key = workspace or "default"
        if key not in _memory_stores:
            ws_path = workspace_dir_for(key)
            store = MemoryStore(workspace=ws_path)
            store.init()
            _memory_stores[key] = store
        return _memory_stores[key]

    @staticmethod
    def get_lessons_for(workspace: str | None = None) -> LessonStore:
        """Return a LessonStore for the given workspace, creating lazily."""
        key = workspace or "default"
        if key not in _lesson_stores:
            ws_path = workspace_dir_for(key)
            _lesson_stores[key] = LessonStore(base_dir=ws_path)
        return _lesson_stores[key]

    def __init__(
        self,
        memory: MemoryStore | None = None,
        skills: SkillsLoader | None = None,
        hooks: HookManager | None = None,
        lessons: LessonStore | None = None,
        conversation_log: "ConversationLog | None" = None,
        channel_history: "ChannelHistory | None" = None,
        bot_name: str = "",
    ):
        self.memory = memory or MemoryStore()
        self.skills = skills or SkillsLoader()
        self.hooks = hooks or HookManager()
        self.lessons = lessons or LessonStore()
        self.conversation_log = conversation_log
        self.channel_history = channel_history
        if bot_name:
            self._bot_name = bot_name
        else:
            cfg = KiroClawConfig.load()
            self._bot_name = "KiroClaw" if cfg.agent.provider == "claude_code" else "Kiro"
        # Register default memory in the workspace cache
        _memory_stores["default"] = self.memory

    def _substitute_bot_name(self, prompt: str) -> str:
        """Replace {bot_name} placeholder in prompt text."""
        return prompt.replace("{bot_name}", self._bot_name)

    @staticmethod
    def _resolve_prompt_templates(prompt: str, session_key: str) -> str:
        """Resolve conditional template blocks in prompt text.

        Dashboard sessions get a short widget pointer; Slack/CLI get it stripped.
        """
        is_dashboard = session_key and (
            session_key.startswith("dashboard:") or session_key.startswith("dashboard_")
        )
        if not is_dashboard:
            return prompt.replace("{{WIDGET_BLOCK}}", "")

        cfg = KiroClawConfig.load()
        density = getattr(cfg.dashboard, "widget_density", "more")

        if density == "more":
            widget_block = (
                "## Inline Widgets\n\n"
                "You can render rich HTML inline using "
                "`<mcwidget title=\"Title\">HTML</mcwidget>` tags. Load the `widgets` "
                "skill for theme variables, format rules, interactive widgets, and "
                "best practices when emitting one.\n\n"
                "## Artifacts\n\n"
                "Widgets render once and disappear with chat scrollback. Persist "
                "and iterate on them via `@kiroclaw-core/artifact_save`, "
                "`artifact_get`, `artifact_update`, `artifact_list`, "
                "`artifact_versions`, `artifact_delete`. Load the `artifacts` skill "
                "for the full workflow — when to save proactively, the iterate "
                "decision tree (including iterate-without-slug auto-save), naming "
                "conventions, and worked examples."
            )
        else:
            widget_block = (
                "## Inline Widgets\n\n"
                "You can render rich HTML inline using `<mcwidget>` tags, but prefer "
                "plain markdown by default. Load the `widgets` skill when a widget is "
                "genuinely warranted.\n\n"
                "## Artifacts\n\n"
                "Persist widgets via `@kiroclaw-core/artifact_save` (and friends). "
                "Load the `artifacts` skill when iterating, listing, or persisting."
            )
        return prompt.replace("{{WIDGET_BLOCK}}", widget_block)

    @staticmethod
    def _load_agent_prompt(agent: str) -> str:
        """Read the prompt from a custom agent's config file."""
        agents_dir = Path.home() / ".kiro" / "agents"
        for f in agents_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("name") == agent or f.stem == agent:
                    prompt = data.get("prompt") or ""
                    if prompt.startswith("file://"):
                        try:
                            return safe_read_file(prompt[7:])
                        except (OSError, PermissionError):
                            return ""
                    return prompt
            except (json.JSONDecodeError, OSError):
                continue
        return ""

    def build_session_context(
        self,
        session_key: str | None = None,
        agent: str | None = None,
        resumed: bool = False,
        workspace: str | None = None,
        memory_store: str | None = None,
        compressed_history: str | None = None,
        mode: str = "",
        blocks_reads: bool = False,
        provider_type: str = "acp",
        *,
        exclude_last_n: int = 0,
    ) -> str:
        """Build context for a new session (memory + skills + history).

        Injected once at session start, not on every message.

        When *compressed_history* is provided, it replaces the naive
        truncation of thread history.  Callers obtain it by awaiting
        ``compress_thread_history()`` before calling this method.

        When ``provider_type="claude_code"``, thread history and critical
        rules are skipped (CC manages its own context and output formatting).
        Memory, skills, and lessons are still injected (KiroClaw-specific).

        For custom agents (non-kiroclaw), skills and workspace identity
        are skipped — the agent loads its own via kiro-cli. Memory,
        lessons, critical rules, and hooks are injected for all agents.
        """
        is_cc = provider_type == "claude_code"
        is_custom = agent and agent != "kiroclaw"
        parts: list[str] = []

        if is_custom:
            logger.info(
                "Custom agent %r: injecting memory/lessons/rules, skipping skills",
                agent,
            )
        else:
            logger.debug("Building session context for kiroclaw agent")

        # Critical rules: inject for ACP agents (diff rendering, OPTIONS buttons).
        # CC provider handles its own output formatting via CLAUDE.md.
        if not is_cc:
            parts.append(_CRITICAL_RULES)

        # Current date/time — inject for ALL agents so the LLM knows "today".
        # Honour KiroClawConfig.timezone (e.g. "Asia/Tokyo") so the LLM sees
        # the user's local time instead of the gateway host's system TZ, which
        # is often UTC on Cloud Desktops and makes "today" ambiguous.

        _, tz = get_local_tz()
        now = datetime.now(tz)
        parts.append(f"[CURRENT DATE] {now.strftime('%A, %Y-%m-%d %H:%M %Z')}\n\n")

        # Agent identity and runtime — inject for ALL agents so the LLM
        # knows which agent it is and where it's running.  Without this,
        # the LLM cannot distinguish dashboard from kiro-cli and may
        # incorrectly tell the user to "go to the dashboard" when it IS
        # the dashboard.
        #
        # Runtime detection reuses the same heuristic as sel.py
        # _infer_source() to keep a single source of truth.
        agent_label = agent or "kiroclaw"
        if session_key:
            runtime = _runtime_display_name(session_key)
            parts.append(
                f"[CURRENT AGENT] {agent_label}\n"
                f"[RUNTIME] {runtime}\n"
                f"You ARE this agent running in {runtime}. "
                f"Prefer solutions native to this runtime. "
                f"Only suggest switching interfaces if the user asks "
                f"or the task requires it.\n\n"
            )

        # Workspace identity — kiroclaw-only (custom agents don't use workspaces)
        if not is_custom:
            ws_name = workspace or "default"
            ws_path = workspace_dir_for(ws_name)
            parts.append(
                "[WORKSPACE IDENTITY]\n"
                f"You are operating in workspace: {ws_name}\n"
                f"Workspace path: {ws_path}\n"
                "A workspace is an isolated context with its own memory (preferences, "
                "projects, daily history) and files. Different workspaces have different "
                "memory — what you learn in one workspace stays in that workspace.\n\n"
                "Lessons have two scopes (use learn_add tool to save):\n"
                "- scope=global (default): shared across ALL workspaces. "
                "Use for universal preferences (e.g. 'always use dark mode').\n"
                f"- scope=workspace: only visible in this workspace ({ws_name}). "
                "Use for project-specific rules "
                "(e.g. 'this repo uses pytest-asyncio strict mode').\n"
                "[End of workspace identity]\n\n"
            )

        # Documentation pointer — kiroclaw-only, lightweight reference
        if not is_custom:
            docs_ctx = _build_docs_section()
            if docs_ctx:
                parts.append(docs_ctx)

        # Steering files from agent config resources.
        # kiro-cli injects these natively; dashboard must load explicitly.
        # Skip for CC provider (manages its own context via CLAUDE.md).
        if not is_custom and not is_cc:
            steering_ctx = _load_steering_resources()
            if steering_ctx:
                parts.append(steering_ctx)

        # Thread conversation history — highest priority context.
        # Use pre-computed LLM compression when available; fall back to truncation.
        # Skip for CC provider (CC manages its own context natively).
        if session_key and self.conversation_log and not resumed and not is_cc:
            _history_header = (
                "[THREAD CONVERSATION HISTORY — this is the PRIMARY context.\n"
                "When the user says 'just now', 'earlier', 'the task', 'try again', "
                "or refers to something discussed — ALWAYS look here first. "
                "Do NOT say 'there is no previous context' if content exists below. "
                "Do NOT re-execute past actions unprompted.]\n"
            )
            if compressed_history:

                compressed_history, _ = redact_exfiltration_urls(compressed_history)
                compressed_history, _ = redact_credentials(compressed_history)
                compressed_history = _MODE_IDENTITY_RE.sub("", compressed_history)
                logger.info(
                    "🔍 build_session_context: session_key=%s LLM-compressed " "history (%d chars)",
                    session_key,
                    len(compressed_history),
                )
                parts.append(_history_header + compressed_history + "\n[End of thread history]\n\n")
            else:
                recent = self.conversation_log.recent(
                    session_key, roles={"user", "assistant"}, exclude_last_n=exclude_last_n
                )
                logger.info(
                    "🔍 build_session_context: session_key=%s resumed=%s "
                    "conv_log_entries=%d (fallback truncation)",
                    session_key,
                    resumed,
                    len(recent),
                )
                if recent:
                    budget = _HISTORY_BUDGET_CHARS
                    history_lines: list[str] = []
                    for m in reversed(recent):
                        content = _MODE_IDENTITY_RE.sub("", m["content"])
                        if m["role"] == "assistant":
                            content = _compress_assistant_message(content)
                        if len(content) > _PER_MESSAGE_CAP:
                            content = content[:_PER_MESSAGE_CAP] + "…[truncated]"
                        line = f"{m['role'].title()}: {content}"
                        if budget - len(line) < 0:
                            break
                        history_lines.append(line)
                        budget -= len(line)
                    if history_lines:
                        history_lines.reverse()
                        history_block = "\n".join(history_lines)
                        history_block, _ = redact_exfiltration_urls(history_block)
                        history_block, _ = redact_credentials(history_block)
                        parts.append(
                            _history_header + history_block + "\n[End of thread history]\n\n"
                        )
        elif session_key and resumed:
            logger.info(
                "🔍 build_session_context: session_key=%s RESUMED — "
                "skipping thread history (kiro-cli has native history)",
                session_key,
            )

        # Stop event context — inject notes for recent stop events so the
        # LLM knows prior turns were cancelled by the user.
        if session_key and self.conversation_log:
            _stop_notes = _build_stop_event_notes(
                self.conversation_log, session_key
            )
            if _stop_notes:
                parts.append(_stop_notes)

        # Memory and lessons: inject for ALL agents (including custom).
        # The user's preferences, project context, and learned corrections
        # are valuable regardless of which agent is running.
        # Temporary sessions skip all memory reads.
        mem_key = memory_store or workspace
        memory = self.get_memory_for(mem_key)
        if not blocks_reads:
            memory_ctx = memory.get_context(
                prefs_cap=_MEMORY_PREFS_CAP,
                projects_cap=_MEMORY_PROJECTS_CAP,
                history_cap=_MEMORY_HISTORY_CAP,
                semantic_cap=_SEMANTIC_MEMORY_CAP,
                episodic_cap=_EPISODIC_MEMORY_CAP,
            )
            if memory_ctx:
                parts.append(memory_ctx)

        # Skills: kiroclaw-only (custom agents load their own via kiro-cli)
        if not is_custom:
            skills_ctx = self.skills.get_context()
            if skills_ctx:
                parts.append(skills_ctx)

        # Lessons: merge global + workspace-scoped — inject for ALL agents
        # (skipped for temporary sessions)
        lessons_ctx = ""
        if not blocks_reads:
            if memory.vector_store and memory.vector_store.get_lessons():
                lessons_ctx = memory.vector_store.get_lessons_context()
            else:
                lessons_ctx = self.lessons.get_context()
            # Merge workspace-scoped lessons if workspace differs from default
            if workspace and workspace != "default":
                ws_lessons = self.get_lessons_for(workspace)
                ws_ctx = ws_lessons.get_context()
                if ws_ctx and lessons_ctx:
                    # Append workspace lessons inside the same block
                    lessons_ctx = (
                        lessons_ctx.rstrip().removesuffix("[End of learned corrections]").rstrip()
                        + "\n"
                        + ws_ctx.split("]", 1)[-1].lstrip()
                    )
                elif ws_ctx:
                    lessons_ctx = ws_ctx
            if lessons_ctx:
                if len(lessons_ctx) > _LESSONS_CAP:
                    lessons_ctx = lessons_ctx[:_LESSONS_CAP] + "\n…[lessons truncated]\n"
                parts.append(lessons_ctx)

        # Provenance-tagged entries from recent sessions (skipped for temporary)
        if session_key and self.conversation_log and not blocks_reads:
            provenance = self.conversation_log.recent_with_provenance(
                session_key, exclude_last_n=exclude_last_n
            )
            if provenance:
                prov_lines: list[str] = []
                for p in provenance:
                    prov_lines.append(
                        f"- [thread {p['source_thread']}, {p['ts'][:16]}] {p['snippet']}"
                    )
                parts.append("## Recent Session Context\n" + "\n".join(prov_lines) + "\n\n")

        context = "".join(parts)
        if len(context) > _MAX_CONTEXT_CHARS:
            logger.warning(
                "Session context too large (%d chars), truncating to %d",
                len(context),
                _MAX_CONTEXT_CHARS,
            )
            context = context[:_MAX_CONTEXT_CHARS]
            # Avoid cutting mid-line
            last_nl = context.rfind("\n")
            if last_nl > 0:
                context = context[: last_nl + 1]

        logger.debug(
            "Session context: agent=%s, custom=%s, %d chars",
            agent or "kiroclaw",
            is_custom,
            len(context),
        )
        return context

    def build_message(
        self,
        text: str,
        is_new_session: bool,
        session_key: str | None = None,
        channel_id: str | None = None,
        interactive: bool = True,
        agent: str | None = None,
        resumed: bool = False,
        thread_ts: str | None = None,
        workspace: str | None = None,
        project: str | None = None,
        memory_store: str | None = None,
        user_display_name: str | None = None,
        compressed_history: str | None = None,
        mode: str = "",
        blocks_reads: bool = False,
        action_context: str | None = None,
        thread_parent_text: str | None = None,
        thread_meta: str | None = None,
        provider_type: str = "acp",
        *,
        exclude_last_n: int = 0,
    ) -> tuple[str, HookResult]:
        """Build the full message with context and hook processing.

        On new sessions: prepends memory + always-on skills + lessons + history
        + episodic memory.
        On follow-up messages: only channel history (group channels), triggered
        skills, and hook context. ACP native history is trusted — no parallel
        transcript is injected.

        Pass *compressed_history* (from ``compress_thread_history()``) to
        inject LLM-compressed thread context instead of naive truncation.

        Returns:
            (full_message, hook_result) — hook_result may be a reply/modify/inject.
        """
        is_custom = agent and agent != "kiroclaw"
        hook_result = self.hooks.on_message(text)

        parts: list[str] = []
        is_cc = provider_type == "claude_code"

        # Session context on first message only
        if is_new_session:
            # Agent prompt goes BEFORE session context wrapper
            # so the LLM treats it as its identity, not background info.
            if is_cc:
                # CC gets the KiroClaw persona prompt (identity, capabilities)
                # but NOT the output format rules or kiro-specific references.
                try:
                    pp = _prompt_path(mode=mode)
                    agent_prompt = pp.read_text(encoding="utf-8")
                    # Strip output format section — CC handles this itself
                    agent_prompt = re.sub(
                        r"## Output Format.*?(?=\n## |\Z)", "", agent_prompt, flags=re.DOTALL
                    )
                    # Replace kiro-cli references with claude code equivalents
                    agent_prompt = agent_prompt.replace("kiro-cli", "claude code")
                    agent_prompt = re.sub(r"\bKiro\b", "Claude", agent_prompt)
                    agent_prompt = re.sub(r"\bkiro\b", "claude", agent_prompt)
                    agent_prompt = agent_prompt.strip()
                except Exception:
                    agent_prompt = ""
            elif is_custom:
                agent_prompt = self._load_agent_prompt(agent or "")
            else:

                try:
                    pp = _prompt_path(mode=mode)
                    logger.debug("Prompt selection: mode=%r → %s", mode, pp)
                    agent_prompt = pp.read_text(encoding="utf-8")
                except OSError:
                    agent_prompt = ""
            if agent_prompt:
                agent_prompt = self._resolve_prompt_templates(
                    agent_prompt, session_key or ""
                )
                agent_prompt = self._substitute_bot_name(agent_prompt)
                parts.append(
                    f"[AGENT SYSTEM PROMPT]\n{agent_prompt}\n[END AGENT SYSTEM PROMPT]\n\n"
                )
            session_ctx = self.build_session_context(
                session_key,
                agent=agent,
                resumed=resumed,
                workspace=workspace,
                memory_store=memory_store,
                compressed_history=None,
                mode=mode,
                blocks_reads=blocks_reads,
                provider_type=provider_type,
                exclude_last_n=exclude_last_n,
            )
            if session_ctx:
                parts.append(
                    "[SESSION CONTEXT — background reference only, NOT a task to act on.\n"
                    "This is your memory, lessons, and conversation history from prior "
                    "sessions. Use it to stay consistent but ONLY respond to the "
                    "CURRENT USER REQUEST below.]\n" + session_ctx + "[END OF SESSION CONTEXT]\n\n"
                )
            # Session replay: inject OUTSIDE the capped session context so it
            # doesn't get truncated at 165K. This is the full conversation
            # history from KiroClaw's conversation_log — provider-agnostic.
            if compressed_history:
                parts.append(
                    "[CONVERSATION HISTORY — recent session replay, tail-heavy, may be truncated]\n"
                    + compressed_history
                    + "\n[END CONVERSATION HISTORY]\n\n"
                )

        # Channel history — inject on every message for group channel context
        ch_ctx: str | None = None
        if channel_id and self.channel_history:
            ch_ctx = self.channel_history.context_for(channel_id, thread_ts=thread_ts) or None
            if ch_ctx:
                parts.append(ch_ctx)

        # Thread parent text — inject whenever available, even alongside
        # channel history (they serve different purposes: ch_ctx has recent
        # messages, parent text has the original post that started the thread).
        if channel_id and thread_ts and thread_parent_text:
            parts.append(
                "[SLACK THREAD CONTEXT]\n"
                f"channel_id: {channel_id}\n"
                f"thread_ts: {thread_ts}\n"
                "This thread was started by a prior session. "
                "Here is what was posted:\n"
                f"{thread_parent_text}\n"
                "If you need more context from this thread, use the Slack MCP "
                "tool (e.g. batch_get_thread_replies) with the identifiers above.\n"
                "[END SLACK THREAD CONTEXT]\n\n"
            )
        elif channel_id and thread_ts:
            # No parent text — provide bare thread metadata so the LLM
            # always knows it's in a thread and can fetch context via MCP tools.
            parts.append(
                "[SLACK THREAD CONTEXT]\n"
                f"channel_id: {channel_id}\n"
                f"thread_ts: {thread_ts}\n"
                "You are responding inside a Slack thread. If you need prior "
                "conversation context that is not shown above, use the Slack MCP "
                "tool (e.g. batch_get_thread_replies) with these identifiers.\n"
                "[END SLACK THREAD CONTEXT]\n\n"
            )

        # Trust ACP native history for follow-up messages — do NOT inject
        # a parallel transcript reminder. Only inject
        # transcript on new sessions (via build_session_context), never
        # on follow-ups. Dual sources of truth cause contradictions.
        logger.info(
            "🔍 build_message: session_key=%s is_new=%s resumed=%s "
            "has_channel_history=%s injected_parts=%d",
            session_key,
            is_new_session,
            resumed,
            bool(channel_id and self.channel_history),
            len(parts),
        )

        # Episodic memory — only on new sessions to avoid cross-thread contamination;
        # ACP native history already provides in-thread context for follow-ups.
        # Skipped for temporary sessions.
        if blocks_reads:
            logger.info("🔍 Temporary session — episodic memory skipped")
        elif is_new_session:
            memory = self.get_memory_for(memory_store or workspace)
            if memory.vector_store:
                episodic_ctx = memory.vector_store.get_episodic_context(
                    query_text=text, cap=3000,
                )
                if episodic_ctx:
                    parts.append(episodic_ctx + "\n")
                    logger.info("🔍 Injected episodic memory (%d chars)", len(episodic_ctx))
            else:
                logger.info("🔍 No vector store — episodic memory skipped")
        else:
            logger.info("🔍 Follow-up message — episodic memory skipped (trust ACP)")

        # Project context — inject on every message so the LLM always knows
        # the active project, even when set/changed after session start.
        if project:
            parts.append(
                f"[PROJECT] Active project directory: {project}\n"
                "This is the codebase you are working in for this session. "
                "File search, @-mentions, and code references are scoped to "
                "this directory. Prefer files and patterns from this project "
                "when answering questions.\n\n"
            )

        # Triggered skills (on-demand, any message) — skip for custom agents
        if not is_custom:
            triggered = self.skills.get_triggered_skills(text)
            if triggered:
                logger.info("Triggered skills: %s", ", ".join(triggered))
            for name in triggered:
                content = self.skills.load_skill(name)
                if content:
                    stripped = self.skills.strip_frontmatter(content)
                    parts.append(f"[Skill: {name}]\n{stripped}\n[End of skill]\n\n")

        # Hook-injected context — apply to all agents
        if hook_result.action == HOOK_INJECT_CONTEXT:
            parts.append(f"[Hook context:]\n{hook_result.text}\n[End of hook context]\n\n")

        # Action button context — structured payload from inline button click
        if action_context:
            parts.append(action_context + "\n\n")

        # The actual message (possibly modified by transform hook)
        if parts:
            if thread_meta:
                parts.append(thread_meta)
            if user_display_name:
                parts.append(f"[CURRENT USER] {user_display_name}\n")
            parts.append("[CURRENT USER REQUEST — respond to this]\n")
        if hook_result.action == HOOK_MODIFY:
            parts.append(hook_result.text)
        else:
            parts.append(text)

        # Lightweight reminder for interactive choices — tool differs by provider
        if interactive:
            if provider_type == "claude_code":
                parts.append(
                    "\n\n(If presenting choices, use the AskUserQuestion tool to render interactive option buttons. "
                    "Users can select an option or type a custom answer.)"
                )
            else:
                parts.append(
                    "\n\n(If presenting choices, end with [OPTIONS: choice1 | choice2 | choice3]. "
                    "Users can select multiple options before submitting.)"
                )

        # Widget instructions live in the bundled `widgets` skill.

        return "".join(parts).translate(_MULTIBYTE_TABLE), hook_result
