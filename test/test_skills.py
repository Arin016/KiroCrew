"""Tests for skills module."""

from __future__ import annotations

import pytest

from kiro_claw.config.loader import KiroClawConfig, SkillsConfig
from kiro_claw.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _isolate_extra_paths(monkeypatch):
    """SkillsLoader.__init__ reads global config for ``skills.extra_paths``;
    isolate it so a developer's local ~/.kiroclaw extra_paths don't bleed into
    these hermetic loader tests. Tests that need extra_paths pass ``config=``."""
    monkeypatch.setattr(
        KiroClawConfig, "load",
        classmethod(lambda cls: KiroClawConfig(skills=SkillsConfig(extra_paths=[]))),
    )


def _create_skill(skills_dir, name, content):
    """Helper to create a skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)


class TestSkillsLoader:
    def test_list_empty(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.list_skills() == []

    def test_list_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\n---\n# Weather\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        skills = loader.list_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "weather"
        assert skills[0]["description"] == "Get weather info"

    def test_load_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "test", "---\nname: test\n---\n# Test Skill\nDo stuff.")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        content = loader.load_skill("test")
        assert content is not None
        assert "Test Skill" in content

    def test_load_missing_skill(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.load_skill("nonexistent") is None

    def test_always_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "memory",
            "---\nname: memory\ndescription: Memory system\nalways: true\n---\n# Memory\n",
        )
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Weather\n---\n# Weather\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        always = loader.get_always_skills()
        assert "memory" in always
        assert "weather" not in always

    def test_get_context(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "memory",
            "---\nname: memory\ndescription: Memory system\nalways: true\n---\n# Memory\nUse it.",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ctx = loader.get_context()
        assert "[Skills:]" in ctx
        assert "Memory" in ctx
        assert "Use it." in ctx

    def test_get_context_empty(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "empty", install_builtins=False)
        assert loader.get_context() == ""


class TestTriggeredSkills:
    """Tests for fuzzy trigger matching (P397239580)."""

    def _loader_with_skill(self, tmp_path, triggers, monkeypatch=None):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            f"---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: {triggers}\n---\n# Tiny URL\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        if monkeypatch is not None:
            from unittest.mock import MagicMock

            monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())
        return loader

    def test_exact_trigger_match(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("make a tiny url for me")

    def test_reworded_query_matches(self, tmp_path, monkeypatch):
        """Words present but not contiguous — should still match."""
        loader = self._loader_with_skill(tmp_path, "shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("can you shorten this url please")

    def test_fuzzy_partial_overlap(self, tmp_path, monkeypatch):
        """≥70% word overlap triggers the skill."""
        loader = self._loader_with_skill(tmp_path, "shorten this url", monkeypatch)
        # 2 of 3 words = 66% → no match
        assert "tiny-url" not in loader.get_triggered_skills("shorten this link")
        # 3 of 3 words = 100% → match
        assert "tiny-url" in loader.get_triggered_skills("shorten this url now")

    def test_no_match_unrelated(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert loader.get_triggered_skills("check my pipeline health") == []

    def test_always_skills_excluded(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "always-on",
            "---\nname: always-on\nalways: true\ntriggers: hello world\n---\n# Always\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("hello world") == []

    def test_case_insensitive(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "Tiny URL", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("Make a TINY url")

    def test_multiple_triggers_first_wins(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        result = loader.get_triggered_skills("shorten this url for me")
        assert result.count("tiny-url") == 1

    def test_empty_triggers_no_crash(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "no-trigger",
            "---\nname: no-trigger\ndescription: No triggers\n---\n# No Trigger\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("anything") == []


class TestSkillsCRUD:
    def test_create_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.create_skill("my-tool", "---\nname: my-tool\n---\n# My Tool\n")
        assert ok is True
        assert (skills_dir / "my-tool" / "SKILL.md").exists()
        content = loader.load_skill("my-tool")
        assert "My Tool" in content

    def test_create_duplicate_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "existing", "# Existing\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.create_skill("existing", "# New\n")
        assert ok is False

    def test_update_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "updatable", "# Old\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.update_skill("updatable", "# Updated\nNew content.")
        assert ok is True
        content = loader.load_skill("updatable")
        assert "Updated" in content

    def test_update_missing_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.update_skill("nonexistent", "# Nope\n")
        assert ok is False

    def test_delete_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "deletable", "# Delete me\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.load_skill("deletable") is not None
        ok = loader.delete_skill("deletable")
        assert ok is True
        assert loader.load_skill("deletable") is None
        assert not (skills_dir / "deletable").exists()

    def test_delete_missing_fails(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        ok = loader.delete_skill("nonexistent")
        assert ok is False

    def test_path_traversal_load(self, tmp_path):
        """Path traversal in skill name must be rejected."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.load_skill("../../etc/passwd") is None
        assert loader.load_skill("../secret") is None
        assert loader.load_skill("foo/bar") is None

    def test_path_traversal_create(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.create_skill("../escape", "# bad") is False
        assert loader.create_skill("", "# bad") is False
        # Nested paths are now allowed
        assert loader.create_skill("foo/bar", "# nested") is True

    def test_create_then_list(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        loader.create_skill(
            "alpha",
            "---\nname: alpha\ndescription: Alpha skill\n---\n# Alpha\n",
        )
        loader.create_skill(
            "beta",
            "---\nname: beta\ndescription: Beta skill\n---\n# Beta\n",
        )
        skills = loader.list_skills()
        names = [s["name"] for s in skills]
        assert "alpha" in names
        assert "beta" in names


class TestTriggerMatching:
    """Tests for word-overlap matching with negative keywords and max_triggered."""

    def test_basic_trigger_match(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\ntriggers: weather forecast\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        mock_config = MagicMock()
        mock_config.skills.max_triggered = 3
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_config)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("what's the weather forecast today")
        assert "weather" in result

    def test_negative_trigger_excludes(self, tmp_path, monkeypatch):
        """Negative trigger !keyword should exclude the skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "code-search",
            "---\nname: code-search\ndescription: Search code\ntriggers: search code, !search examples\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        mock_config = MagicMock()
        mock_config.skills.max_triggered = 3
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_config)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        # Positive match without negative words
        assert "code-search" in loader.get_triggered_skills("search code repositories")
        # Negative trigger fires — "search" and "examples" both present
        assert "code-search" not in loader.get_triggered_skills("search for code examples")

    def test_max_triggered_limit(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir,
                f"skill{i}",
                f"---\nname: skill{i}\ndescription: Skill {i}\ntriggers: test\n---\n",
            )
        # max_triggered is snapshotted at construction, so inject via config=.
        cfg = KiroClawConfig(skills=SkillsConfig(max_triggered=2))
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)

        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert len(result) == 2

    def test_sort_by_overlap_score(self, tmp_path, monkeypatch):
        """Higher overlap skills should appear first."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # "good" trigger "alpha beta gamma delta" → 3/4 = 0.75 (above 0.7)
        _create_skill(
            skills_dir,
            "good",
            "---\nname: good\ndescription: Good\ntriggers: alpha beta gamma delta\n---\n",
        )
        # "better" trigger "alpha beta gamma" → 3/3 = 1.0
        _create_skill(
            skills_dir,
            "better",
            "---\nname: better\ndescription: Better\ntriggers: alpha beta gamma\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        mock_config = MagicMock()
        mock_config.skills.max_triggered = 5
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_config)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("alpha beta gamma")
        assert len(result) == 2
        assert result[0] == "better"  # higher overlap first
        assert result[1] == "good"

    def test_always_on_skills_skipped(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir, "always", "---\nname: always\nalways: true\ntriggers: test\n---\n"
        )
        _create_skill(
            skills_dir, "normal", "---\nname: normal\ntriggers: test\n---\n"
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        mock_config = MagicMock()
        mock_config.skills.max_triggered = 5
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_config)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert "always" not in result
        assert "normal" in result

    def test_multi_word_trigger_phrase(self, tmp_path, monkeypatch):
        """Multi-word trigger phrases should match as a unit."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: shorten url, create tiny link, make short url\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        mock_config = MagicMock()
        mock_config.skills.max_triggered = 3
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_config)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        assert "tiny-url" in loader.get_triggered_skills("please shorten this url")
        assert "tiny-url" in loader.get_triggered_skills("create a tiny link for me")
        # Single word "url" alone shouldn't trigger (1/2 = 50% < 70%)
        assert "tiny-url" not in loader.get_triggered_skills("what is a url")


class TestAutoSkillProvenance:
    """Tests for the AutoSkillProvenance dataclass frontmatter serialization."""

    def test_now_iso_is_utc(self):
        from kiro_claw.skills import AutoSkillProvenance

        stamp = AutoSkillProvenance.now_iso()
        # ISO 8601 UTC ends with +00:00 when using timezone.utc
        assert "+00:00" in stamp

    def test_frontmatter_lines_minimum(self):
        from kiro_claw.skills import AutoSkillProvenance

        prov = AutoSkillProvenance(session_key="dashboard:chat-1", created_at="2026-05-05T11:30:00+00:00")
        lines = prov.to_frontmatter_lines()
        assert "source: auto" in lines
        assert "session_key: dashboard:chat-1" in lines
        assert "created_at: 2026-05-05T11:30:00+00:00" in lines
        # Optional fields omitted when unset
        assert not any(line.startswith("refined_at:") for line in lines)
        assert not any(line.startswith("reuse_count:") for line in lines)

    def test_frontmatter_lines_with_refinement(self):
        from kiro_claw.skills import AutoSkillProvenance

        prov = AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-05T11:30:00+00:00",
            refined_at="2026-05-06T09:15:00+00:00",
            reuse_count=3,
        )
        lines = prov.to_frontmatter_lines()
        assert "refined_at: 2026-05-06T09:15:00+00:00" in lines
        assert "reuse_count: 3" in lines


class TestFindSimilar:
    """Tests for SkillsLoader.find_similar description overlap dedup."""

    def test_returns_none_when_no_skills(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.find_similar("anything") is None

    def test_returns_none_when_no_overlap(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info for a city\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        assert loader.find_similar("deploy kubernetes service") is None

    def test_detects_near_duplicate(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "ssh-timber",
            "---\nname: ssh-timber\ndescription: SSH chained log search on Timber production hosts\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        match = loader.find_similar(
            "SSH chained log search on Timber production hosts", threshold=0.8
        )
        assert match == "ssh-timber"

    def test_exclude_self_during_refine(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "auto/foo",
            "---\nname: auto/foo\ndescription: One two three four five keywords\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        # Without exclude, we match ourselves
        assert loader.find_similar("one two three four five keywords") == "auto/foo"
        # With exclude, we don't
        assert loader.find_similar("one two three four five keywords", exclude="auto/foo") is None

    def test_threshold_respected(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "alpha",
            "---\nname: alpha\ndescription: one two three four five six seven\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        # 3/10 = 0.3 overlap — below 0.85 threshold, rejected
        assert loader.find_similar("one two three eight nine ten eleven") is None


class TestIsAutoGenerated:
    """Tests for the auto/<name> namespace check."""

    def test_true_for_auto_prefix(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.is_auto_generated("auto/foo") is True
        assert loader.is_auto_generated("auto/debug-timber-logs") is True

    def test_false_for_manual(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.is_auto_generated("ssh-timber") is False
        assert loader.is_auto_generated("utils/tiny-url") is False

    def test_false_for_unsafe_name(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Path traversal defence — safe_name check kicks in first
        assert loader.is_auto_generated("../auto/foo") is False
        assert loader.is_auto_generated("auto/..\\bar") is False


class TestCreateAutoSkill:
    """Tests for SkillsLoader.create_auto_skill."""

    def _make_provenance(self):
        from kiro_claw.skills import AutoSkillProvenance

        return AutoSkillProvenance(
            session_key="dashboard:chat-1",
            created_at="2026-05-05T11:30:00+00:00",
        )

    def test_creates_skill_under_auto_namespace(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        name = loader.create_auto_skill(
            "debug-timber",
            description="Debug Timber log searches",
            triggers="timber, debug, log search",
            procedure_md="## When\nSSH chain patterns\n",
            provenance=self._make_provenance(),
        )
        assert name == "auto/debug-timber"
        skill_file = tmp_path / "skills" / "auto" / "debug-timber" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        assert "name: auto/debug-timber" in content
        assert "source: auto" in content
        assert "session_key: dashboard:chat-1" in content
        assert "created_at: 2026-05-05T11:30:00+00:00" in content
        assert "triggers: timber, debug, log search" in content
        assert "SSH chain patterns" in content

    def test_rejects_invalid_slug(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Too short
        assert loader.create_auto_skill(
            "ab",
            description="desc",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) is None
        # Spaces
        assert loader.create_auto_skill(
            "bad name",
            description="desc",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) is None
        # Leading hyphen
        assert loader.create_auto_skill(
            "-bad",
            description="desc",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) is None
        # Path traversal
        assert loader.create_auto_skill(
            "../evil",
            description="desc",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) is None

    def test_rejects_oversized_procedure(self, tmp_path):
        from kiro_claw.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        huge = "x" * (AUTO_SKILL_MAX_PROCEDURE_CHARS + 1)
        assert loader.create_auto_skill(
            "test-skill",
            description="desc",
            triggers="",
            procedure_md=huge,
            provenance=self._make_provenance(),
        ) is None

    def test_refuses_duplicate(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.create_auto_skill(
            "duplicate-name",
            description="desc",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) == "auto/duplicate-name"
        # Second call with same slug is rejected
        assert loader.create_auto_skill(
            "duplicate-name",
            description="different",
            triggers="",
            procedure_md="different body",
            provenance=self._make_provenance(),
        ) is None


class TestUpdateAutoSkill:
    """Tests for SkillsLoader.update_auto_skill (refine path)."""

    def _make_provenance(self, refined_at=""):
        from kiro_claw.skills import AutoSkillProvenance

        return AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-05T11:30:00+00:00",
            refined_at=refined_at,
        )

    def test_refuses_manual_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "manual-skill",
            "---\nname: manual-skill\ndescription: hand-crafted\n---\n# Manual\nHand-authored content.\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        # Refuse to update a hand-authored skill even if the caller asks
        assert loader.update_auto_skill(
            "manual-skill",
            description="trying to overwrite",
            triggers="",
            procedure_md="new body",
            provenance=self._make_provenance(),
        ) is False
        # Original content untouched
        content = (skills_dir / "manual-skill" / "SKILL.md").read_text()
        assert "Hand-authored content" in content

    def test_updates_auto_skill(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Create then refine
        loader.create_auto_skill(
            "refine-me",
            description="original desc",
            triggers="",
            procedure_md="## Step 1\nrun X\n",
            provenance=self._make_provenance(),
        )
        ok = loader.update_auto_skill(
            "auto/refine-me",
            description="refined desc",
            triggers="refine, me",
            procedure_md="## Step 1\nrun Y (better)\n",
            provenance=self._make_provenance(refined_at="2026-05-06T09:00:00+00:00"),
        )
        assert ok is True
        content = (tmp_path / "skills" / "auto" / "refine-me" / "SKILL.md").read_text()
        assert "refined desc" in content
        assert "run Y (better)" in content
        assert "refined_at: 2026-05-06T09:00:00+00:00" in content

    def test_returns_false_for_missing(self, tmp_path):
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        assert loader.update_auto_skill(
            "auto/does-not-exist",
            description="x",
            triggers="",
            procedure_md="body",
            provenance=self._make_provenance(),
        ) is False


class TestListAutoSkills:
    """Tests for list_auto_skills filtering."""

    def test_filters_to_auto_namespace_only(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "manual/one",
            "---\nname: manual/one\ndescription: manual\n---\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        loader.create_auto_skill(
            "generated-one",
            description="auto",
            triggers="",
            procedure_md="body",
            provenance=(
                __import__("kiro_claw.skills", fromlist=["AutoSkillProvenance"])
                .AutoSkillProvenance(
                    session_key="x",
                    created_at="2026-05-05T11:30:00+00:00",
                )
            ),
        )
        auto_only = loader.list_auto_skills()
        assert len(auto_only) == 1
        assert auto_only[0]["key"] == "auto/generated-one"


class TestAutoNameFromTitleTruncation:
    """Regression test for #6: trailing hyphen after truncation would fail regex."""

    def test_trailing_hyphen_stripped_after_truncation(self):
        from kiro_claw.skills import _auto_name_from_title

        # Build a title where the 62-char boundary lands in the middle of a
        # word-separator run ("-") that would otherwise leave a trailing
        # hyphen and silently fail _AUTO_NAME_PATTERN.
        # 60 alphanumerics + 2 non-alphanumerics -> "a" * 60 + "-x"
        # After truncation at [:62], you get "a"*60 + "-x" — 62 chars, still valid.
        # A tricker case: 61 alphanumerics + non-alphanum + alphanum
        # -> "a" * 61 + "-b" -> after re.sub + strip + truncate[:62] ->
        # "a"*61 + "-" which ends in a hyphen.
        title = "a" * 61 + " b"  # Space becomes hyphen during sanitization
        slug = _auto_name_from_title(title)
        # Post-fix: trailing hyphen stripped, slug is "a"*61 -> 61 chars, valid
        assert slug
        assert not slug.endswith("-")
        assert slug == "a" * 61

    def test_normal_title_unaffected(self):
        from kiro_claw.skills import _auto_name_from_title

        assert _auto_name_from_title("Debug Timber logs via SSH") == "debug-timber-logs-via-ssh"

    def test_empty_and_invalid_inputs_still_return_empty(self):
        from kiro_claw.skills import _auto_name_from_title

        assert _auto_name_from_title("") == ""
        assert _auto_name_from_title("!!!") == ""
        # Single character is below the min length (3)
        assert _auto_name_from_title("a") == ""


class TestUpdateAutoSkillPreservesCreatedAt:
    """Regression test for #5: refine must not clobber created_at."""

    def test_created_at_preserved_across_refine(self, tmp_path):
        from kiro_claw.skills import AutoSkillProvenance

        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        original = AutoSkillProvenance(
            session_key="dashboard:chat-1",
            created_at="2026-05-05T11:00:00+00:00",
        )
        loader.create_auto_skill(
            "preserved-ts",
            description="initial",
            triggers="",
            procedure_md="v1",
            provenance=original,
        )
        # Caller passes a fresh provenance with a new created_at — the
        # update path should ignore that and preserve the original.
        bogus_new = AutoSkillProvenance(
            session_key="dashboard:chat-2",
            created_at="2026-05-06T12:00:00+00:00",  # WRONG created_at
            refined_at="2026-05-06T12:00:00+00:00",
        )
        ok = loader.update_auto_skill(
            "auto/preserved-ts",
            description="refined",
            triggers="",
            procedure_md="v2",
            provenance=bogus_new,
        )
        assert ok is True
        content = (tmp_path / "skills" / "auto" / "preserved-ts" / "SKILL.md").read_text()
        # Original created_at must survive
        assert "created_at: 2026-05-05T11:00:00+00:00" in content
        # New refined_at was honored
        assert "refined_at: 2026-05-06T12:00:00+00:00" in content
        # Session key from the refine provenance was honored (provenance
        # fields other than created_at update normally).
        assert "session_key: dashboard:chat-2" in content


def _cfg_with_extra(paths):
    """Build a config with skills.extra_paths set (isolated, no disk read)."""
    return KiroClawConfig(skills=SkillsConfig(extra_paths=paths))


class TestSkillsLoaderExtraPaths:
    def test_extra_path_skill_listed(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "roaring", "---\nname: roaring\ndescription: Roaring ops\n---\n# Roaring\n")
        loader = SkillsLoader(
            skills_path=local, install_builtins=False, config=_cfg_with_extra([str(extra)]),
        )
        names = {s["name"] for s in loader.list_skills()}
        assert "roaring" in names

    def test_local_takes_precedence(self, tmp_path):
        local = tmp_path / "local"
        extra = tmp_path / "extra"
        _create_skill(local, "dup", "---\nname: dup\ndescription: LOCAL\n---\n# Local\n")
        _create_skill(extra, "dup", "---\nname: dup\ndescription: EXTRA\n---\n# Extra\n")
        loader = SkillsLoader(
            skills_path=local, install_builtins=False, config=_cfg_with_extra([str(extra)]),
        )
        dup = [s for s in loader.list_skills() if s["name"] == "dup"]
        assert len(dup) == 1
        assert dup[0]["description"] == "LOCAL"

    def test_load_skill_from_extra_path(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "contributing", "---\nname: contributing\n---\n# Contributing\nGuide.")
        loader = SkillsLoader(
            skills_path=local, install_builtins=False, config=_cfg_with_extra([str(extra)]),
        )
        content = loader.load_skill("contributing")
        assert content is not None
        assert "Contributing" in content

    def test_local_skill_shadows_extra_on_load(self, tmp_path):
        local = tmp_path / "local"
        extra = tmp_path / "extra"
        _create_skill(local, "dup", "---\nname: dup\n---\n# LocalBody\n")
        _create_skill(extra, "dup", "---\nname: dup\n---\n# ExtraBody\n")
        loader = SkillsLoader(
            skills_path=local, install_builtins=False, config=_cfg_with_extra([str(extra)]),
        )
        assert "LocalBody" in loader.load_skill("dup")

    def test_nonexistent_extra_path_ignored(self, tmp_path):
        local = tmp_path / "local"
        local.mkdir()
        loader = SkillsLoader(
            skills_path=local, install_builtins=False,
            config=_cfg_with_extra([str(tmp_path / "missing")]),
        )
        assert loader._extra_paths == []

    def test_sensitive_extra_path_skipped(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setattr("kiro_claw.skills.is_sensitive_path", lambda p: True)
        loader = SkillsLoader(
            skills_path=tmp_path / "local", install_builtins=False,
            config=_cfg_with_extra([str(extra)]),
        )
        assert loader._extra_paths == []

    def test_sensitive_skill_file_skipped(self, tmp_path, monkeypatch):
        # Extra dir is allowed, but an individual SKILL.md is flagged sensitive:
        # it must be excluded from listing AND refused on load.
        local = tmp_path / "local"
        local.mkdir()
        extra = tmp_path / "extra"
        _create_skill(extra, "x", "---\nname: x\n---\n# X\n")
        # Both listing (_iter) and load_skill route extra-path reads through
        # validate_file_path — flagging it there blocks both.
        monkeypatch.setattr(
            "kiro_claw.skills.validate_file_path",
            lambda p: None if str(p).endswith("SKILL.md") else p,
        )
        loader = SkillsLoader(
            skills_path=local, install_builtins=False, config=_cfg_with_extra([str(extra)]),
        )
        assert "x" not in {s["name"] for s in loader.list_skills()}
        assert loader.load_skill("x") is None


class TestTriggerPerformance:
    """Per-message cost reductions in get_triggered_skills (perf)."""

    def test_single_audit_event_for_triggered_set(self, tmp_path, monkeypatch):
        """One SEL event per call (for the matched set), not one per skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # Several skills; only one will match the query.
        _create_skill(skills_dir, "tiny-url",
                      "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n")
        _create_skill(skills_dir, "weather",
                      "---\nname: weather\ndescription: d\ntriggers: weather forecast\n---\n# x\n")
        _create_skill(skills_dir, "pipeline",
                      "---\nname: pipeline\ndescription: d\ntriggers: pipeline health\n---\n# x\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: fake_sel)
        triggered = loader.get_triggered_skills("please shorten this url")

        assert "tiny-url" in triggered
        # Exactly ONE audit event, regardless of how many skills were scanned.
        assert fake_sel.log_tool_invocation.call_count == 1

    def test_no_audit_event_when_nothing_triggers(self, tmp_path, monkeypatch):
        """The common case (no match) writes zero SEL events."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "tiny-url",
                      "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: fake_sel)
        assert loader.get_triggered_skills("hello there friend") == []
        assert fake_sel.log_tool_invocation.call_count == 0

    @pytest.mark.parametrize(
        "triggers",
        [
            "shorten url, !test",  # positive first
            "!test, shorten url",  # negative first — must be order-independent
        ],
    )
    def test_negative_trigger_exclusion_is_audited(self, tmp_path, monkeypatch, triggers):
        """A negative trigger that excludes an otherwise-matching skill is a
        permission DENY and must still emit an audit event (with the negated
        skill in metadata), regardless of trigger order in the frontmatter."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "tiny-url",
                      f"---\nname: tiny-url\ndescription: d\ntriggers: {triggers}\n---\n# x\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: fake_sel)
        # "shorten url" matches the positive trigger, but "test" fires the
        # negative trigger → excluded.
        triggered = loader.get_triggered_skills("shorten url for this test")

        assert triggered == []  # excluded by negative trigger
        # The denial is audited (one event), with the skill named in metadata —
        # even when "!test" is listed before "shorten url".
        assert fake_sel.log_tool_invocation.call_count == 1
        _, kwargs = fake_sel.log_tool_invocation.call_args
        assert kwargs["outcome"] == "denied"
        assert "tiny-url" in kwargs["metadata"]["negated"]

    def test_iter_result_is_cached(self, tmp_path, monkeypatch):
        """The skill-file walk is cached, not re-run on every call."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "tiny-url",
                      "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        orig = loader._iter_uncached

        def _counting():
            calls["n"] += 1
            return orig()

        monkeypatch.setattr(loader, "_iter_uncached", _counting)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        # 5 messages, but the underlying walk ran at most once (TTL cache).
        assert calls["n"] <= 1

    def test_config_not_loaded_per_message(self, tmp_path, monkeypatch):
        """get_triggered_skills must not re-load config on every message — the
        trigger cap is snapshotted at construction (max_triggered hoist)."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(skills_dir, "tiny-url",
                      "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n")
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        real_load = KiroClawConfig.load

        def _counting_load():
            calls["n"] += 1
            return real_load()

        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", _counting_load)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        # Zero config loads across 5 messages — the cap was snapshotted in __init__.
        assert calls["n"] == 0

    def test_max_triggered_snapshot_caps_results(self, tmp_path, monkeypatch):
        """The snapshotted max_triggered still bounds the result count."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir, f"s{i}",
                f"---\nname: s{i}\ndescription: d\ntriggers: shorten url\n---\n# x\n",
            )
        cfg = KiroClawConfig()
        cfg.skills.max_triggered = 2
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg)
        monkeypatch.setattr("kiro_claw.skills.sel", lambda: MagicMock())

        triggered = loader.get_triggered_skills("shorten url")
        assert len(triggered) == 2
