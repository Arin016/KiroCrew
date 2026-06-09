"""Tests for ``kiro_claw.aim_agents.list_agents`` agent-config scanning.

Focus on the robustness/security guards around scanning ``~/.kiro/agents/*.json``:
- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- A ``*.json`` symlink pointing at a sensitive credential file must NOT be read.

Tests use a tmp_path fake $HOME so the real filesystem is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_claw.aim_agents import list_agents


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


class TestListAgentsRobustness:
    def test_survives_non_utf8_and_appledouble(self, fake_home):
        """A non-UTF-8 file (AppleDouble ``._*.json`` sidecar or arbitrary
        binary ``*.json``) must be skipped, not raise UnicodeDecodeError."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        # AppleDouble sidecar: starts with "._" and is non-UTF-8 binary.
        (d / "._good.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        # Arbitrary non-UTF-8 *.json that is not an AppleDouble name either.
        (d / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_non_dict_json(self, fake_home):
        """Valid JSON that is not an object (e.g. a top-level array) must be
        skipped, not raise AttributeError on data.get()."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "array.json").write_text(json.dumps([1, 2, 3]))
        (d / "scalar.json").write_text(json.dumps("just a string"))

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """A ``*.json`` symlink under ~/.kiro/agents/ that resolves to a
        sensitive credential path must NOT be read or returned."""
        d = _agents_dir(fake_home)
        (d / "real.json").write_text(json.dumps({"name": "real"}))

        # Plant a credential file under the sensitive ~/.aws dir and symlink
        # it in as a fake agent config. Even though it is valid JSON that
        # would parse, the sensitive-path guard must skip it.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"name": "evil"}))
        (d / "evil.json").symlink_to(creds)

        names = [a.name for a in list_agents(agents_dir=d)]
        assert "evil" not in names
        assert names == ["real"]
