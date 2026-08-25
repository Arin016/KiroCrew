"""The pre-swap interpreter-floor gate on both update paths.

`dep_sync.sync()` already refuses a revision the target venv cannot import, but
it runs AFTER the checkout has been replaced: the refusal is accurate and too
late, because the tree is already the new revision and the next launch fails on
an import rather than on that message. These cover the same question asked one
step earlier -- after the fetch, before the pull/reset -- where declining costs
the user only the update.

The load-bearing property is that the gate reads the INCOMING revision, which is
not checked out yet. A test that let the worktree answer would pass against a
gate that does nothing, so the fixture below deliberately makes the worktree and
the committed ref disagree and asserts the COMMITTED floor is the one enforced.

Scope: these cover the gate's own logic plus the ORDER of its two call sites. The
endpoint's 409 body is not asserted here -- reaching it requires stubbing enough
of `api_update_apply` that the test would mostly assert against its own mocks,
and driving it through the shared subprocess executor leaves a worker alive that
never lets pytest exit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew import dep_sync

GIT = "git"
_IDENT = (
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "user.name=Test",
    "-c",
    "commit.gpgsign=false",
)


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([GIT, *args], cwd=str(cwd), check=True, capture_output=True, timeout=60)


def _pyproject(floor: str) -> str:
    return f'[project]\nname = "kirocrew"\nrequires-python = "{floor}"\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose COMMITTED pyproject declares an unreachable floor.

    The worktree copy is then overwritten with a permissive floor, so any reader
    that consults the working tree instead of the object database answers
    "runnable" and the assertions below fail. That disagreement is the fixture's
    whole purpose.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    _run("init", "-q", cwd=proj)
    (proj / "pyproject.toml").write_text(_pyproject(">=3.99"), encoding="utf-8")
    _run("add", "pyproject.toml", cwd=proj)
    _run(*_IDENT, "commit", "-q", "-m", "floor", cwd=proj)
    # The worktree now disagrees with HEAD, in the direction that would hide a bug.
    (proj / "pyproject.toml").write_text(_pyproject(">=3.0"), encoding="utf-8")
    return proj


class TestBlobReader:
    def test_it_reads_the_ref_not_the_worktree(self, repo: Path) -> None:
        from_ref = dep_sync.blob_reader(repo, "HEAD", GIT)("pyproject.toml")
        from_disk = dep_sync.read_text_from(repo)("pyproject.toml")

        assert from_ref is not None and ">=3.99" in from_ref
        assert from_disk is not None and ">=3.0" in from_disk

    def test_a_missing_path_at_the_ref_reads_as_unavailable(self, repo: Path) -> None:
        assert dep_sync.blob_reader(repo, "HEAD", GIT)("no-such-file.toml") is None

    def test_an_unknown_ref_reads_as_unavailable(self, repo: Path) -> None:
        assert dep_sync.blob_reader(repo, "not-a-ref", GIT)("pyproject.toml") is None

    def test_requires_python_honours_the_reader_seam(self, repo: Path) -> None:
        """The seam has to carry the whole precedence chain, not just one file."""
        at_ref = dep_sync.requires_python(repo, dep_sync.blob_reader(repo, "HEAD", GIT))
        at_disk = dep_sync.requires_python(repo)

        assert at_ref == ">=3.99"
        assert at_disk == ">=3.0"


class TestIncomingFloorBreach:
    def test_a_higher_floor_at_the_ref_is_reported(self, repo: Path) -> None:
        assert dep_sync.incoming_floor_breach(repo, "HEAD", GIT, (3, 12, 0)) == (
            ">=3.99",
            "3.99.0",
        )

    def test_a_satisfied_floor_is_not_reported(self, repo: Path) -> None:
        assert dep_sync.incoming_floor_breach(repo, "HEAD", GIT, (3, 99, 0)) is None

    def test_an_unreadable_declaration_fails_OPEN(self, repo: Path) -> None:
        """Deliberately permissive -- and the reason is worth pinning.

        Every other refusal in `dep_sync` fails closed. This one must not: it is
        not a security boundary, and `sync()`'s post-swap refusal still stands
        behind it. Failing closed would invent a new way for updates to stop
        working on any layout this reader cannot parse, trading a rare late
        refusal for a common total block.
        """
        assert dep_sync.incoming_floor_breach(repo, "not-a-ref", GIT, (3, 12, 0)) is None

    def test_a_missing_git_binary_fails_open_rather_than_raising(self, repo: Path) -> None:
        assert (
            dep_sync.incoming_floor_breach(
                repo, "HEAD", str(repo / "definitely-not-git"), (3, 12, 0)
            )
            is None
        )


class TestBothApplyPathsGateBeforeTheDestructiveStep:
    """A source ratchet, because the two paths are easy to update one at a time.

    The manual endpoint and the unattended auto-update each replace the checkout
    with their own command (`git pull` / `git reset --hard`). A floor gate added
    to one and missed on the other leaves the unattended path -- the one with
    nobody watching -- as the case that strands an install. Asserting ORDER, not
    mere presence, is what makes this catch a gate that runs too late to help.
    """

    @staticmethod
    def _source(rel: str) -> str:
        root = Path(dep_sync.__file__).resolve().parent
        return (root / rel).read_text(encoding="utf-8")

    def test_the_dashboard_endpoint_gates_before_git_pull(self) -> None:
        src = self._source("dashboard/handlers/updates.py")
        gate = src.index("incoming_floor_breach")
        pull = src.index('"pull",')
        assert gate < pull, "the floor gate must run before `git pull` replaces the tree"

    def test_the_auto_update_gates_before_git_reset(self) -> None:
        src = self._source("slack/gateway.py")
        gate = src.index("incoming_floor_breach")
        reset = src.index('"--hard",')
        assert gate < reset, "the floor gate must run before `git reset --hard` lands"
