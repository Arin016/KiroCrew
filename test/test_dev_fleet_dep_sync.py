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


def test_python_floor_breach_reads_a_compatible_release_as_a_floor():
    """``~=3.12`` means ``>=3.12,<4``, so its floor half must be enforced.

    Read as declaring no floor at all, a revision that switches its
    requires-python to the compatible-release spelling and adopts 3.12-only
    syntax would merge cleanly under a 3.10 interpreter and fail at import.
    """
    assert dep_sync.python_floor_breach("~=3.12", (3, 10)) == "3.12"
    assert dep_sync.python_floor_breach("~=3.10", (3, 12)) is None
    assert dep_sync.python_floor_breach("~=3.12", (3, 12)) is None


def test_python_floor_breach_compares_the_micro_component():
    """``>=3.10.5`` refuses a 3.10.0 interpreter; truncating to the minor would not.

    The interpreter probe reports three components for the same reason: at
    exactly the boundary being checked, 3.12.13 and 3.12.14 are different
    answers, whichever spelling declared the floor.
    """
    assert dep_sync.python_floor_breach(">=3.10.5", (3, 10, 0)) == "3.10.5"
    assert dep_sync.python_floor_breach(">=3.10.5", (3, 10, 6)) is None
    assert dep_sync.python_floor_breach("~=3.12.14", (3, 12, 13)) == "3.12.14"
    assert dep_sync.python_floor_breach("~=3.12.14", (3, 12, 14)) is None
    # A two-component interpreter answer reads as micro 0: the conservative side.
    assert dep_sync.python_floor_breach(">=3.10.5", (3, 10)) == "3.10.5"


def test_python_floor_breach_excludes_the_named_version_under_a_bare_gt():
    """``>3.10`` excludes 3.10.0 itself; reading it as ``>=`` waves it through."""
    assert dep_sync.python_floor_breach(">3.10", (3, 10)) == "3.10"
    assert dep_sync.python_floor_breach(">3.10", (3, 10, 5)) is None
    assert dep_sync.python_floor_breach(">3.10", (3, 11)) is None


def test_python_floor_breach_reads_an_equality_pin_as_a_floor():
    """``==X.Y.*`` and ``===X.Y.Z`` declare their named version as the floor.

    Left unrecognized they read as no floor at all -- the same silent merge of an
    unimportable revision the ``~=`` fix closes. Only the floor side is enforced
    here; a higher interpreter is left to pip like every other upper bound.
    """
    assert dep_sync.python_floor_breach("==3.12.*", (3, 11)) == "3.12"
    assert dep_sync.python_floor_breach("==3.12.*", (3, 12)) is None
    assert dep_sync.python_floor_breach("==3.12.*", (3, 13)) is None
    assert dep_sync.python_floor_breach("===3.12.4", (3, 12, 3)) == "3.12.4"
    assert dep_sync.python_floor_breach("===3.12.4", (3, 12, 4)) is None


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


def test_console_script_removed_from_pyproject_is_not_read_from_setup_cfg():
    """A present-but-omitting ``[project.scripts]`` table means REMOVED.

    setuptools builds the wrapper from the pyproject table when it exists, so a
    revision whose table drops the script has removed the entry point. setup.cfg's
    copy is stale in exactly that case, and reading it would report an agreement
    that no longer holds -- so the answer is the removal sentinel, distinct from
    the unreadable-declarations ``None``.
    """
    removed = _PYPROJECT.replace('kirocrew = "kiro_crew._bootstrap:main"', "")
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": removed, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == dep_sync.SCRIPT_REMOVED


def test_console_script_renamed_in_pyproject_reads_as_removed():
    """A table that lists OTHER scripts but not this one is the rename spelling.

    The stale setup.cfg copy must not be consulted just because the table's
    remaining entries do not include the script being asked about.
    """
    renamed = _PYPROJECT.replace(
        'kirocrew = "kiro_crew._bootstrap:main"',
        'kirocrew-next = "kiro_crew._bootstrap:main"',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": renamed, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == dep_sync.SCRIPT_REMOVED


def test_console_script_absent_with_a_static_project_table_reads_as_removed():
    """``[project]`` present, ``scripts`` neither named nor dynamic: REMOVED.

    setuptools treats a statically-absent field as authoritative and builds no
    wrapper from setup.cfg's copy (verified against setuptools 80.9), so reading
    setup.cfg here would report an agreement the incoming revision no longer
    makes -- the merge would leave the locked wrapper dispatching to a target
    the revision may have deleted.
    """
    tableless = _PYPROJECT.split("[project.scripts]")[0]
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": tableless, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == dep_sync.SCRIPT_REMOVED


def test_console_script_falls_back_to_setup_cfg_when_scripts_is_dynamic():
    """``dynamic = [..., "scripts"]`` is the one PEP 621 shape that reads setup.cfg."""
    dynamic = _PYPROJECT.split("[project.scripts]")[0].replace(
        'dynamic = ["dependencies", "optional-dependencies"]',
        'dynamic = ["dependencies", "optional-dependencies", "scripts"]',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": dynamic, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew._bootstrap:main"


def test_console_script_falls_back_to_setup_cfg_without_a_project_table():
    """A pyproject with no ``[project]`` table (pre-PEP 621) leaves setup.cfg governing."""
    build_only = '[build-system]\nrequires = ["setuptools"]\n'
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": build_only, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew._bootstrap:main"


def test_console_script_header_with_a_trailing_comment_is_still_the_declaration():
    """``[project.scripts]  # note`` is the same table, not an absent one.

    TOML allows a comment after the header; reading that spelling as absence
    would fall back to setup.cfg's stale copy exactly when pyproject repointed
    the script.
    """
    commented = _PYPROJECT.replace("[project.scripts]", "[ project.scripts ]  # wrapper")
    repointed = commented.replace("_bootstrap:main", "cli:main")
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": repointed, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew.cli:main"


def test_console_script_declared_as_an_inline_table_is_read_not_setup_cfg():
    """``scripts = { ... }`` inside ``[project]`` is the authoritative table.

    A revision that spells the declaration inline and repoints the script must
    be seen as a repoint; the pre-fix fallback read setup.cfg's stale copy,
    which agreed with the installed wrapper and let the merge proceed.
    """
    inline = _PYPROJECT.split("[project.scripts]")[0].replace(
        'name = "kirocrew"',
        'name = "kirocrew"\nscripts = { kirocrew = "kiro_crew.cli:main" }',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": inline, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew.cli:main"


def test_console_script_omitted_from_an_inline_table_reads_as_removed():
    """An inline table that names other scripts has removed this one."""
    inline = _PYPROJECT.split("[project.scripts]")[0].replace(
        'name = "kirocrew"',
        'name = "kirocrew"\nscripts = { kirocrew-next = "kiro_crew._bootstrap:main" }',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": inline, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == dep_sync.SCRIPT_REMOVED


def test_console_script_declared_with_dotted_keys_is_read_not_setup_cfg():
    """``scripts.kirocrew = ...`` inside ``[project]`` is the same declaration."""
    dotted = _PYPROJECT.split("[project.scripts]")[0].replace(
        'name = "kirocrew"',
        'name = "kirocrew"\nscripts.kirocrew = "kiro_crew.cli:main"',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": dotted, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == "kiro_crew.cli:main"


def test_console_script_absent_from_dotted_keys_reads_as_removed():
    """Dotted keys that declare only other scripts are a removal of this one."""
    dotted = _PYPROJECT.split("[project.scripts]")[0].replace(
        'name = "kirocrew"',
        'name = "kirocrew"\nscripts."kirocrew-next" = "kiro_crew._bootstrap:main"',
    )
    with patch.object(
        dep_sync,
        "show",
        _shows(**{"pyproject.toml": dotted, "setup.cfg": _SETUP_CFG}),
    ):
        got = dep_sync.console_script_target_at(
            Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
        )

    assert got == dep_sync.SCRIPT_REMOVED


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
        patch.object(dep_sync, "console_script_target_at", return_value=None),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
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
        patch.object(dep_sync, "console_script_target_at", return_value=None),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
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
    """The one gap: a moved entry point cannot be refreshed while it is locked.

    The refusal must also precede the install: the comparison needs nothing from
    pip, so a mismatch leaves the venv untouched -- requirements are declared here
    precisely to prove pip was never asked to install them.
    """

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["aiohttp>=3.9"], {})),
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
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "kiro_crew.new:main" in err
    assert "kiro_crew.old:main" in err
    assert "Nothing was merged" in err


def test_main_refuses_a_removed_console_script_before_any_write(capsys):
    """A removal is the same disagreement as a repoint and must refuse end to end.

    The removal sentinel travels the same comparison as a repointed target; were
    it collapsed into the unreadable-``None``, the sync would exit 0 and the merge
    would leave the locked wrapper dispatching to a target the revision may have
    deleted. Requirements are declared here to prove pip was never asked either.
    """

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["aiohttp>=3.9"], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "console_script_target_at", return_value=dep_sync.SCRIPT_REMOVED),
        patch.object(
            dep_sync, "installed_console_script_target", return_value="kiro_crew.old:main"
        ),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "removed" in err
    assert "kiro_crew.old:main" in err
    assert "Nothing was merged" in err


def test_main_refuses_a_removal_even_when_the_installed_target_is_unreadable(capsys):
    """A removal is a fact about the incoming revision alone.

    Whatever the locked wrapper dispatches to, the revision that removed the
    entry point no longer promises that target exists -- so an unreadable
    installed target must not launder the removal into a merge.
    """

    class _Ok:
        returncode = 0

    with (
        patch.object(dep_sync, "declared_requirements_at", return_value=(["aiohttp>=3.9"], {})),
        patch.object(dep_sync, "requires_python_at", return_value=None),
        patch.object(dep_sync, "installed_names", return_value=set()),
        patch.object(dep_sync, "console_script_target_at", return_value=dep_sync.SCRIPT_REMOVED),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value = _Ok()
        rc = dep_sync.main(list(_ARGS))

    assert rc == 1
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "removed" in err
    assert "Nothing was merged" in err


def test_console_script_multiline_string_contents_are_not_declarations():
    """Text inside a TOML multiline string must not read as the scripts table.

    A ``description`` string carrying the OLD header and entry would otherwise
    mask the real repointed declaration below it. Pinned on both read paths:
    the TOML parser (when available) and the line-scanner fallback, which
    blanks multiline strings before scanning.
    """
    decoy = (
        "[project]\n"
        'name = "kirocrew"\n'
        'description = """\n'
        "[project.scripts]\n"
        'kirocrew = "kiro_crew._bootstrap:main"\n'
        '"""\n'
        'dynamic = ["dependencies", "optional-dependencies"]\n'
        "\n"
        "[project.scripts]\n"
        'kirocrew = "kiro_crew.cli:main"\n'
    )
    for parser in (dep_sync._toml, None):
        with (
            patch.object(dep_sync, "_toml", parser),
            patch.object(
                dep_sync,
                "show",
                _shows(**{"pyproject.toml": decoy, "setup.cfg": _SETUP_CFG}),
            ),
        ):
            got = dep_sync.console_script_target_at(
                Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
            )

        assert got == "kiro_crew.cli:main"


def test_console_script_commented_dynamic_entry_does_not_count_as_declared():
    """A commented ``"scripts"`` inside the dynamic array is not a declaration.

    TOML allows comments between multi-line array elements; on the scanner
    fallback the comment text must not send the read to setup.cfg's stale copy
    when the field is statically absent. Pinned on both read paths.
    """
    commented = (
        "[project]\n"
        'name = "kirocrew"\n'
        "dynamic = [\n"
        '    "dependencies",\n'
        '    "optional-dependencies",\n'
        '    # "scripts",\n'
        "]\n"
    )
    for parser in (dep_sync._toml, None):
        with (
            patch.object(dep_sync, "_toml", parser),
            patch.object(
                dep_sync,
                "show",
                _shows(**{"pyproject.toml": commented, "setup.cfg": _SETUP_CFG}),
            ),
        ):
            got = dep_sync.console_script_target_at(
                Path("/fake/repo"), Path("/fake/git"), "newrev", "kirocrew"
            )

        assert got == dep_sync.SCRIPT_REMOVED


def test_dependency_authority_commented_dynamic_entry_reads_as_moved():
    """The same comment blindness in the other direction: a move must be seen.

    A commented ``"optional-dependencies"`` means the field is NOT dynamic any
    more; comment text keeping it looking dynamic would install a stale set.
    """
    commented = (
        "[project]\n"
        'name = "kirocrew"\n'
        "dynamic = [\n"
        '    "dependencies",\n'
        '    # "optional-dependencies",\n'
        "]\n"
    )
    with patch.object(dep_sync, "show", _shows(**{"pyproject.toml": commented})):
        moved = dep_sync.dependency_authority_moved(
            Path("/fake/repo"), Path("/fake/git"), "newrev"
        )

    assert moved is not None
    assert "optional-dependencies" in moved


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
