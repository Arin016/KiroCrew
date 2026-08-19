"""Tests for the dependency-only sync that stands in for a blocked reinstall.

The module exists because Windows cannot rewrite a running console script, so
these tests pin what that substitution rests on: it must never leave the checkout
merged-but-unstartable (every refusal happens before the merge), extra activity is
judged against the declarations the venv was installed FROM, the interpreter gate
`pip install -e .` used to provide is still applied, and the authoritative
console-script declaration is the pyproject one.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.dev_fleet import dep_sync


@pytest.fixture(autouse=True)
def _preconditions_ok(request):
    """Stub main()'s git/venv preconditions for the ``main()`` tests only.

    Each precondition has its own direct tests; patching them out here keeps the
    main() tests about what main does with the answers, and stops them reaching a
    real git or a real interpreter through the mocked subprocess. The venv-mapping
    COMPARISON is deliberately left real -- only the probe that reads the mapping is
    stubbed -- so a main() test can make the venv foreign by changing that answer.
    """
    if not request.node.name.startswith("test_main_"):
        yield
        return
    with (
        patch.object(dep_sync, "merge_not_guaranteed", return_value=None),
        patch.object(dep_sync, "dependency_authority_moved", return_value=None),
        patch.object(dep_sync, "pin_incoming", return_value="cafe1234"),
        patch.object(dep_sync, "installed_package_origin", return_value="/fake/repo/src/x.py"),
    ):
        yield


def test_pin_incoming_resolves_once_and_writes_the_pin():
    """The merge must consume a commit, not a ref a concurrent fetch can advance.

    Installing for one revision and merging another is the missing-dependency
    outcome this step exists to prevent, so the incoming ref is resolved a single
    time and pinned at a ref only this module writes.
    """
    calls = []

    with (
        patch.object(dep_sync, "_git_output", return_value="deadbeefcafe") as out,
        patch.object(dep_sync, "_git_succeeds", side_effect=lambda *a: calls.append(a) or True),
    ):
        oid = dep_sync.pin_incoming(Path("/fake/repo"), Path("/fake/git"), "origin/main")

    assert oid == "deadbeefcafe"
    # Resolved with an explicit commit peel, so an annotated tag cannot slip through.
    assert "rev-parse" in out.call_args[0]
    # Pinned to the OID, never to the ref name.
    assert calls[0][2:] == ("update-ref", dep_sync.PIN_REF, "deadbeefcafe")


def test_pin_incoming_returns_none_when_the_ref_cannot_be_resolved():
    with patch.object(dep_sync, "_git_output", return_value=None):
        assert dep_sync.pin_incoming(Path("/fake/repo"), Path("/fake/git"), "origin/main") is None


def test_pin_incoming_returns_none_when_the_pin_cannot_be_written():
    with (
        patch.object(dep_sync, "_git_output", return_value="deadbeef"),
        patch.object(dep_sync, "_git_succeeds", return_value=False),
    ):
        assert dep_sync.pin_incoming(Path("/fake/repo"), Path("/fake/git"), "origin/main") is None


def test_main_refuses_when_the_incoming_ref_cannot_be_pinned(capsys):
    """No pin means no guarantee the merge consumes what was installed for."""
    with patch.object(dep_sync, "pin_incoming", return_value=None):
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    err = capsys.readouterr().err
    assert "pin" in err
    assert "Nothing was merged" in err


_SETUP_CFG = """\
[options]
install_requires =
    aiohttp>=3.9,<4
    # A full-line comment: configparser keeps it in the value, so it has to be
    # stripped before the value reaches pip.
    tzdata>=2024.1; platform_system == "Windows"
    python-docx>=1,<2

[options.extras_require]
voice =
    openai-whisper>=1
    sounddevice>=0.4
perf =
    py-spy>=0.3

[options.entry_points]
console_scripts =
    kirocrew = kiro_crew._bootstrap:main
"""

_PYPROJECT = """\
[project]
name = "kirocrew"
requires-python = ">=3.10"
dynamic = ["dependencies", "optional-dependencies"]

[project.scripts]
kirocrew = "kiro_crew._bootstrap:main"
"""

_ARGS = ["/fake/repo", "/fake/python", "/fake/git", "oldrev", "newrev"]


def _shows(**by_path):
    """A `show` stub resolving (rev, path) -> text, defaulting to the fixtures."""

    def fake_show(repo, git_bin, rev, path):
        return by_path.get((rev, path), by_path.get(path))

    return fake_show


def test_requirement_name_normalizes_per_pep503():
    """Names must compare equal across the spellings a declaration may use."""
    assert dep_sync.requirement_name("aiohttp>=3.9,<4") == "aiohttp"
    assert dep_sync.requirement_name("python_docx>=1") == "python-docx"
    assert dep_sync.requirement_name("Some.Pkg[extra]>=1") == "some-pkg"
    assert dep_sync.requirement_name('tzdata>=2024.1; platform_system == "Windows"') == "tzdata"


def test_declared_requirements_at_drops_comment_lines():
    """A comment handed to pip is a resolution error, not a no-op."""
    with patch.object(dep_sync, "show", _shows(**{"setup.cfg": _SETUP_CFG})):
        base, extras = dep_sync.declared_requirements_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev"
        )

    assert base == [
        "aiohttp>=3.9,<4",
        'tzdata>=2024.1; platform_system == "Windows"',
        "python-docx>=1,<2",
    ]
    assert set(extras) == {"voice", "perf"}


def test_declared_requirements_at_returns_none_when_unreadable():
    """A shallow clone or rewritten history must not read as 'no extras'."""
    with patch.object(dep_sync, "show", _shows()):
        assert (
            dep_sync.declared_requirements_at(Path("/fake/repo"), Path("/fake/git"), "deadbeef")
            is None
        )


def test_an_extra_is_active_when_all_its_distributions_are_present():
    extras = {
        "voice": ["openai-whisper>=1", "sounddevice>=0.4"],
        "perf": ["py-spy>=0.3"],
    }

    assert dep_sync.active_extras(extras, {"openai-whisper", "sounddevice"}) == ["voice"]


def test_a_partially_installed_extra_is_not_active():
    """Installed metadata records no requested extras, so presence is the signal."""
    extras = {"voice": ["openai-whisper>=1", "sounddevice>=0.4"]}

    assert dep_sync.active_extras(extras, {"openai-whisper"}) == []


def test_an_extra_that_gained_a_distribution_stays_active():
    """The regression this module's design turns on.

    A revision that adds a distribution to an extra the operator uses would, if
    activity were judged against the INCOMING declarations, make that extra read
    inactive -- its new requirement is skipped, the step exits 0, and the gateway
    fails on that import at its next restart. Judging against the installed-from
    declarations keeps it active, so the new requirement is installed.
    """
    incoming = (
        ["aiohttp>=3.9,<4"],
        {"voice": ["openai-whisper>=1", "sounddevice>=0.4", "brand-new-dep>=2"]},
    )
    previous_extras = {"voice": ["openai-whisper>=1", "sounddevice>=0.4"]}

    specs, chosen, skipped = dep_sync.plan(
        incoming, previous_extras, {"openai-whisper", "sounddevice"}
    )

    assert chosen == ["voice"]
    assert "brand-new-dep>=2" in specs
    assert skipped == []


def test_plan_never_includes_the_project_itself():
    """Installing the project is the one thing that rewrites the locked script."""
    incoming = (["aiohttp>=3.9,<4"], {"perf": ["py-spy>=0.3"]})

    specs, chosen, skipped = dep_sync.plan(incoming, {"perf": ["py-spy>=0.3"]}, {"py-spy"})

    assert chosen == ["perf"]
    assert not any(dep_sync.requirement_name(s) == "kirocrew" for s in specs)
    assert skipped == []


def test_python_floor_breach_reports_the_highest_unmet_floor():
    """The floor is the bound a revision raises when it adopts newer syntax."""
    assert dep_sync.python_floor_breach(">=3.12", (3, 10)) == "3.12"
    assert dep_sync.python_floor_breach(">=3.10", (3, 12)) is None
    assert dep_sync.python_floor_breach(">=3.10,<4", (3, 10)) is None
    # Multiple lower bounds: the highest unmet one is what the operator must hear.
    assert dep_sync.python_floor_breach(">=3.11,>=3.13", (3, 12)) == "3.13"


def test_requires_python_is_read_from_the_incoming_revision():
    with patch.object(dep_sync, "show", _shows(**{"pyproject.toml": _PYPROJECT})):
        assert (
            dep_sync.requires_python_at(Path("/fake/repo"), Path("/fake/git"), "newrev") == ">=3.10"
        )


def test_console_script_target_prefers_the_pyproject_declaration():
    """`scripts` is not dynamic here, so pyproject is what setuptools builds from.

    A repoint made only in pyproject must be seen; reading setup.cfg alone would
    report success while the locked wrapper keeps its old target.
    """
    repointed = _PYPROJECT.replace("_bootstrap:main", "cli:main")
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": repointed, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew.cli:main"


def test_console_script_target_falls_back_to_setup_cfg():
    with patch.object(dep_sync, "show", _shows(**{"setup.cfg": _SETUP_CFG})):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew._bootstrap:main"


def test_merge_not_guaranteed_flags_a_dirty_checkout():
    """The invariant: refuse unless the following ff-only merge cannot fail.

    Installing and merging are two writes with no shared transaction, so whichever
    runs second can strand the first. A clean tree plus an ancestor relationship is
    the condition under which the merge has nothing to refuse.
    """

    class _Dirty:
        returncode = 0
        stdout = " M src/kiro_crew/foo.py\n"

    with patch.object(dep_sync, "subprocess") as sp:
        sp.run.return_value = _Dirty()
        why = dep_sync.merge_not_guaranteed(Path("/fake/repo"), Path("/fake/git"), "old", "new")

    assert why is not None
    assert "local modifications" in why


def test_merge_not_guaranteed_flags_a_non_fast_forward():
    class _Clean:
        returncode = 0
        stdout = "\n"

    with patch.object(dep_sync, "subprocess") as sp:
        sp.run.return_value = _Clean()
        with patch.object(dep_sync, "_git_succeeds", return_value=False):
            why = dep_sync.merge_not_guaranteed(Path("/fake/repo"), Path("/fake/git"), "old", "new")

    assert why is not None
    assert "fast-forward" in why


def test_merge_not_guaranteed_passes_a_clean_fast_forward():
    class _Clean:
        returncode = 0
        stdout = ""

    with patch.object(dep_sync, "subprocess") as sp:
        sp.run.return_value = _Clean()
        with patch.object(dep_sync, "_git_succeeds", return_value=True):
            assert (
                dep_sync.merge_not_guaranteed(Path("/fake/repo"), Path("/fake/git"), "old", "new")
                is None
            )


def test_dependency_authority_moved_detects_a_migration_to_pyproject():
    """setup.cfg is only authoritative while pyproject keeps the fields dynamic.

    A revision that moves requirements into pyproject's own [project] table would
    make setup.cfg stale; reading it anyway installs the wrong set and reports
    success.
    """
    migrated = _PYPROJECT.replace(
        'dynamic = ["dependencies", "optional-dependencies"]',
        'dependencies = ["aiohttp>=3.9"]',
    )
    with patch.object(dep_sync, "show", _shows(**{"pyproject.toml": migrated})):
        why = dep_sync.dependency_authority_moved(Path("/fake/repo"), Path("/fake/git"), "newrev")

    assert why is not None
    assert "dynamic" in why


def test_dependency_authority_intact_when_fields_stay_dynamic():
    with patch.object(dep_sync, "show", _shows(**{"pyproject.toml": _PYPROJECT})):
        assert (
            dep_sync.dependency_authority_moved(Path("/fake/repo"), Path("/fake/git"), "newrev")
            is None
        )


def test_main_refuses_when_the_installed_revision_is_unreadable(capsys):
    """Guessing which extras were installed is what drops a dependency."""
    with patch.object(dep_sync, "declared_requirements_at", return_value=None):
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    err = capsys.readouterr().err
    assert "oldrev" in err
    assert "Nothing was merged" in err


def test_main_refuses_when_the_interpreter_is_below_the_incoming_floor(capsys):
    """The gate `pip install -e .` used to provide has to be applied here.

    Without it a revision that raises requires-python installs cleanly and then
    cannot be imported by the interpreter that has to import it.
    """
    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=([], {})),
        patch.object(dep_sync, "requires_python_at", return_value=">=3.13"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 10)),
    ):
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    err = capsys.readouterr().err
    assert "3.13" in err
    assert "Nothing was merged" in err


def test_installed_package_origin_reports_where_the_package_resolves():
    """The import path is what the installed dependencies live alongside."""

    class _Proc:
        returncode = 0
        stdout = "/checkouts/main/src/kiro_crew/__init__.py\n"

    with patch.object(dep_sync.subprocess, "run", return_value=_Proc()):
        origin = dep_sync.installed_package_origin(Path("py"))

    assert origin is not None
    assert Path(origin).name == "__init__.py"


def test_installed_package_origin_is_none_when_the_package_is_absent():
    """An empty answer must not be read as a location."""

    class _Proc:
        returncode = 0
        stdout = "\n"

    with patch.object(dep_sync.subprocess, "run", return_value=_Proc()):
        assert dep_sync.installed_package_origin(Path("py")) is None


def test_venv_serving_another_checkout_is_reported(tmp_path):
    """The harm this guards: upgrading a runtime another checkout is served by."""
    repo = tmp_path / "main"
    other = tmp_path / "other"
    reason = dep_sync.venv_not_mapped_to(str(other / "src" / "kiro_crew" / "__init__.py"), repo)

    assert reason is not None
    assert "other" in reason
    assert "main" in reason


def test_an_unresolvable_package_is_not_taken_as_a_match(tmp_path):
    """Unproven is refused, not assumed.

    Reading nothing is exactly what a venv this step cannot vouch for produces, so
    treating it as a match would leave the hole open in the one configuration that
    cannot be checked.
    """
    assert dep_sync.venv_not_mapped_to(None, tmp_path / "main") is not None


def test_a_venv_serving_this_checkout_passes(tmp_path):
    """The ordinary layout -- an editable install of the checkout's own src tree."""
    repo = tmp_path / "main"
    origin = repo / "src" / "kiro_crew" / "__init__.py"

    assert dep_sync.venv_not_mapped_to(str(origin), repo) is None


def test_a_sibling_directory_sharing_the_prefix_does_not_count_as_inside(tmp_path):
    """`<repo>-wt` starts with `<repo>` as a string but is a different checkout."""
    repo = tmp_path / "main"
    sibling = tmp_path / "main-wt" / "src" / "kiro_crew" / "__init__.py"

    assert dep_sync.venv_not_mapped_to(str(sibling), repo) is not None


def test_main_refuses_a_venv_serving_another_checkout(capsys):
    """Refused before the pin ref is written and before pip runs."""

    class _Ok:
        returncode = 0

    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value="/checkouts/other/src/kiro_crew/__init__.py",
        ),
        patch.object(dep_sync, "pin_incoming") as pin,
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    assert not pin.called
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "other" in err
    assert "Nothing was merged" in err


def test_main_hands_every_spec_to_pip_and_stops_on_failure():
    """pip decides satisfaction; a failed install must not report success."""
    calls = []

    class _Proc:
        returncode = 1

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["aiohttp>=3.9,<4"], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.side_effect = fake_run
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    assert calls[0][1:4] == ["-m", "pip", "install"]
    assert "aiohttp>=3.9,<4" in calls[0]


def test_main_ends_pip_option_parsing_before_the_specs():
    """A declared requirement must never be readable as a pip option.

    The `--` separator is belt to the braces of `rejected_specs`: the rejection list
    is what stops a hostile declaration reaching pip at all, and the separator keeps
    a merely odd one from being parsed as a flag if that list is ever loosened.
    """
    calls = []

    class _Ok:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Ok()

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["aiohttp>=3.9"], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "console_script_target_at", return_value=None),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.side_effect = fake_run
        dep_sync.main(list(_ARGS))

    cmd = calls[0]
    assert cmd[1:5] == ["-m", "pip", "install", "--"]
    assert cmd.index("--") < cmd.index("aiohttp>=3.9")


def test_rejected_specs_refuses_paths_and_the_project_itself():
    """The module's premise -- pip is asked for dependencies, never the project.

    A declaration of `.` would have pip install the checkout, remove the editable
    install, and then fail on the locked executable, leaving the venv unable to
    import the package at all. That used to hold only because this repository
    happens not to declare it; now it is checked.
    """
    assert dep_sync.rejected_specs(["aiohttp>=3.9,<4", 'tzdata; sys_platform == "win32"']) == []

    for hostile in [".", "./local", "/abs/path", r"C:\pkgs\x", "file:./x", "x.whl", "-e"]:
        assert dep_sync.rejected_specs([hostile]), hostile

    # Only spellings PEP 503 actually folds onto this project's name. `kiro_crew`
    # normalizes to `kiro-crew`, which is a DIFFERENT distribution, so it is not
    # claimed here.
    for spelling in ["kirocrew", "KiroCrew", "KIROCREW", "kirocrew>=1"]:
        rejected = dep_sync.rejected_specs([spelling])
        assert rejected and "names this project" in rejected[0], spelling


def test_main_refuses_a_declaration_that_names_the_project(capsys):
    """Refused BEFORE pip runs, so the venv is never touched."""

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["."], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "will not hand to pip" in err
    assert "Nothing was merged" in err


def test_main_refuses_a_repointed_console_script_before_merging(capsys):
    """The one gap: a moved entry point cannot be refreshed while it is locked."""

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=([], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "console_script_target_at", return_value="kiro_crew.new:main"),
        patch.object(
            dep_sync, "installed_console_script_target", return_value="kiro_crew.old:main"
        ),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    err = capsys.readouterr().err
    assert "kiro_crew.new:main" in err
    assert "kiro_crew.old:main" in err
    assert "Nothing was merged" in err


def test_main_reports_which_extras_were_left_alone(capsys):
    """The docstring promises the skipped extras are reported, so pin it."""

    class _Ok:
        returncode = 0

    previous = (["aiohttp>=3.9,<4"], {"voice": ["openai-whisper>=1"]})
    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=previous),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(
            dep_sync, "console_script_target_at", return_value="kiro_crew._bootstrap:main"
        ),
        patch.object(
            dep_sync, "installed_console_script_target", return_value="kiro_crew._bootstrap:main"
        ),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 0
    assert "extras left alone" in capsys.readouterr().out


def test_main_tolerates_an_unreadable_installed_entry_point():
    """Missing evidence must not fail a sync that otherwise succeeded."""

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=([], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(
            dep_sync, "console_script_target_at", return_value="kiro_crew._bootstrap:main"
        ),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 0


def test_main_rejects_a_wrong_argument_count():
    assert dep_sync.main([]) == 2
    assert dep_sync.main(["a", "b", "c", "d"]) == 2
