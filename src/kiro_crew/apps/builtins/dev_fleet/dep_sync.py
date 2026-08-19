"""Dependency-only sync for a checkout whose console script cannot be replaced.

Windows holds a mandatory lock on a running executable's image, so pip cannot
rewrite ``Scripts\\kirocrew.exe`` while the gateway is served by the very venv it
is reinstalling into -- the ordinary single-checkout layout. ``pip install -e .``
fails there even when the revision being synced changed nothing about the console
script, because a reinstall rewrites it unconditionally.

An editable install needs no reinstall for a source change: ``src`` is already on
``sys.path``, so merged code is live the moment the merge lands. What a source
change CAN require is a dependency the venv does not have yet, and installing a
dependency never touches the project's own console script. Syncing dependencies
alone is therefore the whole of what pip is needed for.

This step runs BEFORE the merge, and reads every declaration out of the fetched
revision with ``git show`` rather than from the working tree. Ordering alone is not
enough, though: installing and merging are two writes that cannot be made one
transaction, so whichever runs second can fail and leave the other applied --
merge-then-install can strand a merged revision with missing dependencies, and
install-then-merge can strand an upgraded venv on an unmerged checkout, since pip
replaces existing packages as well as adding new ones. The step therefore refuses
unless the merge is *guaranteed* to succeed first (clean tree, incoming revision a
descendant of the installed one), and only then installs. Every refusal happens
before either write, so the checkout is always left on a revision whose
dependencies are satisfied.

The venv is identified by location -- ``<repo>/.venv`` -- which is not a binding, so
the step also requires that venv to import this project from inside THIS checkout
before it writes to it. Otherwise a sync of one checkout could upgrade the runtime
another checkout is served by.

Satisfaction is left to pip rather than computed here: every declared requirement
is handed to ``pip install`` verbatim, which no-ops the ones already satisfied and
evaluates specifiers and environment markers with the same code
``pip install -e .`` would have run. Deciding it locally would need a PEP 508
parser this package does not depend on (``packaging`` is not an install
requirement) and would drift from pip's own answer.

Extras carry no record of having been requested -- installed metadata lists every
extra's requirements behind a marker whether or not the operator asked for the
extra -- so activity is inferred from what is installed, judged against the
declarations the venv was installed FROM. Judged against the incoming ones
instead, a revision that adds a distribution to an extra the operator uses makes
that extra read inactive, its new requirement is skipped, and the gateway fails
on that import at its next restart. The residual imprecision runs the safe way:
an extra whose distributions are present transitively reads active, which installs
something unnecessary but never leaves something missing.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# A real TOML parser is preferred for reading pyproject declarations: three
# review rounds produced escalating spellings the line scanner misread (inline
# tables, header comments, multiline-string contents), which is the sign the
# scanner is the wrong tool for the primary path. `tomllib` is stdlib only from
# 3.11 (PEP 680) and this project supports 3.10, so the import is guarded --
# same ladder as `onboarding_import.py` -- and the hardened line scanner stays
# as the 3.10-without-tomli fallback.
try:
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

#: Characters that terminate the distribution name at the head of a PEP 508
#: requirement (version specifier, extras bracket, marker separator, or the
#: whitespace some declarations put before the specifier).
_NAME_END = re.compile(r"[\s<>=!~;\[(]")

#: The project's only console script. It is the wrapper the operator restarts
#: through, so it is the one whose staleness has to be reported rather than
#: silently tolerated.
_SCRIPT = "kirocrew"

#: This project's own distribution name, normalized. Asking pip for it is the one
#: request that would rewrite the locked console script.
_PROJECT = "kirocrew"

#: ``requires-python`` lower bounds. Only the floor is enforced: it is the bound
#: a revision raises when it starts using newer syntax, and the one whose breach
#: makes the merged tree unimportable under this interpreter. A compatible
#: release ``~=X.Y`` declares the same floor as ``>=X.Y`` (its upper bound is
#: someone else's problem, like every other upper bound here), and the equality
#: pins ``==X.Y.*`` and ``===X.Y.Z`` declare their named version as the floor
#: the same way -- their upper side is an upper bound like any other. A third
#: component tightens the floor to the micro release, and a bare ``>`` excludes
#: the named version itself. Upper bounds and exclusions are left to pip on the
#: next real reinstall rather than reimplemented here without a PEP 440 parser.
_PY_FLOOR = re.compile(r"(===|==|>=?|~=)\s*(\d+)\.(\d+)(?:\.(\d+))?")


#: A plain PEP 508 distribution name -- letters, digits, and the separators PEP
#: 503 normalizes. Anything else at the head of a requirement is not a name: a
#: path (``.``, ``./x``, ``/abs``, ``C:\x``), a URL scheme, an archive filename, or
#: a stray option. Those are what could make pip install the project itself.
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Distribution-archive suffixes. pip reads a bare token ending in one of these as
#: a local file when the file exists, so such a token is refused rather than left
#: to be disambiguated by the working directory's contents.
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")


def rejected_specs(specs: list[str]) -> list[str]:
    """Requirements that must never reach pip, with the reason for each.

    The module's whole premise is that pip is asked for DEPENDENCIES and never for
    the project, because installing the project is what rewrites the locked console
    script. Until now that was a property of this repository's declarations rather
    than something enforced: a declaration of ``.`` would have pip install the
    checkout itself, remove the editable install, and then fail on the locked
    executable -- leaving the venv unable to import the package at all, which is
    the exact damage the step exists to avoid.

    Two shapes are refused: a head that is not a plain distribution name (a path,
    a URL, an archive, a leftover option), and any requirement that normalizes to
    this project's own name however it is spelled.
    """
    bad: list[str] = []
    for spec in specs:
        head = _NAME_END.split(spec.strip(), 1)[0].strip()
        if not _PLAIN_NAME.match(head):
            bad.append(f"{spec!r} is not a plain requirement name")
        elif head.lower().endswith(_ARCHIVE_SUFFIXES):
            # A bare `foo.whl` is a legal NAME by character set, but pip resolves it
            # as a local file whenever one exists in the working directory, so it is
            # refused rather than disambiguated by what happens to be on disk.
            bad.append(f"{spec!r} names an archive rather than a distribution")
        elif normalize(head) == _PROJECT:
            bad.append(f"{spec!r} names this project, whose reinstall is the blocker")
    return bad


def normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503 so lookups compare equal."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def requirement_name(spec: str) -> str:
    """The distribution name at the head of a requirement line."""
    head = _NAME_END.split(spec.strip(), 1)[0]
    return normalize(head)


def _requirement_lines(raw: str) -> list[str]:
    """Requirement lines from one setup.cfg value, comments dropped.

    setup.cfg carries a full-line ``#`` comment for most requirements here, and
    configparser keeps them, so they have to be stripped before the value is
    handed to pip. A trailing comment on a requirement line is NOT stripped: a
    PEP 508 marker can legitimately contain ``#`` inside a quoted string, and this
    project's declarations put their commentary on their own lines.
    """
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def show(repo: Path, git_bin: Path, rev: str, path: str) -> str | None:
    """``<path>`` as of *rev*, or ``None`` when it cannot be read.

    Reading through git rather than the working tree is what lets this step run
    before the merge: the incoming revision's declarations are available without
    advancing HEAD, so a failure here leaves the checkout untouched.
    """
    proc = subprocess.run(
        [str(git_bin), "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


#: Where the incoming revision is pinned. A background fetch can advance
#: ``<remote>/<base>`` between this step and the merge that follows it, so the two
#: would be judging and consuming DIFFERENT commits -- dependencies installed for
#: one revision, code merged from another, which is the missing-dependency outcome
#: this step exists to prevent. Only this module writes this ref, so once pinned it
#: cannot move under the merge.
PIN_REF = "refs/kirocrew/dep-sync-target"


def _git_succeeds(repo: Path, git_bin: Path, *args: str) -> bool:
    """Whether a git command exits 0. Used for predicates, not for output."""
    proc = subprocess.run(
        [str(git_bin), "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0


def _git_output(repo: Path, git_bin: Path, *args: str) -> str | None:
    """A git command's stdout, or ``None`` when it fails or prints nothing."""
    proc = subprocess.run(
        [str(git_bin), "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def pin_incoming(repo: Path, git_bin: Path, ref: str) -> str | None:
    """Resolve *ref* once and pin that OID at :data:`PIN_REF`; return the OID.

    The merge step is given :data:`PIN_REF` rather than the mutable remote-tracking
    ref, so every later read here and the merge itself address the same object even
    if a concurrent fetch advances the remote ref meanwhile.
    """
    oid = _git_output(repo, git_bin, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if oid is None:
        return None
    if not _git_succeeds(repo, git_bin, "update-ref", PIN_REF, oid):
        return None
    return oid


def merge_not_guaranteed(
    repo: Path, git_bin: Path, installed_rev: str, incoming_rev: str
) -> str | None:
    """Why the following ``--ff-only`` merge could fail, or ``None`` if it cannot.

    This is the invariant the whole step rests on. Installing dependencies and
    merging are two writes that cannot be made one transaction, so whichever runs
    second can fail and leave the other applied: merge-then-install can strand a
    merged revision with missing dependencies, and install-then-merge can strand
    an upgraded venv on an unmerged checkout -- pip does not only ADD packages, it
    replaces existing ones. Ordering alone therefore cannot fix this; the fix is to
    only start when the second write cannot fail.

    A clean tree plus an installed revision that is an ancestor of the incoming one
    is exactly the condition under which ``git merge --ff-only`` has nothing to
    refuse: no local modifications to overwrite and no divergence to reconcile. When
    either does not hold, nothing is installed and nothing is merged.
    """
    dirty = subprocess.run(
        [str(git_bin), "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if dirty.returncode != 0:
        return "the checkout's status could not be read"
    if dirty.stdout.strip():
        return (
            "the checkout has local modifications, so the merge could fail after "
            "the venv had already been changed"
        )
    if not _git_succeeds(repo, git_bin, "merge-base", "--is-ancestor", installed_rev, incoming_rev):
        return (
            f"{installed_rev} is not an ancestor of {incoming_rev}, so the merge "
            "would not fast-forward"
        )
    return None


def dependency_authority_moved(repo: Path, git_bin: Path, rev: str) -> str | None:
    """Why *rev*'s requirements cannot be read from setup.cfg, or ``None``.

    This module reads requirements out of setup.cfg because that is where this
    project declares them, with pyproject marking ``dependencies`` and
    ``optional-dependencies`` dynamic. A revision that moves them into
    pyproject's own ``[project]`` table would make setup.cfg stale, and reading it
    anyway would install the wrong set and report success. The assumption is
    therefore checked against the incoming revision rather than trusted.
    """
    pyproject = show(repo, git_bin, rev, "pyproject.toml")
    if pyproject is None:
        return "the incoming revision has no pyproject.toml to check"
    project = _section(pyproject, "project") or ""
    declared_dynamic = _declared_dynamic(project)
    for field in ("dependencies", "optional-dependencies"):
        if f'"{field}"' not in declared_dynamic and f"'{field}'" not in declared_dynamic:
            return (
                f"the incoming revision no longer declares {field!r} as dynamic, so "
                "setup.cfg is not where its requirements live"
            )
    return None


def _parse_requirements(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Base requirements and extras declared by one setup.cfg's *text*."""
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    base = _requirement_lines(cfg.get("options", "install_requires", fallback=""))
    extras = {
        name: _requirement_lines(value)
        for name, value in (
            cfg.items("options.extras_require") if cfg.has_section("options.extras_require") else []
        )
    }
    return base, extras


def declared_requirements_at(
    repo: Path, git_bin: Path, rev: str
) -> tuple[list[str], dict[str, list[str]]] | None:
    """Declarations as of *rev*, or ``None`` when that revision cannot be read.

    ``None`` is a refusal signal, not an empty answer: without the declarations
    there is no sound way to tell an extra the operator uses from one they do not,
    and guessing is what would drop a required dependency.
    """
    text = show(repo, git_bin, rev, "setup.cfg")
    return None if text is None else _parse_requirements(text)


def requires_python_at(repo: Path, git_bin: Path, rev: str) -> str | None:
    """The ``requires-python`` value declared by *rev*, if any.

    Parsed with a regex rather than a TOML reader because ``tomllib`` is 3.11+ and
    this package still supports 3.10, and the value needed is a single scalar.
    """
    text = show(repo, git_bin, rev, "pyproject.toml")
    if text is None:
        return None
    match = re.search(r"^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    return match.group(1) if match else None


def interpreter_version(target_py: Path) -> tuple[int, int, int] | None:
    """``(major, minor, micro)`` of *target_py*, or ``None`` if it cannot be asked.

    The micro component matters: a ``requires-python`` floor can name one
    (``>=3.10.5``), and reporting only the minor would make a 3.10.0 interpreter
    indistinguishable from a 3.10.5 one at exactly the boundary being checked.
    """
    proc = subprocess.run(
        [str(target_py), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        major, minor, micro = proc.stdout.strip().split(".")
        return int(major), int(minor), int(micro)
    except ValueError:
        return None


def python_floor_breach(spec: str, version: tuple[int, ...]) -> str | None:
    """The highest ``requires-python`` floor *version* fails, if it fails one.

    ``pip install -e .`` doubles as this project's interpreter gate: a revision
    that raises its floor and starts using newer syntax is refused by pip rather
    than installed. A dependency-only sync spawns no such check, so the gate has
    to be applied here or a merged revision becomes unimportable under the
    interpreter that has to import it.

    Compared at three components, with an absent micro reading as ``.0`` on both
    sides, so ``>=3.10.5`` refuses a 3.10.0 interpreter instead of being
    truncated to its minor. A bare ``>`` excludes the named version itself.
    """
    v = (tuple(version) + (0, 0, 0))[:3]
    breached: tuple[int, int, int] | None = None
    breached_text: str | None = None
    for op, major, minor, micro in _PY_FLOOR.findall(spec):
        floor = (int(major), int(minor), int(micro) if micro else 0)
        fails = v <= floor if op == ">" else v < floor
        if fails and (breached is None or floor > breached):
            breached = floor
            breached_text = f"{major}.{minor}" + (f".{micro}" if micro else "")
    return breached_text


def installed_names(target_py: Path) -> set[str]:
    """Normalized names of every distribution installed in *target_py*'s venv.

    Read through the target interpreter rather than this process: the two are
    different venvs whenever the app backend runs from a feature worktree, and the
    answer must describe the venv pip will install into.
    """
    probe = (
        "import importlib.metadata as m, json;"
        "print(json.dumps([d.metadata['Name'] for d in m.distributions()"
        " if d.metadata['Name']]))"
    )
    proc = subprocess.run(
        [str(target_py), "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    return {normalize(n) for n in json.loads(proc.stdout)}


def active_extras(extras: dict[str, list[str]], present: set[str]) -> list[str]:
    """Extras whose every named distribution is already installed.

    *extras* must be the declarations the venv was INSTALLED FROM -- see the module
    docstring for why judging against the incoming ones drops a dependency.
    """
    active = []
    for name, specs in extras.items():
        names = {requirement_name(s) for s in specs}
        if names and names <= present:
            active.append(name)
    return sorted(active)


#: What :func:`console_script_target_at` returns when the authoritative
#: declaration EXISTS and omits the script: the entry point is removed as of that
#: revision. Distinct from ``None``, which means the declarations could not be
#: read at all -- collapsing the two would let a removal merge silently, leaving
#: the locked wrapper dispatching to a target the revision may have deleted. A
#: truthy string that is never a valid ``module:attr`` (it contains spaces), so
#: the mismatch comparison in ``main`` treats a removal like any other
#: disagreement between the declaration and the installed wrapper.
SCRIPT_REMOVED = "removed by the incoming revision"


def console_script_target_at(repo: Path, git_bin: Path, rev: str, script: str) -> str | None:
    """The ``module:attr`` *script* is declared to dispatch to as of *rev*.

    ``pyproject.toml``'s ``[project.scripts]`` is authoritative and is consulted
    first: this project declares ``dependencies`` and ``optional-dependencies`` as
    dynamic but NOT ``scripts``, so setuptools builds the wrapper from the
    pyproject table and ignores setup.cfg's copy. Reading only setup.cfg would
    miss a repoint made in the file that actually decides it.

    setup.cfg is consulted only when setuptools itself would read it: when there
    is no ``[project]`` table at all (pre-PEP 621 metadata), or when ``scripts``
    is declared dynamic. A ``[project]`` table that neither names ``scripts``
    (as a ``[project.scripts]`` table, an inline table, or dotted keys -- any
    spelling TOML allows counts) nor declares it dynamic makes the field
    statically absent: setuptools builds no wrapper from setup.cfg's copy
    (verified empirically against setuptools 80.9), so the entry point has been
    REMOVED as of *rev* -- reported as :data:`SCRIPT_REMOVED`, not read out of
    setup.cfg's stale copy, which would report an agreement that no longer
    holds. The same removal answer covers a table that exists but omits
    *script*. An omission in setup.cfg itself (when it IS the authority) stays
    ``None`` rather than a removal: setup.py can also declare entry points and
    cannot be read here, so that omission is missing evidence, not a known
    removal. Read with the real TOML parser when the interpreter provides one
    (tomllib on 3.11+, tomli when importable); the hardened line scanner is the
    fallback for 3.10 without tomli and for text the parser rejects. Successive
    reviews produced one scanner-missed spelling per round (inline tables,
    header comments, multiline-string contents), which is the signal the
    scanner cannot be the primary path.
    """
    pyproject = show(repo, git_bin, rev, "pyproject.toml")

    # Primary path: a real TOML parser, when the interpreter provides one. The
    # scanner below exists only for 3.10 without tomli (and unparseable text);
    # it cannot be made spelling-complete, which successive review rounds
    # demonstrated one spelling at a time.
    parsed = _toml_load(pyproject) if pyproject is not None else None
    if parsed is not None:
        project_tbl = parsed.get("project")
        if isinstance(project_tbl, dict):
            scripts_tbl = project_tbl.get("scripts")
            if isinstance(scripts_tbl, dict):
                target = scripts_tbl.get(script)
                return target if isinstance(target, str) else SCRIPT_REMOVED
            dynamic_val = project_tbl.get("dynamic")
            dynamic_list = dynamic_val if isinstance(dynamic_val, list) else []
            if "scripts" not in dynamic_list:
                return SCRIPT_REMOVED
        return _setup_cfg_script_target(repo, git_bin, rev, script)

    scripts = _section(pyproject, "project.scripts") if pyproject is not None else None
    if scripts is None and pyproject is not None:
        scripts = _inline_scripts(pyproject)
    if scripts is not None:
        key = rf"(?:{re.escape(script)}|\"{re.escape(script)}\"|'{re.escape(script)}')"
        match = re.search(
            rf"^\s*{key}\s*=\s*[\"']([^\"']+)[\"']",
            scripts,
            re.M,
        )
        return match.group(1) if match else SCRIPT_REMOVED

    if pyproject is not None:
        # No scripts declaration anywhere in pyproject. setup.cfg's copy governs
        # only when setuptools would actually read it: either there is no
        # ``[project]`` table at all (pre-PEP 621 metadata), or ``scripts`` is
        # declared dynamic. A ``[project]`` table that neither names ``scripts``
        # nor declares it dynamic makes the field statically absent -- setuptools
        # builds no wrapper and ignores setup.cfg's copy (verified empirically
        # against setuptools 80.9) -- so the entry point is removed as of *rev*
        # and setup.cfg's agreement is stale.
        project = _section(pyproject, "project")
        if project is not None:
            declared_dynamic = _declared_dynamic(project)
            if '"scripts"' not in declared_dynamic and "'scripts'" not in declared_dynamic:
                return SCRIPT_REMOVED
    return _setup_cfg_script_target(repo, git_bin, rev, script)


def _setup_cfg_script_target(repo: Path, git_bin: Path, rev: str, script: str) -> str | None:
    """setup.cfg's declared target for *script* at *rev*, or ``None``."""
    cfg_text = show(repo, git_bin, rev, "setup.cfg")
    if cfg_text is None:
        return None
    cfg = configparser.ConfigParser()
    cfg.read_string(cfg_text)
    raw = cfg.get("options.entry_points", "console_scripts", fallback="")
    for line in _requirement_lines(raw):
        name, _, target = line.partition("=")
        if name.strip() == script:
            return target.strip()
    return None


def _toml_load(toml_text: str) -> dict | None:
    """*toml_text* parsed by the real TOML parser, or ``None`` when unavailable.

    ``None`` means "no authoritative parse exists" -- either the interpreter has
    no parser (3.10 without tomli) or the text is not valid TOML -- and sends the
    caller to the line scanner, which degrades the same way it always has rather
    than inventing a new failure mode for invalid input.
    """
    if _toml is None:
        return None
    try:
        return _toml.loads(toml_text)
    except Exception:
        return None


def _inline_scripts(toml_text: str) -> str | None:
    """``[project]``'s ``scripts`` declared inline or dotted, as flat lines.

    TOML lets the scripts table be spelled without its own ``[project.scripts]``
    header: an inline table (``scripts = { name = "..." }``) or dotted keys
    (``scripts.name = "..."``) inside ``[project]``. Either spelling is the same
    authoritative declaration setuptools builds the wrapper from, so reading
    setup.cfg when a revision uses one of them would consult the stale copy in
    exactly the case where the two disagree. Returns the entries as
    ``name = value`` lines (an empty string when the declaration exists and
    names nothing), or ``None`` when ``[project]`` declares no ``scripts`` key
    in either form.
    """
    project = _section(toml_text, "project")
    if project is None:
        return None
    entries: list[str] = []
    found = False
    for line in project.splitlines():
        dotted = re.match(
            r"\s*scripts\s*\.\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9._-]+))\s*=\s*(.+)",
            line,
        )
        if dotted:
            found = True
            name = dotted.group(1) or dotted.group(2) or dotted.group(3)
            entries.append(f"{name} = {dotted.group(4)}")
            continue
        inline = re.match(r"\s*scripts\s*=\s*\{(.*)\}\s*(?:#.*)?$", line)
        if inline:
            found = True
            for item in inline.group(1).split(","):
                key, sep, value = item.partition("=")
                if sep:
                    name = key.strip().strip("'\"")
                    entries.append(f"{name} = {value.strip()}")
    return "\n".join(entries) if found else None


#: TOML multiline strings (basic and literal). Their CONTENTS are data, not
#: structure: a line inside one can look exactly like a table header or a
#: ``name = "value"`` entry, so the line scanner blanks them before scanning.
#: Non-greedy matching ends a basic string at the first unescaped-looking
#: delimiter -- an embedded ``\"""`` escape can end it early, which is accepted
#: for a fallback path (the parser-first primary path is exact).
_TOML_MULTILINE_STRING = re.compile(r'"""(?:[^"]|"(?!""))*"""|\'\'\'(?:[^\']|\'(?!\'\'))*\'\'\'')


def _blank_multiline_strings(toml_text: str) -> str:
    """*toml_text* with every multiline string's span reduced to blank lines."""
    return _TOML_MULTILINE_STRING.sub(lambda m: "\n" * m.group(0).count("\n"), toml_text)


def _declared_dynamic(project_body: str) -> str:
    """The ``dynamic = [...]`` array's contents with comments removed.

    TOML allows comments between the elements of a multi-line array, so comment
    text must not read as a declared field -- in either direction: a commented
    ``"scripts"`` must not send the console-script read to setup.cfg's stale
    copy, and a commented ``"dependencies"`` must not make a moved field look
    still-dynamic. Legal PEP 621 dynamic values are bare quoted field names, so
    everything from ``#`` to end of line is comment, never value.
    """
    match = re.search(r"^\s*dynamic\s*=\s*\[([^\]]*)\]", project_body, re.M)
    if not match:
        return ""
    return "\n".join(re.sub(r"#.*", "", line) for line in match.group(1).splitlines())


def _section(toml_text: str, header: str) -> str | None:
    """The body of one ``[header]`` table, or ``None`` when the table is absent.

    Presence and content answer different questions -- an existing table that
    omits an entry is a REMOVAL of that entry, not an invitation to read a stale
    copy elsewhere -- so absence is reported distinctly rather than as an empty
    body. Done without a TOML parser: only ever used on flat tables whose
    entries are ``name = "value"`` lines. Multiline strings are blanked first so
    their contents cannot masquerade as headers or entries. The header
    comparison tolerates the spellings TOML allows on the header line itself --
    whitespace around the dotted parts, quoted parts, and a trailing comment --
    because a header that fails to be RECOGNIZED here reads as the table being
    absent, which is a different answer with different consequences.
    """
    wanted = header.split(".")
    inside = False
    found = False
    out: list[str] = []
    for line in _blank_multiline_strings(toml_text).splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            matched = re.match(r"\[([^\]]*)\]\s*(?:#.*)?$", stripped)
            parts = (
                [p.strip().strip("\"'") for p in matched.group(1).split(".")]
                if matched
                else None
            )
            inside = parts == wanted
            found = found or inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out) if found else None


def installed_console_script_target(target_py: Path, script: str) -> str | None:
    """The ``module:attr`` *script* currently dispatches to in *target_py*'s venv.

    Reads the entry points off the ``kirocrew`` distribution rather than the
    module-level ``entry_points()`` selector: that function returns a group->list
    mapping on 3.10/3.11 and an ``EntryPoints`` sequence on 3.12+, and this project
    supports both. A distribution's own ``entry_points`` is a sequence in every
    supported version, and scoping the lookup to the distribution also keeps a
    same-named script from another package out of the answer.

    Returns ``None`` when the answer cannot be read at all. The caller only
    compares two KNOWN values, so an unreadable probe leaves the sync alone
    instead of failing it on missing evidence.
    """
    probe = (
        "import importlib.metadata as m;"
        "d=m.distribution('kirocrew');"
        "print(next((e.value for e in d.entry_points"
        f" if e.group=='console_scripts' and e.name=={script!r}), ''))"
    )
    proc = subprocess.run(
        [str(target_py), "-c", probe],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def installed_package_origin(target_py: Path) -> str | None:
    """Where *target_py*'s venv resolves this project's package FROM.

    Read through the target interpreter for the same reason ``installed_names``
    does: the venv being written to is not necessarily this process's own.

    ``find_spec`` rather than an import, so nothing in the package runs; and the
    spec rather than installed metadata, because the metadata proxies for this
    answer instead of giving it. A venv can carry a working PEP 660 editable
    install whose ``direct_url.json`` is absent -- one repaired by hand, or
    installed by a route that never wrote it -- and refusing that venv would
    refuse the ordinary single-checkout layout this whole step exists to serve.
    The import path is what the installed dependencies will be imported alongside,
    so it is the thing worth checking.

    Returns ``None`` when the package cannot be located at all.
    """
    probe = (
        "import importlib.util as u, os;"
        "s=u.find_spec('kiro_crew');"
        "print(os.path.abspath(s.origin) if s and s.origin else '')"
    )
    proc = subprocess.run(
        [str(target_py), "-c", probe],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def venv_not_mapped_to(origin: str | None, repo: Path) -> str | None:
    """Why the venv at *origin* cannot be shown to serve *repo*, if it cannot.

    The venv is addressed as ``<repo>/.venv``, but that is a location, not a
    binding: nothing stops it from being an install of a DIFFERENT checkout.
    Installing this revision's dependencies into such a venv upgrades the runtime
    another checkout is served by, which is how a working checkout gets broken by a
    sync that never touched it.

    The test is therefore whether that venv imports this project from inside
    *repo*. A yes means the write cannot reach another checkout's runtime, which is
    the whole of what has to hold. An unreadable answer refuses too -- unproven is
    not the same as safe, and refusing costs nothing this path did not already cost,
    since the reinstall it stands in for was refused outright.
    """
    if origin is None:
        return (
            "the target venv does not resolve this project's package at all, so it "
            "cannot be shown to serve this checkout"
        )
    mapped = os.path.normcase(str(Path(origin).resolve()))
    root = os.path.normcase(str(Path(repo).resolve()))
    if mapped != root and not mapped.startswith(root + os.sep):
        return (
            f"the target venv imports this project from {Path(origin).resolve()}, "
            f"which is outside {Path(repo).resolve()}, so a dependency install "
            "would change a runtime this sync does not own"
        )
    return None


def plan(
    incoming: tuple[list[str], dict[str, list[str]]],
    previous_extras: dict[str, list[str]],
    present: set[str],
) -> tuple[list[str], list[str], list[str]]:
    """Specs to hand pip, the extras they came from, and the extras left alone."""
    base, extras = incoming
    chosen = active_extras(previous_extras, present)
    specs = list(base)
    for name in chosen:
        specs.extend(extras.get(name, []))
    skipped = sorted((set(extras) | set(previous_extras)) - set(chosen))
    return specs, chosen, skipped


def _refuse(message: str, target_py: Path, repo: Path, remedy: str | None = None) -> int:
    if remedy is None:
        remedy = (
            "Stop the gateway and sync from a terminal: "
            f'"{target_py}" -m pip install -e "{repo}"'
        )
    print(
        f"dep-sync: {message} Nothing was merged, so the checkout still has "
        f"satisfied dependencies. {remedy}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 5:
        print(
            "usage: dep_sync <repo> <target-python> <git-binary> <installed-rev> " "<incoming-rev>",
            file=sys.stderr,
        )
        return 2
    repo, target_py, git_bin = Path(args[0]), Path(args[1]), Path(args[2])
    installed_rev, incoming_ref = args[3], args[4]

    # Establish that the venv about to be written to serves THIS checkout, before
    # anything is pinned, installed or merged. `<repo>/.venv` is where the
    # interpreter was found, which says nothing about what it is an install of;
    # every other precondition below reasons about the checkout, and all of that
    # reasoning is void if the runtime belongs to someone else.
    foreign = venv_not_mapped_to(installed_package_origin(target_py), repo)
    if foreign:
        return _refuse(
            f"{foreign}.",
            target_py,
            repo,
            remedy=(
                "Give this checkout its own editable install, or run the sync from "
                "the checkout that venv serves."
            ),
        )

    # Pin FIRST: everything below -- the declarations read, the ancestor check, the
    # console-script comparison -- and the merge that follows this step must all
    # address the same commit. Resolving the mutable remote ref more than once is
    # what would let a concurrent fetch slide a newer revision under the merge.
    incoming_rev = pin_incoming(repo, git_bin, incoming_ref)
    if incoming_rev is None:
        return _refuse(
            f"cannot resolve {incoming_ref} to a commit and pin it, so the revision "
            "the dependencies are installed for cannot be the same one the merge "
            "consumes.",
            target_py,
            repo,
        )

    previous = declared_requirements_at(repo, git_bin, installed_rev)
    if previous is None:
        return _refuse(
            f"cannot read setup.cfg at {installed_rev}, so which extras this venv "
            "was installed from is unknown, and guessing is what would leave a "
            "required dependency uninstalled.",
            target_py,
            repo,
        )
    incoming = declared_requirements_at(repo, git_bin, incoming_rev)
    if incoming is None:
        return _refuse(
            f"cannot read setup.cfg at {incoming_rev}, so the requirements the "
            "incoming revision declares are unknown.",
            target_py,
            repo,
        )

    # Two preconditions, both checked before ANYTHING is installed.
    #
    # The first is the step's central invariant: installing and merging cannot be
    # made one transaction, so the only way neither can be left applied without the
    # other is to start only when the merge cannot fail. See merge_not_guaranteed.
    blocked = merge_not_guaranteed(repo, git_bin, installed_rev, incoming_rev)
    if blocked:
        return _refuse(f"{blocked}.", target_py, repo)

    # The second guards this module's one assumption about where requirements live.
    moved = dependency_authority_moved(repo, git_bin, incoming_rev)
    if moved:
        return _refuse(
            f"{moved}, so a dependency-only sync would install a stale set.",
            target_py,
            repo,
        )

    # The interpreter gate `pip install -e .` would have applied. Checked before
    # anything is installed, because a revision this interpreter cannot run must
    # not be merged at all.
    floor_spec = requires_python_at(repo, git_bin, incoming_rev)
    version = interpreter_version(target_py) if floor_spec else None
    if floor_spec and version:
        breach = python_floor_breach(floor_spec, version)
        if breach:
            return _refuse(
                f"the incoming revision requires Python {floor_spec} but the "
                f"target venv runs {'.'.join(map(str, version))}, so its code could "
                "not be imported after a merge.",
                target_py,
                repo,
            )

    # The one thing a dependency-only sync structurally cannot deliver: if the
    # incoming revision REPOINTED or REMOVED the console script, the wrapper on
    # disk still dispatches to the old target and no amount of dependency
    # installing refreshes it. The comparison needs nothing from the install, so
    # it runs here with every other refusal -- before pip as well as before the
    # merge, keeping the invariant that a refusal leaves BOTH writes unapplied.
    declared = console_script_target_at(repo, git_bin, incoming_rev, _SCRIPT)
    installed = installed_console_script_target(target_py, _SCRIPT)
    removed = declared == SCRIPT_REMOVED
    if removed or (declared and installed and declared != installed):
        # A removal refuses even when the installed wrapper's own target cannot
        # be read: the removal is a fact about the incoming revision alone, and
        # whatever the locked wrapper dispatches to, the revision that removed
        # the entry point no longer promises that target exists.
        change = "removed" if removed else f"repointed to {declared}"
        wrapper = (
            f"the installed wrapper still calls {installed}"
            if installed
            else "the installed wrapper's target cannot be read to confirm agreement"
        )
        return _refuse(
            f"the {_SCRIPT!r} console script is {change} by the incoming revision "
            f"while {wrapper}, and that "
            "wrapper cannot be rewritten while a process is running from it.",
            target_py,
            repo,
        )

    specs, chosen, skipped = plan(incoming, previous[1], installed_names(target_py))
    if not specs:
        print("dep-sync: no requirements declared; nothing to install")
    else:
        rejected = rejected_specs(specs)
        if rejected:
            return _refuse(
                "the incoming revision declares requirements this step will not "
                f"hand to pip: {'; '.join(rejected)}.",
                target_py,
                repo,
            )
        print(
            f"dep-sync: {len(specs)} declared requirements; extras active (judged "
            f"against {installed_rev}): {', '.join(chosen) if chosen else 'none'}; "
            f"extras left alone: {', '.join(skipped) if skipped else 'none'}"
        )
        # Hand every spec to pip and let it decide: an already-satisfied
        # requirement is a no-op, so this installs what is new and leaves the rest
        # alone without this module ever comparing a version. The project itself is
        # deliberately absent from the list -- installing it is what would rewrite
        # the locked console script.
        # `--` ends pip's option parsing, so a declared requirement can never be
        # read as a flag. Without it a line beginning with `-` in the incoming
        # setup.cfg -- `--target <path>`, or a bare `.` -- would be consumed as a
        # pip option and could write package files into the checkout before the
        # merge. The declarations come from the revision being synced, so this
        # costs one token and removes the argument-injection surface entirely.
        proc = subprocess.run([str(target_py), "-m", "pip", "install", "--", *specs])
        if proc.returncode != 0:
            return proc.returncode

    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
