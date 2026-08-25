"""Domain dictionary — corrects speech-to-text misrecognitions.

Speech recognition mangles project nouns ("dynamo db" → "DynamoDB"). The user
maintains a small TOML dictionary of alias → correct-spelling pairs and every
transcription line passes through it before reaching an agent.

File format (``<data>/dictionary.toml``)::

    [[term]]
    correct = "DynamoDB"
    aliases = ["dynamo db", "dynamo d.b."]

Matching is case-insensitive with word boundaries; the longest alias wins so a
multi-word alias is not shadowed by one of its own prefixes.

The parse is deliberately paranoid: this file is user-editable AND writable by
the agent's own file tools, so a malformed or hostile document must degrade to
"no corrections", never raise into a request handler.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any, Callable

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger("kirocrew.app.meetings")

# An alias is matched with \b…\b, so a regex-special alias must be escaped (it
# is) and an absurdly long one must be refused (a 10k-char alias compiled 500
# times is a cheap CPU sink for something the user never intended).
_MAX_ALIAS_LEN = 120
_MAX_CORRECT_LEN = 120
_DICTIONARY_HEADER = "# Domain dictionary for meetings speech-to-text correction\n"


def _literal_replacement(value: str) -> Callable[[re.Match[str]], str]:
    """A ``re.sub`` replacement that inserts *value* verbatim.

    ``re.sub``'s replacement argument is a TEMPLATE, not a literal, so passing the
    correct spelling directly made a backslash in it an escape sequence:
    ``"C:\\Users"`` raises ``error: bad escape \\U`` and ``"a\\1b"`` silently
    substitutes a capture group. The term is user-supplied text from the
    pronunciation dictionary, and :meth:`DomainDictionary.correct` runs on every
    transcript segment before dispatch — so one such term killed the correction
    path for the whole meeting.

    A named function rather than an inline ``lambda`` with a default argument:
    the default-argument form defeats mypy's inference (``Cannot infer type of
    lambda``), and this reads as what it is.
    """
    return lambda _match: value


class DomainDictionary:
    """Applies alias → correct-spelling substitutions to transcription text."""

    def __init__(self) -> None:
        self.terms: list[tuple[str, list[str]]] = []
        self._compiled: list[tuple[str, re.Pattern[str]]] = []

    # -- loading --

    def load(self, path: Path) -> None:
        """(Re)load from *path*. A missing or malformed file clears the dictionary."""
        self.terms = []
        self._compiled = []
        if not path.is_file():
            return
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("meetings: failed to parse dictionary at %s: %s", path, exc)
            return
        self.load_terms(data.get("term", []))

    def load_terms(self, entries: Any) -> None:
        """Populate from a list of ``{"correct":…, "aliases":[…]}`` dicts."""
        self.terms = []
        self._compiled = []
        if not isinstance(entries, list):
            return
        for entry in entries[: k.MAX_DICTIONARY_TERMS]:
            if not isinstance(entry, dict):
                continue
            correct = entry.get("correct")
            aliases = entry.get("aliases")
            if not isinstance(correct, str) or not isinstance(aliases, list):
                continue
            correct = correct.strip()[:_MAX_CORRECT_LEN]
            clean = [
                a.strip()[:_MAX_ALIAS_LEN]
                for a in aliases
                if isinstance(a, str) and a.strip()
            ]
            if correct and clean:
                self.terms.append((correct, clean))
        self._compile()

    def _compile(self) -> None:
        # Longest alias first so "a w s s three" wins over "a w s".
        replacements: list[tuple[str, str]] = [
            (alias, correct) for correct, aliases in self.terms for alias in aliases
        ]
        replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
        for alias, correct in replacements:
            self._compiled.append((correct, re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)))

    # -- applying --

    def correct(self, text: str) -> str:
        """Apply every correction to *text* (identity when empty)."""
        if not self._compiled or not text:
            return text
        for correct, pattern in self._compiled:
            text = pattern.sub(_literal_replacement(correct), text)
        return text

    # -- serializing --

    def as_list(self) -> list[dict[str, Any]]:
        return [{"correct": c, "aliases": list(a)} for c, a in self.terms]

    def render_toml(self) -> str:
        """Render the current terms back to TOML.

        Values go through ``json.dumps`` because TOML basic strings and JSON
        strings share an escaping grammar for the characters that matter here —
        so a quote or backslash in a term can never break out of its string and
        inject a new ``[[term]]`` table.
        """
        parts = [_DICTIONARY_HEADER]
        for correct, aliases in self.terms:
            parts.append(
                f"\n[[term]]\ncorrect = {json.dumps(correct)}\n"
                f"aliases = {json.dumps(aliases)}\n"
            )
        return "".join(parts)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, self.render_toml())

    # -- mutation --

    def add_term(self, correct: str, aliases: list[str]) -> None:
        """Add or replace a term. Raises ValueError on invalid input."""
        correct = (correct or "").strip()
        clean = [a.strip() for a in aliases if isinstance(a, str) and a.strip()]
        if not correct or not clean:
            raise ValueError("correct and at least one alias are required")
        if len(correct) > _MAX_CORRECT_LEN or any(len(a) > _MAX_ALIAS_LEN for a in clean):
            raise ValueError("term or alias is too long")
        if len(self.terms) >= k.MAX_DICTIONARY_TERMS:
            raise ValueError(f"dictionary is limited to {k.MAX_DICTIONARY_TERMS} terms")
        existing = [(c, a) for c, a in self.terms if c.lower() != correct.lower()]
        existing.append((correct, clean))
        self.load_terms([{"correct": c, "aliases": a} for c, a in existing])

    def remove_term(self, correct: str) -> bool:
        """Remove a term by its correct spelling. Returns True if it existed."""
        target = (correct or "").strip().lower()
        kept = [(c, a) for c, a in self.terms if c.lower() != target]
        if len(kept) == len(self.terms):
            return False
        self.load_terms([{"correct": c, "aliases": a} for c, a in kept])
        return True
