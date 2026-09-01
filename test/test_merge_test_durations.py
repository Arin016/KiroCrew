"""Tests for scripts/merge_test_durations.py.

The merge step feeds pytest-split's shard balancer, so its failure modes are the
interesting part: a partial set of shards must NOT produce a ledger (it would
silently tell pytest-split that the missing tests take zero time and skew every
future run), and overlapping shards must be refused outright (that would mean
pytest-split's disjoint-group contract broke and the merge is unsound).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "merge_test_durations.py"


def _load():
    spec = importlib.util.spec_from_file_location("merge_test_durations", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mtd = _load()


def _shard(directory: pathlib.Path, index: int, payload: dict[str, float]) -> pathlib.Path:
    path = directory / f".test_durations_shard_{index}"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(shard_dir: pathlib.Path, expected: int, out: pathlib.Path) -> int:
    return mtd.main(["--shard-dir", str(shard_dir), "--expected", str(expected), "--out", str(out)])


class TestTheHappyPath:
    def test_disjoint_shards_merge_into_one_ledger(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {"test/a.py::test_one": 1.5})
        _shard(shards, 2, {"test/b.py::test_two": 2.5})
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 0

        merged = json.loads(out.read_text(encoding="utf-8"))
        assert merged == {"test/a.py::test_one": 1.5, "test/b.py::test_two": 2.5}

    def test_output_is_byte_stable_across_reruns(self, tmp_path: pathlib.Path) -> None:
        # sort_keys is what keeps the cache entry stable; without it the same
        # input can serialise two ways and every run looks like a real change.
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {"z": 1.0, "a": 2.0})
        _shard(shards, 2, {"m": 3.0})
        first, second = tmp_path / "one", tmp_path / "two"

        assert _run(shards, 2, first) == 0
        assert _run(shards, 2, second) == 0
        assert first.read_bytes() == second.read_bytes()


class TestAPartialSetIsNotAnError:
    """Missing a shard must be a warning + no ledger, never a red build."""

    @pytest.mark.parametrize("present", [0, 1, 3])
    def test_fewer_shards_than_expected_writes_nothing(
        self, tmp_path: pathlib.Path, present: int
    ) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        for i in range(1, present + 1):
            _shard(shards, i, {f"test/s{i}.py::t": 1.0})
        out = tmp_path / ".test_durations"

        assert _run(shards, 4, out) == 0
        assert not out.exists()

    def test_more_shards_than_expected_also_writes_nothing(self, tmp_path: pathlib.Path) -> None:
        # An unexpected extra file means the shape changed; do not guess.
        shards = tmp_path / "shards"
        shards.mkdir()
        for i in range(1, 4):
            _shard(shards, i, {f"test/s{i}.py::t": 1.0})
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 0
        assert not out.exists()


class TestUnsoundInputIsRefused:
    def test_overlapping_shards_fail(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {"test/dup.py::test_x": 1.0})
        _shard(shards, 2, {"test/dup.py::test_x": 9.0})
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 1
        assert not out.exists()

    def test_all_shards_empty_fails(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {})
        _shard(shards, 2, {})
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 1
        assert not out.exists()

    def test_unparseable_shard_fails(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {"test/a.py::t": 1.0})
        (shards / ".test_durations_shard_2").write_text("{not json", encoding="utf-8")
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 1
        assert not out.exists()

    def test_shard_holding_a_json_list_fails(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        _shard(shards, 1, {"test/a.py::t": 1.0})
        (shards / ".test_durations_shard_2").write_text("[1, 2]", encoding="utf-8")
        out = tmp_path / ".test_durations"

        assert _run(shards, 2, out) == 1
        assert not out.exists()

    def test_nonsense_expected_count_is_rejected(self, tmp_path: pathlib.Path) -> None:
        shards = tmp_path / "shards"
        shards.mkdir()
        out = tmp_path / ".test_durations"

        assert _run(shards, 0, out) == 2
        assert not out.exists()


class TestTheCiContract:
    def test_glob_matches_the_filename_ci_actually_writes(self) -> None:
        # ci.yml passes --durations-path .test_durations_shard_<group>; if either
        # side is renamed without the other, the merge silently finds zero files
        # and the ledger quietly stops refreshing.
        ci = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        text = ci.read_text(encoding="utf-8")
        assert "--durations-path .test_durations_shard_{0}" in text
        assert mtd.SHARD_GLOB == ".test_durations_shard_*"

    def test_durations_artifacts_are_gitignored(self) -> None:
        # The whole point of this change is that no durations file is ever
        # committed again.
        ignore = (pathlib.Path(__file__).resolve().parents[1] / ".gitignore").read_text(
            encoding="utf-8"
        )
        assert ".test_durations" in ignore
        assert ".test_durations_shard_*" in ignore
