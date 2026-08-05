"""Host-independent tests for the worktree LIST and REMOVE paths.

Why a second file next to ``test_worktree_create.py``: that suite drives the real
sandboxed git, so every test in it calls ``_require_sandbox_exec()`` and SKIPS on a
host with no sandbox backend — which is most CI shards. The endpoint logic that
skipping leaves unexecuted is exactly the parsing and refusal reasoning that most
needs pinning, so this file fakes ``_run_git`` instead and therefore runs
everywhere. No real repository, no spawn, no skip.

The seam is deliberate: ``_run_git`` is the single chokepoint every git call in the
module goes through, so replacing it exercises the callers' own logic while leaving
the isolation contract (argv-only, credential-scrubbed, resource-capped) to the
sandbox tests in the sibling file.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.worktree import (
    _active_slot_beneath,
    _active_worktree_slots,
    _list_worktrees_detailed,
    _list_worktrees_sync,
    _norm_path,
    _remove_worktree_sync,
    _sync_result_response,
    _worktree_dirty,
    api_worktree_list,
    api_worktree_remove,
)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _porcelain(*entries: dict) -> str:
    """Build ``git worktree list --porcelain -z`` output.

    Every attribute is NUL-terminated and an extra NUL separates records, which is
    the shape the parser is written against — building it here rather than pasting a
    literal keeps the test honest about that framing.
    """
    out = []
    for e in entries:
        out.append(f"worktree {e['path']}\0")
        if e.get("head"):
            out.append(f"HEAD {e['head']}\0")
        if e.get("branch"):
            out.append(f"branch {e['branch']}\0")
        for flag in ("bare", "detached", "locked"):
            if e.get(flag):
                out.append(f"{flag}\0")
        out.append("\0")
    return "".join(out)


class _GitStub:
    """Records calls and answers from a queue keyed by the git subcommand."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        for key, val in self.answers.items():
            if key.replace("_", " ") in " ".join(args) or key == args[0]:
                return val(self) if callable(val) else val
        return _proc()

    def argv_for(self, needle: str) -> tuple[str, ...] | None:
        for args, _ in self.calls:
            if needle in " ".join(args):
                return args
        return None


@pytest.fixture
def git(monkeypatch):
    """Install a ``_GitStub`` over ``_run_git`` for the module under test."""

    def install(**answers):
        from kiro_crew.dashboard.handlers import worktree as wt

        stub = _GitStub(**answers)
        monkeypatch.setattr(wt, "_run_git", stub)
        # The dirty probe consults the filter gate first; default it to "no filter
        # driver" so a test that does not care about it still reaches `git status`.
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "")
        return stub

    return install


class TestListWorktreesDetailedParsing:
    def test_parses_branch_head_and_flags_in_git_order(self, git):
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": "/repo", "head": "a" * 40, "branch": "refs/heads/main"},
                    {"path": "/repo-wt-x", "head": "b" * 40, "branch": "refs/heads/feat/x"},
                    {"path": "/repo-wt-d", "head": "c" * 40, "detached": True},
                )
            )
        )
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/repo", "/repo-wt-x", "/repo-wt-d"]
        # `refs/heads/` is stripped; a detached entry carries no branch at all.
        assert [r["branch"] for r in recs] == ["main", "feat/x", ""]
        assert recs[2]["detached"] is True
        assert recs[0]["head"] == "a" * 40

    def test_locked_with_and_without_a_reason_both_read_as_locked(self, git):
        git(worktree_list=_proc("worktree /a\0locked\0\0worktree /b\0locked being repaired\0\0"))
        recs = _list_worktrees_detailed("/repo")

        assert [r["locked"] for r in recs] == [True, True]

    def test_a_path_containing_a_newline_stays_one_record(self, git):
        # This is the whole reason the query passes `-z`; a line-based parser would
        # split this into two bogus worktrees.
        git(worktree_list=_proc(_porcelain({"path": "/a\nb", "head": "d" * 40})))
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/a\nb"]

    def test_git_failure_is_none_not_an_empty_list(self, git):
        # The remove path keys a destructive decision off this answer, so "git could
        # not tell us" must never be indistinguishable from "nothing is registered".
        git(worktree_list=_proc("", returncode=128))

        assert _list_worktrees_detailed("/repo") is None

    def test_attributes_before_any_worktree_line_are_ignored(self, git):
        git(worktree_list=_proc("branch refs/heads/stray\0worktree /a\0\0"))
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/a"]
        assert recs[0]["branch"] == ""


class TestWorktreeDirty:
    """Every case here needs a REAL directory.

    The probe refuses to spawn git at all when the worktree path is not a
    directory (git keeps registrations for deleted trees, and a dead cwd raises
    rather than returning non-zero). A fabricated path like ``/wt`` would take
    that early exit and every assertion below would pass for the wrong reason.
    """

    def test_clean_tree_is_false_and_dirty_tree_is_true(self, git, tmp_path):
        git(status=_proc(""))
        assert _worktree_dirty(str(tmp_path)) is False

        git(status=_proc(" M file.py\n?? other\n"))
        assert _worktree_dirty(str(tmp_path)) is True

    def test_status_failure_is_unknown(self, git, tmp_path):
        git(status=_proc("", returncode=128))
        assert _worktree_dirty(str(tmp_path)) is None

    def test_a_missing_worktree_directory_is_unknown_and_spawns_nothing(self, git, tmp_path):
        """A registered tree whose directory is gone must not reach git.

        Both probes below run with the worktree as cwd, which RAISES instead of
        failing softly -- so this early exit is what keeps one stale registration
        from 500-ing the whole list request.
        """
        stub = git(status=_proc(""))
        gone = tmp_path / "vanished"  # deliberately never created

        assert _worktree_dirty(str(gone)) is None
        assert stub.calls == []

    def test_status_runs_against_the_worktree_not_the_repo_root(self, git, tmp_path):
        wt_dir = tmp_path / "some-worktree"
        wt_dir.mkdir()
        stub = git(status=_proc(""))
        _worktree_dirty(str(wt_dir))

        assert stub.calls[-1][1] == str(wt_dir)

    def test_a_filter_driver_short_circuits_before_status_runs(self, monkeypatch, tmp_path):
        from kiro_crew.dashboard.handlers import worktree as wt

        stub = _GitStub(status=_proc(" M f\n"))
        monkeypatch.setattr(wt, "_run_git", stub)
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "evil")

        # `git status` would run a `filter.<name>.clean` driver for any tracked file
        # carrying that attribute, so the answer is "unknown" and `status` is never
        # invoked at all.
        assert _worktree_dirty(str(tmp_path)) is None
        assert stub.argv_for("status") is None


class TestActiveWorktreeSlots:
    def test_maps_normalized_realpath_to_slot_key(self, tmp_path):
        real = tmp_path / "tree"
        real.mkdir()
        state = MagicMock()
        state._slots = {"chat-7": MagicMock(project=str(real))}

        active = _active_worktree_slots(state)

        assert list(active.values()) == ["chat-7"]

    def test_symlinked_project_resolves_to_the_same_entry(self, tmp_path):
        real = tmp_path / "tree"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - needs privilege
            pytest.skip("symlinks unavailable on this host")
        state = MagicMock()
        state._slots = {"chat-1": MagicMock(project=str(link))}

        active = _active_worktree_slots(state)

        assert os.path.realpath(str(real)).lower() in {k.lower() for k in active}

    def test_slots_without_a_project_are_skipped(self):
        state = MagicMock()
        state._slots = {"a": MagicMock(project=""), "b": MagicMock(project="   ")}

        assert _active_worktree_slots(state) == {}

    def test_a_state_with_no_slots_mapping_is_empty_not_an_error(self):
        state = MagicMock()
        state._slots = None

        assert _active_worktree_slots(state) == {}


class TestListWorktreesSync:
    def test_first_entry_is_main_and_active_session_is_attached(self, git, tmp_path):
        main = tmp_path / "repo"
        linked = tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "head": "a" * 40, "branch": "refs/heads/main"},
                    {"path": str(linked), "head": "b" * 40, "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )
        state = MagicMock()
        state._slots = {"chat-9": MagicMock(project=str(linked))}

        rows = _list_worktrees_sync(str(main), state)

        assert [r["is_main"] for r in rows] == [True, False]
        assert rows[0]["active_session"] is None
        assert rows[1]["active_session"] == "chat-9"
        # The EXACT key set, not a subset: `head`, `bare` and `locked` are parsed
        # from the porcelain but deliberately kept off the wire, and a field the
        # response carries is one the spec and the client type owe forever. An
        # equality assertion is what makes a future addition fail here rather
        # than ship unread.
        assert set(rows[0]) == {
            "path",
            "branch",
            "is_main",
            "detached",
            "dirty",
            "active_session",
        }

    def test_a_bare_worktree_skips_the_dirty_probe(self, git, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        stub = git(
            worktree_list=_proc(_porcelain({"path": str(bare), "bare": True})),
            status=_proc(" M f\n"),
        )
        state = MagicMock()
        state._slots = {}

        rows = _list_worktrees_sync(str(bare), state)

        # No working tree means nothing to be dirty, so the probe must not run.
        assert rows[0]["dirty"] is None
        assert stub.argv_for("status") is None

    def test_a_git_listing_failure_propagates_as_none(self, git):
        git(worktree_list=_proc("", returncode=128))
        state = MagicMock()
        state._slots = {}

        assert _list_worktrees_sync("/repo", state) is None


class TestRemoveWorktreeSyncRefusals:
    """Every refusal carries a machine-readable ``code`` and the right status."""

    def _state(self, **slots):
        state = MagicMock()
        state._slots = {k: MagicMock(project=v) for k, v in slots.items()}
        return state

    def test_a_listing_failure_refuses_503_without_touching_git_remove(self, git):
        stub = git(worktree_list=_proc("", returncode=128))

        payload, status = _remove_worktree_sync("/repo", "/repo-wt-x", False, self._state())

        assert status == 503
        assert payload["code"] == "worktree_list_unavailable"
        assert stub.argv_for("worktree remove") is None

    def test_a_path_git_does_not_list_is_404(self, git, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        git(worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})))

        payload, status = _remove_worktree_sync(
            str(main), str(tmp_path / "elsewhere"), False, self._state()
        )

        assert (status, payload["code"]) == (404, "worktree_not_found")

    def test_an_embedded_nul_is_refused_400_and_never_reaches_realpath(self, git, tmp_path):
        """A NUL makes ``realpath`` raise ``ValueError``, which is not ``OSError``.

        The handler's input validation screens the request body, but this sync half
        is also where a path first becomes a filesystem call, and a ``ValueError``
        here escapes the ``(OSError, SubprocessError)`` handler that every other
        failure lands in -- crashing the worker thread and answering 500 with no
        audit record. Screened here so the exit is an ordinary 400.
        """
        main = tmp_path / "repo"
        main.mkdir()
        git(worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})))

        payload, status = _remove_worktree_sync(
            str(main), str(tmp_path / "wt") + "\x00evil", False, self._state()
        )

        assert (status, payload["code"]) == (400, "worktree_invalid_arguments")

    def test_the_main_worktree_is_refused(self, git, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        git(worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})))

        payload, status = _remove_worktree_sync(str(main), str(main), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_main_protected")

    def test_a_worktree_live_in_another_session_is_refused_and_names_it(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            )
        )

        payload, status = _remove_worktree_sync(
            str(main), str(linked), False, self._state(**{"chat-4": str(linked)})
        )

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-4"

    def test_an_unprovable_clean_tree_is_refused_rather_than_removed(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc("", returncode=128),
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_dirty_unknown")
        assert stub.argv_for("worktree remove") is None

    def test_a_dirty_tree_is_refused_and_flagged_for_the_confirm_row(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(" M f\n"),
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_dirty")
        assert payload["dirty"] is True


class TestRemoveWorktreeSyncSuccess:
    def _listing(self, main, linked):
        return _proc(
            _porcelain(
                {"path": str(main), "branch": "refs/heads/main"},
                {"path": str(linked), "branch": "refs/heads/feat/x"},
            )
        )

    def test_clean_removal_reports_the_branch_without_a_repo_wide_prune(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(worktree_list=self._listing(main, linked), status=_proc(""))

        payload, status = _remove_worktree_sync(str(main), str(linked), False, MagicMock(_slots={}))

        assert (status, payload["ok"], payload["branch"]) == (200, True, "feat/x")
        # git is handed ITS OWN registered path, never the request string.
        assert stub.argv_for("worktree remove")[-1] == str(linked)
        # And NO repo-wide prune: `worktree remove` already deregistered this
        # tree, while prune would also drop the metadata of an unrelated worktree
        # whose volume merely happens to be unreachable right now.
        assert stub.argv_for("worktree prune") is None

    def test_force_skips_the_dirty_probe_and_passes_force_to_git(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(worktree_list=self._listing(main, linked), status=_proc(" M f\n"))

        payload, status = _remove_worktree_sync(str(main), str(linked), True, MagicMock(_slots={}))

        assert status == 200
        assert "--force" in stub.argv_for("worktree remove")
        assert stub.argv_for("status") is None

    def test_a_git_remove_failure_surfaces_as_400(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()

        def answer(stub):
            return _proc("", returncode=1, stderr="fatal: cannot remove")

        stub = git(
            worktree_list=self._listing(main, linked), status=_proc(""), worktree_remove=answer
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, MagicMock(_slots={}))

        assert (status, payload["code"]) == (400, "worktree_remove_failed")
        assert stub.argv_for("worktree prune") is None


class TestSyncResultResponse:
    def test_status_and_body_ride_through_unchanged(self):
        resp = _sync_result_response({"error": "no", "code": "x"}, 409)
        assert resp.status == 409

    def test_success_is_rendered_as_200(self):
        assert _sync_result_response({"ok": True}, 200).status == 200


def _app(*projects: str, app_claim: str | None = "", user: str = "owner") -> web.Application:
    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    state.owner_id = "owner"
    state._slots = {f"chat-{i}": MagicMock(project=str(p)) for i, p in enumerate(projects) if p}
    app["state"] = state
    app.router.add_get("/api/worktree/list", api_worktree_list)
    app.router.add_post("/api/worktree/remove", api_worktree_remove)
    return app


class TestHandlerInputValidation:
    """Argument screening happens BEFORE any path or git work, so these need no repo."""

    @pytest.mark.asyncio
    async def test_list_without_repo_is_400_with_a_code(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/worktree/list")
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_repo_required"

    @pytest.mark.asyncio
    async def test_remove_with_a_non_json_body_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_remove_with_a_json_array_body_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json=[1, 2, 3])
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_remove_with_non_string_arguments_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json={"repo": 1, "path": 2})
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_remove_with_blank_arguments_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json={"repo": "  ", "path": ""})
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_an_app_caller_is_denied_on_both_endpoints(self):
        # The allow-list is built from EVERY slot's project, so an app caller
        # reaching here could read or delete inside another session's repository.
        async with TestClient(TestServer(_app(app_claim="some-app"))) as client:
            assert (await client.get("/api/worktree/list?repo=/tmp")).status == 403
            assert (
                await client.post("/api/worktree/remove", json={"repo": "/tmp", "path": "/tmp/x"})
            ).status == 403

    @pytest.mark.asyncio
    async def test_a_repo_outside_every_slot_project_is_refused(self, tmp_path):
        outsider = tmp_path / "not-a-slot-project"
        outsider.mkdir()
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get(f"/api/worktree/list?repo={outsider}")

        assert resp.status == 403


class TestForceMustBeARealBoolean:
    """`bool("false")` is True, and that would hand `--force` to git.

    The picker only ever sends a JSON boolean, but the endpoint is reachable by
    anything holding a dashboard session, and the failure mode here is discarding
    a user's uncommitted work -- so the type is enforced rather than coerced.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, "yes", [], {}])
    async def test_a_non_boolean_force_is_refused_before_any_git_work(self, bad):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove",
                json={"repo": "/tmp/x", "path": "/tmp/x/wt", "force": bad},
            )
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_an_absent_force_is_accepted_as_not_forced(self, tmp_path):
        # Reaching the allow-list refusal (403) proves argument screening passed;
        # a 400 here would mean the default was rejected as a bad type.
        outsider = tmp_path / "elsewhere"
        outsider.mkdir()
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove", json={"repo": str(outsider), "path": str(outsider / "wt")}
            )

        assert resp.status == 403


class TestActiveSessionIsRecheckedBeforeRemoval:
    def test_a_session_that_adopts_the_worktree_during_the_dirty_probe_is_honoured(
        self, monkeypatch, tmp_path
    ):
        """The dirty probe can take seconds; the map is re-read after it.

        Without the late re-check, a session that retargeted onto this worktree
        while `git status` was running would be left pointing at a deleted
        directory. Simulated by having the probe itself adopt the worktree.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        state = MagicMock()
        state._slots = {}

        stub = _GitStub(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            )
        )
        monkeypatch.setattr(wt, "_run_git", stub)
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "")

        def adopt_then_report_clean(worktree_path):
            state._slots = {"chat-late": MagicMock(project=str(linked))}
            return False

        monkeypatch.setattr(wt, "_worktree_dirty", adopt_then_report_clean)

        payload, status = _remove_worktree_sync(str(main), str(linked), False, state)

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-late"
        assert stub.argv_for("worktree remove") is None
        assert linked.exists()


class TestSensitiveWorktreePathsAreNotReachable:
    """git having a sensitive path registered is not authority to touch it.

    `_resolve_repo_root` screens the REPO, but per-worktree paths come from git's
    own registration and were never screened -- so a worktree registered under a
    sensitive directory would be status-probed on the list side (a read) and
    deletable on the remove side (a delete).
    """

    def test_a_sensitive_worktree_is_omitted_from_the_list(self, git, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}))

        assert [r["path"] for r in rows] == [str(main)]
        # And it was never probed, which would itself have been a read of that tree.
        assert all(call[1] != str(secret) for call in stub.calls)

    def test_the_omission_is_audited_not_silent(self, git, tmp_path, monkeypatch):
        """Omitting a sensitive worktree is a denial, so it must reach the SEL.

        The endpoint logs its own `allowed` outcome for the list call; without a
        `denied` event here the refusal to hand back a path would leave no trace
        at all, and the audit trail would read as a clean listing.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}), "owner")

        assert [r["path"] for r in rows] == [str(main)]
        denials = [e for e in logged if e.get("outcome") == "denied"]
        assert len(denials) == 1, logged
        assert denials[0]["caller"] == "owner"
        assert denials[0]["operation"] == "worktree_list"
        assert denials[0]["error"] == "worktree_path_sensitive"
        # The offending path is named, so the trail says WHICH tree was withheld.
        assert os.path.realpath(str(secret)) in denials[0]["resources"]

    def test_a_registered_worktree_whose_directory_is_gone_does_not_500(self, git, tmp_path):
        """git keeps the registration after the directory goes; listing must cope.

        Both dirty probes run with the worktree as cwd, which RAISES (rather than
        returning non-zero) when it no longer exists — so one stale entry would
        take down the whole listing instead of rendering as "unknown".
        """
        main, gone = tmp_path / "repo", tmp_path / "repo-wt-vanished"
        main.mkdir()  # `gone` is deliberately never created
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(gone), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}))

        assert [r["path"] for r in rows] == [str(main), str(gone)]
        # The vanished tree reports an unknown dirty state, not a crash and not a
        # false "clean" — remove still demands an explicit force for it.
        assert rows[1]["dirty"] is None

    def test_a_clean_listing_logs_no_denial(self, git, tmp_path, monkeypatch):
        """The other half: an ordinary listing must not emit denial noise."""
        from kiro_crew.dashboard.handlers import worktree as wt

        main = tmp_path / "repo"
        main.mkdir()
        git(
            worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: False)

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)

        _list_worktrees_sync(str(main), MagicMock(_slots={}), "owner")

        assert [e for e in logged if e.get("outcome") == "denied"] == []

    def test_removing_a_sensitive_worktree_is_refused_403(self, git, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        payload, status = _remove_worktree_sync(str(main), str(secret), True, MagicMock(_slots={}))

        assert (status, payload["code"]) == (403, "worktree_path_sensitive")
        # Force must not get it past the gate either.
        assert stub.argv_for("worktree remove") is None
        assert secret.exists()


class TestEveryGitFailureExitIsAudited:
    """A ratchet over the CLASS, not one more instance of it.

    Three review rounds each closed one unaudited exit in this module and were
    handed the next one. The invariant that makes the rest unreachable is
    structural rather than per-endpoint -- every handler that catches a git/OS
    failure must record to the SEL before returning -- so it is asserted over the
    parsed source, which is the only form that also catches a handler added later.

    Parsed with `ast` rather than matched as text: a substring scan for `return`
    reports a false positive when the word appears in a comment first, and misses
    the case where the recording happens inside a helper the handler delegates to.
    Both mistakes were made writing this test.
    """

    @staticmethod
    def _module_ast():
        import ast
        import inspect

        from kiro_crew.dashboard.handlers import worktree as wt

        return ast, ast.parse(inspect.getsource(wt))

    @classmethod
    def _responding_handlers(cls):
        """Every `except` handler in this module that returns an HTTP response.

        Deliberately NOT scoped to one exception clause. The first version of this
        ratchet checked only `except (OSError, subprocess.SubprocessError)`, so the
        next review round handed over the sibling `except SandboxUnavailable` exit
        one clause away -- the same defect, missed because the assertion had been
        written around the instance it was chasing. What matters is that no failure
        leaves this module unrecorded, whatever its exception type.
        """
        ast, tree = cls._module_ast()
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Only handlers that answer the CLIENT, and only after work began:
            #
            #  - the blocking halves (`_create_worktree_sync` and friends) return
            #    `(payload, status)` tuples, and their outcome is recorded by the
            #    async handler that renders them -- flagging those would demand a
            #    second, duplicate record.
            #  - a malformed-JSON rejection fires before any repo is resolved or
            #    any git spawned, so there is no resource or policy decision to
            #    record; auditing it would log traffic, not decisions.
            #
            # Both exclusions are stated rather than silent, because an earlier
            # revision of this ratchet fired on all of them and the noise is what
            # makes a ratchet get deleted instead of fixed.
            answers_client = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "json_response"
                for inner in ast.walk(node)
            ) or any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id.startswith("_audited_")
                for inner in ast.walk(node)
            )
            if not answers_client:
                continue
            src = ast.unparse(node)
            if "invalid JSON" in src:
                continue
            found.append((ast, node))
        return found

    @staticmethod
    def _records(ast, handler) -> bool:
        """True when this handler records, directly or via an `_audited_*` helper."""
        for inner in ast.walk(handler):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            # sel().log_api_access(...)
            if isinstance(fn, ast.Attribute) and fn.attr == "log_api_access":
                return True
            # return _audited_<something>(...) -- the helper does the recording
            if isinstance(fn, ast.Name) and fn.id.startswith("_audited_"):
                return True
        return False

    def test_no_failure_handler_returns_without_recording(self):
        """EVERY responding `except` handler records, whatever it caught."""
        handlers = self._responding_handlers()
        assert (
            len(handlers) >= 5
        ), f"expected several client-answering handlers here, found {len(handlers)}"

        unaudited = [handler.lineno for ast, handler in handlers if not self._records(ast, handler)]
        assert not unaudited, (
            "failure handler(s) at source line(s) "
            f"{unaudited} return a response without a SEL record. Every failure exit "
            "in this module is audited -- record directly, or return through "
            "`_audited_list_failure`. Add the record rather than an exemption: an "
            "unrecorded refusal or failure is invisible to the audit queries the "
            "recorded ones exist for."
        )

    def test_the_module_uses_one_outcome_vocabulary(self):
        """`allowed` / `denied` / `error` and nothing else.

        Audit queries filter on `outcome`, so a fourth spelling silently hides
        whatever it labels -- which is how `remove` came to report policy refusals
        as `error` in the first place.
        """
        import inspect
        import re

        from kiro_crew.dashboard.handlers import worktree as wt

        outcomes = set(re.findall(r'outcome="([a-z_]+)"', inspect.getsource(wt)))
        assert outcomes <= {"allowed", "denied", "error"}, (
            f"unexpected SEL outcome vocabulary: {sorted(outcomes)}. Audit queries "
            "filter on this field; a new spelling hides the events it labels."
        )


class TestListFailuresAreAudited:
    """Every `worktree_list` exit leaves a record, not just the success one.

    The success path logs `allowed` and the per-worktree sensitive-path filter
    logs `denied`, so an unaudited failure exit made the trail selectively
    readable: which listings succeeded and which paths were withheld were both
    visible, while a repository whose listing kept failing -- or a host whose
    sandbox had gone away, which is how every git probe here stops working --
    left nothing behind.
    """

    @staticmethod
    def _recorder(monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)
        return logged

    @staticmethod
    def _stub_resolution(monkeypatch, repo):
        from kiro_crew.dashboard.handlers import worktree as wt

        async def _resolved(*a, **k):
            return repo

        monkeypatch.setattr(wt, "_resolve_repo_root", _resolved)

    @staticmethod
    async def _get(repo):
        """Returns `(status, body)` -- both read BEFORE the client closes.

        Returning the response object instead would hand back a reader whose
        connection the `async with` has already torn down, so `.json()` raises
        `ClientConnectionError` rather than failing the assertion it was meant
        to make.
        """
        async with TestClient(TestServer(_app(repo))) as client:
            resp = await client.get(f"/api/worktree/list?repo={repo}")
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_git_listing_failure_is_recorded_as_an_error(self, tmp_path, monkeypatch):
        """`None` from the sync half means git could not be asked -> 503 + record."""
        from kiro_crew.dashboard.handlers import worktree as wt

        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_resolution(monkeypatch, str(repo))
        monkeypatch.setattr(wt, "_list_worktrees_sync", lambda *a, **k: None)

        status, body = await self._get(str(repo))

        assert status == 503
        assert body["code"] == "worktree_list_unavailable"
        lists = [e for e in logged if e.get("operation") == "worktree_list"]
        assert len(lists) == 1, logged
        assert lists[0]["outcome"] == "error"
        assert lists[0]["error"] == "worktree_list_unavailable"

    @pytest.mark.asyncio
    async def test_a_missing_sandbox_is_recorded_as_an_error(self, tmp_path, monkeypatch):
        """The one failure that takes out every git probe in the module."""
        from kiro_crew.dashboard.handlers import worktree as wt

        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_resolution(monkeypatch, str(repo))

        def _boom(*a, **k):
            raise wt.SandboxUnavailable("no backend")

        monkeypatch.setattr(wt, "_list_worktrees_sync", _boom)

        status, body = await self._get(str(repo))

        assert status == 503
        assert body["code"] == "sandbox_unavailable"
        lists = [e for e in logged if e.get("operation") == "worktree_list"]
        assert [e["outcome"] for e in lists] == ["error"]
        assert lists[0]["error"] == "sandbox_unavailable"

    @pytest.mark.asyncio
    async def test_a_subprocess_failure_is_recorded_as_an_error(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_resolution(monkeypatch, str(repo))

        def _boom(*a, **k):
            raise OSError("git vanished")

        monkeypatch.setattr(wt, "_list_worktrees_sync", _boom)

        status, body = await self._get(str(repo))

        assert status == 500
        assert body["code"] == "worktree_list_failed"
        lists = [e for e in logged if e.get("operation") == "worktree_list"]
        assert [e["outcome"] for e in lists] == ["error"]

    @pytest.mark.asyncio
    async def test_a_successful_listing_still_records_allowed_exactly_once(
        self, tmp_path, monkeypatch
    ):
        """The other half: the fix must not double-log or relabel the success path."""
        from kiro_crew.dashboard.handlers import worktree as wt

        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_resolution(monkeypatch, str(repo))
        monkeypatch.setattr(wt, "_list_worktrees_sync", lambda *a, **k: [])

        status, _body = await self._get(str(repo))

        assert status == 200
        lists = [e for e in logged if e.get("operation") == "worktree_list"]
        assert [e["outcome"] for e in lists] == ["allowed"]


class TestRemoveAuditsRefusalsAsDenied:
    """A policy refusal is logged `denied`, not `error`.

    403 (sensitive path) and 409 (main / in use / dirty) are this endpoint
    refusing a delete on policy grounds -- the events a reviewer greps for.
    Collapsing them into `error` alongside failed git calls and timeouts hides
    every one of them from a SEL query filtering `outcome="denied"`, which is the
    query the list path's own denial is recorded for. These drive the real
    handler and read the outcome off the recorder, so the mapping is asserted
    where it actually runs rather than restated in the test.
    """

    @staticmethod
    def _recorder(monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)
        return logged

    @staticmethod
    def _stub_remove(monkeypatch, payload, status, repo):
        """Pin the sync half's verdict so the test isolates the audit mapping.

        Repo resolution is stubbed too: it shells out to git for the toplevel, so
        against a plain tmp dir it would 400 before the handler ever reaches the
        audit call these tests are about. Its own refusals have their own tests.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        async def _resolved(*a, **k):
            return repo

        monkeypatch.setattr(wt, "_resolve_repo_root", _resolved)
        monkeypatch.setattr(wt, "_remove_worktree_sync", lambda *a, **k: (payload, status))

    @staticmethod
    async def _post(repo):
        """Returns the status, read before the client context closes."""
        async with TestClient(TestServer(_app(repo))) as client:
            resp = await client.post(
                "/api/worktree/remove", json={"repo": repo, "path": f"{repo}-wt-x"}
            )
            return resp.status

    @pytest.mark.parametrize(
        "status,payload,expected",
        [
            (200, {"ok": True, "path": "/repo-wt-x", "branch": "feat/x"}, "allowed"),
            (403, {"error": "Access denied", "code": "worktree_path_sensitive"}, "denied"),
            (409, {"error": "in use", "code": "worktree_in_use"}, "denied"),
            (400, {"error": "git failed", "code": "worktree_remove_failed"}, "error"),
            (503, {"error": "no sandbox", "code": "sandbox_unavailable"}, "error"),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusals_are_denied_and_failures_are_errors(
        self, tmp_path, monkeypatch, status, payload, expected
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_remove(monkeypatch, payload, status, str(repo))

        got_status = await self._post(str(repo))

        assert got_status == status
        removes = [e for e in logged if e.get("operation") == "worktree_remove"]
        assert len(removes) == 1, logged
        assert removes[0]["outcome"] == expected

    @pytest.mark.asyncio
    async def test_a_denial_is_findable_by_the_same_query_as_the_list_path(
        self, tmp_path, monkeypatch
    ):
        """The point of the fix: one vocabulary across both endpoints.

        A reviewer auditing withheld worktrees greps `outcome="denied"` once; if
        `remove` spelled its refusals `error` that grep would return the list
        path's denial and silently miss every refused delete.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        logged = self._recorder(monkeypatch)
        self._stub_remove(
            monkeypatch,
            {"error": "Access denied", "code": "worktree_path_sensitive"},
            403,
            str(repo),
        )

        await self._post(str(repo))

        denials = [e for e in logged if e.get("outcome") == "denied"]
        assert [e["operation"] for e in denials] == ["worktree_remove"]


class TestNestedSessionProjectCountsAsInUse:
    """A session scoped BENEATH a worktree is just as broken by removing it."""

    def test_a_slot_scoped_to_a_subdirectory_blocks_removal(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        (linked / "src").mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )
        state = MagicMock()
        # The session's cwd is inside the worktree, not the worktree root.
        state._slots = {"chat-nested": MagicMock(project=str(linked / "src"))}

        payload, status = _remove_worktree_sync(str(main), str(linked), False, state)

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-nested"

    def test_a_sibling_with_a_shared_prefix_does_not_count(self, tmp_path):
        # `/repo-wt-other` must not read as inside `/repo-wt`.
        target = _norm_path(os.path.realpath(str(tmp_path / "repo-wt")))
        sibling = _norm_path(os.path.realpath(str(tmp_path / "repo-wt-other")))

        assert _active_slot_beneath({sibling: "chat-1"}, target) is None
        assert _active_slot_beneath({target: "chat-1"}, target) == "chat-1"
