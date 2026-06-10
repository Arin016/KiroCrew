"""Skills loader — markdown skill files for agent capabilities."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from kiro_claw.config.loader import KiroClawConfig, config_dir
from kiro_claw.hooks import validate_file_path
from kiro_claw.security import is_sensitive_path
from kiro_claw.sel import sel

logger = logging.getLogger(__name__)


SKILLS_DIR_NAME = "skills"
_MIN_TRIGGER_OVERLAP = 0.7
# Cache the discovered skill-file list for this long. get_triggered_skills runs
# on EVERY message; without this it os.walk()s the skills dir + every extra
# path per message. Skills change rarely (add/remove via setup or sync), so a
# short TTL keeps trigger matching off the per-message filesystem hot path
# while still picking up new skills within a few seconds.
_ITER_CACHE_TTL_SECS = 5.0

# ── Auto skill creation (Mesh-677, Hermes loop) ──

# Namespace for auto-generated skills — keeps them out of the way of
# hand-authored skills.  Final path: ``~/.kiroclaw/skills/auto/<name>/SKILL.md``.
AUTO_SKILL_NAMESPACE = "auto"

# Frontmatter field used to mark a skill as auto-generated.  Absence means
# the skill is hand-authored (or legacy, pre-feature).
AUTO_SKILL_SOURCE_VALUE = "auto"

# Cap synthesized procedure markdown at 10 KB.  Longer outputs indicate
# the aux LLM failed to stay on-task and should be rejected.
AUTO_SKILL_MAX_PROCEDURE_CHARS = 10_240

# Regex for auto-generated skill name segment validation.  Deliberately
# restrictive — we control the generator so we don't need to accept
# arbitrary unicode.  ``_safe_name`` already rejects ``..`` and ``\``;
# this is an additional sanitization layer specific to auto-gen.
_AUTO_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# Bundled fallback — inside the kiro_claw package
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"


@dataclass(frozen=True)
class AutoSkillProvenance:
    """Immutable provenance record for an auto-generated skill.

    Serialized into the SKILL.md YAML frontmatter (``source: auto``,
    ``session_key``, ``created_at``, ``refined_at``, ``reuse_count``) so
    operators can always see how a skill was produced and when it was
    last refined.  Absence of ``source: auto`` identifies the skill as
    hand-authored.
    """

    session_key: str
    created_at: str  # ISO 8601 UTC
    refined_at: str = ""  # ISO 8601 UTC; empty until first refinement
    reuse_count: int = 0

    @staticmethod
    def now_iso() -> str:
        """Return the current time as an ISO 8601 UTC string."""
        return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    def to_frontmatter_lines(self) -> list[str]:
        """Serialize to the YAML key/value lines used in SKILL.md frontmatter."""
        lines = [
            f"source: {AUTO_SKILL_SOURCE_VALUE}",
            f"session_key: {self.session_key}",
            f"created_at: {self.created_at}",
        ]
        if self.refined_at:
            lines.append(f"refined_at: {self.refined_at}")
        if self.reuse_count:
            lines.append(f"reuse_count: {self.reuse_count}")
        return lines


def _auto_name_from_title(raw: str) -> str:
    """Convert a free-form title into a safe ``auto/<slug>`` skill name.

    Strategy:
    - lowercase
    - replace any run of non-alphanumerics with a single hyphen
    - strip leading/trailing hyphens
    - truncate to 62 chars (leaves room for uniqueness suffix)

    Returns the slug component only; caller prepends the namespace.
    Returns an empty string if the input can't be sanitized.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:62].rstrip("-")
    if not _AUTO_NAME_PATTERN.match(slug):
        return ""
    return slug


def _build_auto_skill_content(
    *,
    slug: str,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
) -> str:
    """Render a complete ``SKILL.md`` body for an auto-generated skill.

    Layout::

        ---
        name: auto/<slug>
        description: <description>
        triggers: <comma-separated triggers>
        source: auto
        session_key: <session>
        created_at: <iso8601>
        refined_at: <iso8601>      # omitted if empty
        reuse_count: <int>         # omitted if 0
        ---

        # <slug> (auto-generated)

        <procedure_md>

    The leading ``---`` keeps this compatible with existing frontmatter
    parsing in ``SkillsLoader._parse_frontmatter``.  YAML values are
    single-line and newline-stripped to stay within the parser's
    ``key: value`` line format.
    """
    name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
    desc_safe = re.sub(r"\s+", " ", description or "").strip() or name
    triggers_safe = re.sub(r"\s+", " ", triggers or "").strip()
    header_lines = [
        "---",
        f"name: {name}",
        f"description: {desc_safe}",
    ]
    if triggers_safe:
        header_lines.append(f"triggers: {triggers_safe}")
    header_lines.extend(provenance.to_frontmatter_lines())
    header_lines.append("---")
    # Normalize line endings, strip leading/trailing blanks so diffs
    # between revisions stay readable.
    body = procedure_md.replace("\r\n", "\n").strip()
    return "\n".join(header_lines) + "\n\n" + body + "\n"


def _project_skills_dir() -> Path | None:
    """Return project-level skills/ dir from KIROCLAW_PROJECT_DIR, or None."""
    val = os.environ.get("KIROCLAW_PROJECT_DIR")
    if val:
        p = Path(val) / "skills"
        if p.is_dir():
            return p
    return None


def _iter_skill_files(base: Path) -> list[tuple[str, Path]]:
    """Recursively find all SKILL.md files under *base*.

    Returns ``(relative_name, skill_file_path)`` pairs sorted by name.
    The relative name uses ``/`` as separator (e.g. ``utils/tiny-url``).

    Uses os.walk with followlinks=True because Python 3.12's Path.rglob
    does not follow symlinks.
    """
    results: list[tuple[str, Path]] = []
    if not base.exists():
        return results
    real_base = os.path.realpath(base)
    seen_real: set[str] = set()
    for dirpath, _dirs, files in os.walk(base, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_real:
            _dirs.clear()  # prune this branch — symlink loop
            continue
        seen_real.add(real)
        # Path containment: ensure we stay within the skill base directory
        try:
            Path(real).relative_to(real_base)
        except ValueError:
            _dirs.clear()
            continue
        if is_sensitive_path(real):
            _dirs.clear()  # never traverse into credential stores
            continue
        if "SKILL.md" in files:
            skill_file = Path(dirpath) / "SKILL.md"
            if is_sensitive_path(os.path.realpath(str(skill_file))):
                continue
            rel = skill_file.parent.relative_to(base)
            name = str(rel).replace("\\", "/")
            results.append((name, skill_file))
    return sorted(results, key=lambda x: x[0])


def _ensure_builtin_skills(base: Path) -> None:
    """Sync built-in skills: copy new/updated, remove stale.

    Supports nested directories (e.g. ``utils/tiny-url/SKILL.md``).
    Copies the entire skill directory (scripts, assets, etc.), not just SKILL.md.
    Removes skills from *base* that no longer exist in any source.
    """
    # Collect all source skill names
    source_names: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            source_names.add(name)
            src_dir = src_file.parent
            dest_dir = base / name
            dest_file = dest_dir / "SKILL.md"
            if not dest_file.exists() or src_file.stat().st_mtime > dest_file.stat().st_mtime:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(src_dir, dest_dir)
                logger.info("Synced skill: %s", name)

    # Remove known stale builtin skills (replaced by MCP tools)
    stale_builtins = {"learn", "subagent", "cron", "kiroclaw-core"}
    if base.exists():
        for name in stale_builtins:
            stale = base / name
            if stale.is_dir():
                shutil.rmtree(stale)
                logger.info("Removed stale builtin skill: %s", name)


def skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


class SkillsLoader:
    """Load skill markdown files from ~/.kiroclaw/skills/.

    Supports nested directories. Each skill is identified by its
    relative path from the skills root (e.g. ``utils/tiny-url``).

    Directory layout::

        ~/.kiroclaw/skills/
        ├── learn/SKILL.md
        ├── subagent/SKILL.md
        ├── code/
        │   ├── code-review/SKILL.md
        │   └── code-task-generation/SKILL.md
        └── utils/
            ├── url-shortener/SKILL.md
            └── mcp-debug/SKILL.md
    """

    def __init__(
        self,
        skills_path: Path | None = None,
        install_builtins: bool = True,
        config: KiroClawConfig | None = None,
    ):
        self._dir = skills_path or skills_dir()
        if install_builtins:
            _ensure_builtin_skills(self._dir)
        # Cache: path → (mtime, parsed_frontmatter)
        self._fm_cache: dict[str, tuple[float, dict[str, str]]] = {}
        # TTL cache of the discovered (name, path) list — avoids an os.walk per
        # message in get_triggered_skills. (monotonic_deadline, results)
        self._iter_cache: tuple[float, list[tuple[str, Path]]] | None = None
        # Extra skill paths from config (config injectable for testing)
        cfg = config or KiroClawConfig.load()
        # Snapshot the per-message trigger cap here so get_triggered_skills (the
        # only caller, run on EVERY message) doesn't re-load + re-validate the
        # whole config just to read one int. Matches the eventual-consistency of
        # _extra_paths below — both are resolved once from the construction-time
        # config and refreshed when the loader is rebuilt (per gateway).
        self._max_triggered = cfg.skills.max_triggered
        self._extra_paths: list[Path] = []
        for p in cfg.skills.extra_paths:
            resolved = Path(p).expanduser().resolve()
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive extra skill path: %s", p)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
            else:
                logger.debug("Extra skill path does not exist: %s", p)

    def _iter(self) -> list[tuple[str, Path]]:
        """Return all ``(name, skill_file)`` pairs, TTL-cached.

        Local skills take precedence over extra paths. The underlying os.walk
        is cached for ``_ITER_CACHE_TTL_SECS`` because this runs on every
        message via ``get_triggered_skills`` — re-walking the skills tree (plus
        every extra path) per message was a per-message latency cost.
        """
        cached = self._iter_cache
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        results = self._iter_uncached()
        self._iter_cache = (time.monotonic() + _ITER_CACHE_TTL_SECS, results)
        return results

    def _iter_uncached(self) -> list[tuple[str, Path]]:
        """Walk the skills dir + extra paths once (no caching)."""
        results = _iter_skill_files(self._dir)
        seen = {name for name, _ in results}
        for root in self._extra_paths:
            for name, skill_file in _iter_skill_files(root):
                if name in seen:
                    continue
                # Route through hooks validation (resolves symlinks + sensitive
                # check) so files read later during trigger matching are vetted.
                resolved = validate_file_path(str(skill_file))
                if resolved is None:
                    continue
                results.append((name, Path(resolved)))
                seen.add(name)
        return results

    def _invalidate_iter_cache(self) -> None:
        """Drop the cached skill-file list (after create/remove/refresh)."""
        self._iter_cache = None

    def _cached_frontmatter(self, path: Path) -> dict[str, str]:
        """Parse frontmatter with mtime-based caching."""
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._fm_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        meta = self._parse_frontmatter(path)
        self._fm_cache[key] = (mtime, meta)
        return meta

    def list_skills(self) -> list[dict]:
        """Return list of skill metadata dicts with key, name, description, path, dir, always."""
        skills: list[dict] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            skills.append(
                {
                    "key": name,
                    "name": meta.get("name", name),
                    "description": meta.get("description", name),
                    "path": str(skill_file),
                    "dir": str(skill_file.parent),
                    "always": meta.get("always", "").lower() == "true",
                }
            )
        return skills

    @staticmethod
    def _safe_name(name: str) -> bool:
        """Return True if skill name is safe (no path traversal)."""
        return bool(name) and ".." not in name and "\\" not in name

    def load_skill(self, name: str) -> str | None:
        """Load a single skill's content by name (supports nested paths)."""
        if not self._safe_name(name):
            return None
        skill_file = self._dir / name / "SKILL.md"
        if skill_file.exists():
            return skill_file.read_text(encoding="utf-8")
        # Check extra paths
        for extra in self._extra_paths:
            skill_file = extra / name / "SKILL.md"
            if skill_file.exists():
                resolved = validate_file_path(str(skill_file))
                if resolved is None:
                    logger.warning("Refusing to load skill from sensitive path: %s", skill_file)
                    continue
                return Path(resolved).read_text(encoding="utf-8")
        return None

    def create_skill(self, name: str, content: str) -> bool:
        """Create a new skill directory with SKILL.md.  Returns True on success."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if skill_dir.exists():
            return False
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # so the new skill shows in list_skills() now
        logger.info("Created skill: %s", name)
        return True

    def update_skill(self, name: str, content: str) -> bool:
        """Overwrite an existing skill's SKILL.md.  Returns True if found."""
        if not self._safe_name(name):
            return False
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            return False
        skill_file.write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
        logger.info("Updated skill: %s", name)
        return True

    def delete_skill(self, name: str) -> bool:
        """Delete a skill directory.  Returns True if found and removed."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if not skill_dir.is_dir():
            return False
        shutil.rmtree(skill_dir)
        self._invalidate_iter_cache()  # so the removal is reflected in list_skills() now
        logger.info("Deleted skill: %s", name)
        return True

    # ── Auto skill creation (Mesh-677, Hermes loop) ──

    def is_auto_generated(self, name: str) -> bool:
        """Return True if *name* refers to a skill in the auto namespace.

        Cheap filesystem check (no frontmatter parse) based on the
        directory prefix.  Used for filtering and safety guards (e.g.
        refusing to overwrite a hand-authored skill from an auto-update
        path).
        """
        if not self._safe_name(name):
            return False
        return name.startswith(f"{AUTO_SKILL_NAMESPACE}/")

    def find_similar(
        self,
        description: str,
        threshold: float = 0.85,
        *,
        exclude: str = "",
    ) -> str | None:
        """Return the name of an existing skill whose description overlaps with *description*.

        Uses case-insensitive word-set Jaccard-like overlap against every
        loaded skill's ``description`` frontmatter value:

            score = |words(a) ∩ words(b)| / |words(a) ∪ words(b)|

        Intended for deduplication of auto-generated skills — we don't
        want the agent producing a near-duplicate of an existing skill.
        Returns the first skill whose score ≥ *threshold*, or ``None``
        if nothing matches.

        *exclude* lets callers suppress self-matches during refinement.
        """
        if not description:
            return None
        query_words = set(re.findall(r"\w+", description.lower()))
        if not query_words:
            return None
        best_name: str | None = None
        best_score: float = 0.0
        for name, skill_file in self._iter():
            if exclude and name == exclude:
                continue
            meta = self._cached_frontmatter(skill_file)
            existing = meta.get("description", "")
            if not existing:
                continue
            existing_words = set(re.findall(r"\w+", existing.lower()))
            if not existing_words:
                continue
            intersection = query_words & existing_words
            union = query_words | existing_words
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score:
                best_score = score
                best_name = name
        if best_score >= threshold:
            return best_name
        return None

    def create_auto_skill(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
    ) -> str | None:
        """Write a new auto-generated skill under ``auto/<slug>/SKILL.md``.

        Returns the full skill name (``auto/<slug>``) on success, or
        ``None`` if the slug is invalid or the skill already exists.

        Caller is responsible for:
        - Running ``find_similar()`` first to avoid near-duplicates.
        - Passing already-redacted ``procedure_md`` (sensitive data is
          the caller's responsibility — this method is pure I/O).
        - Enforcing the ``skills.auto_create_from_sessions`` config flag.
        """
        if not _AUTO_NAME_PATTERN.match(slug):
            logger.warning("Rejected auto skill: slug %r failed validation", slug)
            return None
        if len(procedure_md) > AUTO_SKILL_MAX_PROCEDURE_CHARS:
            logger.warning(
                "Rejected auto skill %s: procedure %d chars exceeds cap %d",
                slug,
                len(procedure_md),
                AUTO_SKILL_MAX_PROCEDURE_CHARS,
            )
            return None
        name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
        skill_dir = self._dir / name
        if skill_dir.exists():
            logger.info("Auto skill %s already exists, skipping", name)
            return None
        content = _build_auto_skill_content(
            slug=slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        self._invalidate_iter_cache()  # new skill visible to trigger matching now
        logger.info("Created auto skill: %s", name)
        return name

    def update_auto_skill(
        self,
        name: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
    ) -> bool:
        """Update an existing auto-generated skill with a refined procedure.

        Refuses to overwrite skills NOT in the auto namespace — protects
        hand-authored skills from being clobbered by the refine path.
        Returns True on success.

        Caller is responsible for passing already-redacted ``procedure_md``.
        """
        if not self.is_auto_generated(name):
            logger.warning(
                "Refusing to auto-refine non-auto skill: %s (not in %s/)",
                name,
                AUTO_SKILL_NAMESPACE,
            )
            return False
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            return False
        if len(procedure_md) > AUTO_SKILL_MAX_PROCEDURE_CHARS:
            logger.warning(
                "Refusing to refine %s: procedure %d chars exceeds cap %d",
                name,
                len(procedure_md),
                AUTO_SKILL_MAX_PROCEDURE_CHARS,
            )
            return False
        # Preserve the original creation timestamp — refinement must not
        # clobber provenance history.  Callers typically pass a fresh
        # provenance with created_at=now; we override from the existing
        # frontmatter here so the write path is authoritative.  Uses
        # ``dataclasses.replace`` because AutoSkillProvenance is frozen.
        existing_meta = self._cached_frontmatter(skill_file)
        original_created_at = existing_meta.get("created_at")
        if original_created_at:
            provenance = replace(provenance, created_at=original_created_at)
        slug = name.split("/", 1)[1]
        content = _build_auto_skill_content(
            slug=slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )
        skill_file.write_text(content, encoding="utf-8")
        logger.info("Refined auto skill: %s", name)
        return True

    def list_auto_skills(self) -> list[dict]:
        """Return metadata dicts for all skills under the auto namespace.

        Dashboard / CLI consumers use this to display provenance to
        users.  Hand-authored skills are excluded.
        """
        return [s for s in self.list_skills() if s["key"].startswith(f"{AUTO_SKILL_NAMESPACE}/")]

    def get_always_skills(self) -> list[str]:
        """Return names of skills marked ``always: true`` in frontmatter."""
        result: list[str] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            if meta.get("always", "").lower() == "true":
                result.append(name)
        return result

    def get_triggered_skills(self, text: str) -> list[str]:
        """Return names of skills whose triggers match the given text.

        Uses word-overlap matching with multi-word trigger phrases and
        negative keywords.  Triggers are comma-separated phrases in the
        ``triggers`` frontmatter field.  A phrase prefixed with ``!`` is a
        negative trigger — if *any* negative trigger matches, the skill is
        excluded regardless of positive matches.

        Returns up to ``max_triggered`` skills sorted by best overlap score.
        """
        text_words = set(re.findall(r"\w+", text.lower()))

        scored: list[tuple[str, float]] = []
        # Skills a negative trigger actively excluded — a permission DENY that
        # must still be audited (see the audit event below).
        negated_skills: list[str] = []
        for name, skill_file in self._iter():
            meta = self._cached_frontmatter(skill_file)
            if meta.get("always", "").lower() == "true":
                continue
            triggers = meta.get("triggers", "")
            if not triggers:
                continue

            # Split into positive and negative triggers
            negated = False
            best_overlap = 0.0
            for trigger in triggers.split(","):
                trigger = trigger.strip().lower()
                if not trigger:
                    continue
                # Negative trigger: "!search" excludes if "search" words match.
                # Don't break — keep scoring the remaining positive triggers so
                # best_overlap is correct regardless of trigger order; the DENY
                # audit below needs it to know the skill would otherwise have
                # triggered (e.g. "!test, shorten url" must still compute the
                # "shorten url" overlap).
                if trigger.startswith("!"):
                    neg_words = set(re.findall(r"\w+", trigger[1:]))
                    if neg_words and neg_words <= text_words:
                        negated = True
                else:
                    trigger_words = set(re.findall(r"\w+", trigger))
                    if not trigger_words:
                        continue
                    overlap = len(trigger_words & text_words) / len(trigger_words)
                    best_overlap = max(best_overlap, overlap)

            # Only record a negation as a DENY when the skill would otherwise
            # have triggered (positive overlap met the threshold) — that's the
            # case where the negative trigger actually changed the outcome.
            if negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                negated_skills.append(name)
            elif not negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
                scored.append((name, best_overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        triggered = [name for name, _ in scored[: self._max_triggered]]

        # Emit ONE audit event for the matched + denied sets rather than one per
        # skill. Previously this wrote a SEL entry for every skill (incl. every
        # non-match) on every message — N synchronous writes per message that
        # dominated the per-message cost. The security-relevant signals are which
        # skills were injected (permission grant) and which were excluded by a
        # negative trigger (permission deny); both are captured here. Skipped
        # entirely only when nothing triggered and nothing was denied (the
        # common case).
        if triggered or negated_skills:
            metadata = {"text_hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
            if triggered:
                metadata["skills"] = ",".join(triggered)
            if negated_skills:
                metadata["negated"] = ",".join(negated_skills)
            sel().log_tool_invocation(
                session_key="skills",
                tool_name="skill_trigger",
                tool_kind="permission",
                outcome="triggered" if triggered else "denied",
                metadata=metadata,
            )
        return triggered

    def get_context(self) -> str:
        """Build skills context for prompt injection.

        Always-loaded skills: full content included.
        Other skills: summary with instruction to load via bash when needed.
        """
        always = self.get_always_skills()
        all_skills = self.list_skills()
        if not all_skills:
            return ""

        parts: list[str] = []

        # Full content for always-loaded skills
        for name in always:
            content = self.load_skill(name)
            if content:
                stripped = self.strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{stripped}")

        # Summary for on-demand skills
        on_demand = [s for s in all_skills if s["name"] not in always]
        if on_demand:
            summary_lines = [
                "## Available Skills",
                "",
                "If a user request relates to any skill below, read the full "
                "skill file first with `cat <path>` before responding.",
                "To run a skill's scripts, `cd` into its directory first.",
                "",
            ]
            for s in on_demand:
                summary_lines.append(
                    f"- **{s['name']}**: {s['description']} → `{s['path']}` (dir: `{s['dir']}`)"
                )
            parts.append("\n".join(summary_lines))

        return "[Skills:]\n" + "\n\n---\n\n".join(parts) + "\n[End of skills]\n\n"

    # ── Private ──

    @staticmethod
    def _parse_frontmatter(path: Path) -> dict[str, str]:
        """Parse YAML frontmatter from a markdown file (simple key: value)."""
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}
        meta: dict[str, str] = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip("\"'")
        return meta

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter from markdown."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content
