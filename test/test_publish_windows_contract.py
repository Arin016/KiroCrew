"""Contract tests for the publish-windows lane's ordering and immutability.

The lane's documented guarantees, each pinned structurally because a
plausible-looking edit re-opens a failure mode that only manifests in the
field (a client mid-update, a job re-run, a soft-failed build leg):

* **No pointer ever names bytes that are not live.** Squirrel's `RELEASES`
  must be written only after its `.nupkg`; the `latest` installer alias only
  after the immutable versioned key; `latest-win.json` last of all. Reorder
  any of these and a client can read a pointer to a 404.
* **Immutable keys are never republished with different bytes.** Both
  payload writes use a conditional put and, on the 412 re-run path, verify
  the published bytes are identical before letting any pointer move --
  otherwise a re-run that produces different bytes leaves the mutable
  pointers disagreeing with the immutable key (the CloudFront
  edge-divergence class fixed for `cli/*` in #62).
* **`RELEASES` is validated against the package being published.** Squirrel
  resolves each entry relative to the RELEASES directory, so an entry naming
  a different file -- or carrying a path/URL -- breaks or escapes the
  published prefix.
* **A missing build artifact fails loudly.** `build-desktop.yml` marks its
  Windows leg continue-on-error for publishing callers, so this lane can be
  reached with nothing to publish; a silent skip would serve a stale feed
  under a green run (issue #487's revisit item).
* **Both callers wire it identically to the Linux lane** (`needs:
  [version, build-desktop]`, `environment: prod` via the reusable workflow,
  publish gates on the CDN vars).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-windows.yml"
CALLERS = (
    ROOT / ".github" / "workflows" / "nightly.yml",
    ROOT / ".github" / "workflows" / "release.yml",
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step_index(name_fragment: str) -> int:
    """Line index of the step whose name contains the fragment."""
    lines = _text().splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*- name: ", line) and name_fragment.lower() in line.lower():
            return i
    raise AssertionError(f"no step matching {name_fragment!r} in publish-windows.yml")


def test_releases_pointer_is_written_after_its_package() -> None:
    """Squirrel reads RELEASES then fetches the .nupkg it names."""
    assert _step_index("Publish update package") < _step_index("Update Squirrel RELEASES"), (
        "RELEASES is written before its .nupkg is live -- a client that polls in "
        "between reads a pointer to a 404"
    )


def test_latest_alias_is_written_after_the_versioned_key() -> None:
    assert _step_index("Publish Setup.exe") < _step_index("Update latest installer alias"), (
        "the latest alias is written before the immutable versioned key -- the "
        "permalink can serve bytes that are not yet published"
    )


def test_feed_is_written_last() -> None:
    """The channel pointer is the terminal go-live switch."""
    feed = _step_index("Write update feed")
    for earlier in (
        "Publish Setup.exe",
        "Publish update package",
        "Update Squirrel RELEASES",
        "Update latest installer alias",
    ):
        assert _step_index(earlier) < feed, (
            f"{earlier!r} runs AFTER the feed write; the feed must be the last "
            "pointer moved so it never names bytes or pointers that are not live"
        )


def test_immutable_writes_are_conditional_and_verify_on_reruns() -> None:
    """Both payloads: conditional put + identical-bytes check on the 412 path."""
    text = _text()
    conditional = text.count("--if-none-match '*'")
    assert conditional == 2, (
        f"expected exactly 2 conditional writes (Setup.exe + .nupkg), found "
        f"{conditional}; an unconditional put can republish an immutable key"
    )
    precondition_paths = text.count("PreconditionFailed")
    assert precondition_paths == 2, (
        f"expected a 412 re-run path for each immutable write, found " f"{precondition_paths}"
    )
    # Each 412 path must compare hashes and refuse rather than continue.
    assert text.count("already holds DIFFERENT bytes") == 2, (
        "a 412 path continues without proving the published bytes match this "
        "build; the mutable pointers would then disagree with the immutable key"
    )


def _step_body(name_fragment: str) -> str:
    """Text of one step, from its `- name:` line to the next step."""
    lines = _text().splitlines()
    start = _step_index(name_fragment)
    for j in range(start + 1, len(lines)):
        if re.match(r"\s*- name: ", lines[j]):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


def test_mutable_pointers_use_short_ttl() -> None:
    """Every pointer must be re-writable: no immutable cache on a pointer."""
    for pointer in (
        "Update Squirrel RELEASES",
        "Update latest installer alias",
        "Write update feed",
    ):
        body = _step_body(pointer)
        assert "max-age=300" in body, (
            f"{pointer!r} does not set a short TTL; a pointer cached as "
            "immutable cannot be updated for a year"
        )
        assert (
            "immutable" not in body
        ), f"{pointer!r} is cached as immutable -- it is a mutable pointer"


def test_releases_is_validated_against_the_published_package() -> None:
    """Guard the RELEASES <-> .nupkg agreement before publishing."""
    text = _text()
    verify_idx = _step_index("Verify RELEASES references")
    assert verify_idx < _step_index(
        "Publish update package"
    ), "RELEASES is validated after the package is published"
    assert 'grep -Fq "${NUPKG_NAME}"' in text, (
        "the lane does not assert RELEASES names the .nupkg it publishes; "
        "Squirrel clients would 404 on update"
    )
    assert "non-relative filename" in text, (
        "the lane does not reject a RELEASES entry carrying a path or URL, "
        "which would escape the published prefix"
    )


def test_provenance_is_attested_before_any_upload() -> None:
    """Un-attested bytes must never reach the CDN."""
    attest = _step_index("Attest Windows artifact provenance")
    assert attest < _step_index("Publish Setup.exe")
    assert attest < _step_index("Publish update package")
    text = _text()
    assert "${{ env.SETUP_PATH }}" in text and "${{ env.NUPKG_PATH }}" in text, (
        "attestation must cover BOTH payloads -- the installer humans download "
        "and the package Squirrel downloads"
    )


def test_missing_artifact_fails_rather_than_skipping() -> None:
    """A soft-failed Windows build leg must not yield a silently stale feed."""
    text = _text()
    assert re.search(
        r"::error::Expected exactly one Setup", text
    ), "the lane does not fail loudly when the Setup.exe is absent"
    assert re.search(r"::error::Expected exactly one \*-full\.nupkg", text)
    assert re.search(r"::error::Expected exactly one RELEASES", text)
    # No continue-on-error KEY anywhere in this lane: the whole point is
    # loudness. (Matched as a YAML key, not a substring -- the header prose
    # legitimately explains build-desktop's continue-on-error interaction.)
    assert not re.search(r"^\s*continue-on-error:", text, re.MULTILINE), (
        "publish-windows must not tolerate its own failures -- that is how a "
        "stale channel pointer survives a green run"
    )


def test_publish_gates_require_both_cdn_vars() -> None:
    text = _text()
    assert "vars.CLI_DIST_BUCKET != ''" in text and "vars.CLI_CDN_BASE != ''" in text, (
        "both CDN vars must gate the job: the bucket receives the upload and "
        "the CDN base is baked into public URLs and the 412 byte-verify"
    )


def test_prod_environment_is_declared() -> None:
    """The publish role's OIDC trust only accepts main or environment:prod."""
    assert re.search(r"^    environment: prod$", _text(), re.MULTILINE), (
        "without environment: prod, tag-triggered release runs present a "
        "subject the publish role's trust policy rejects"
    )


def test_both_callers_wire_the_lane_like_the_linux_lane() -> None:
    for caller in CALLERS:
        text = caller.read_text(encoding="utf-8")
        assert "uses: ./.github/workflows/publish-windows.yml" in text, (
            f"{caller.name} does not call publish-windows.yml -- a channel would "
            "publish mac/Linux but silently never publish Windows"
        )
        block = re.search(r"  publish-windows:\n(.*?)(?=\n  [a-z][a-z0-9-]*:\n)", text, re.DOTALL)
        assert block, f"{caller.name}: cannot isolate the publish-windows job block"
        body = block.group(1)
        assert "needs: [version, build-desktop]" in body, (
            f"{caller.name}: publish-windows must depend on version + build-desktop "
            "only -- depending on the wheel or the macOS chain would re-couple lanes"
        )
        assert (
            "windows_artifact: build-windows-x64" in body
        ), f"{caller.name}: publish-windows is not passed the Windows build artifact"
        assert "id-token: write" in body and "attestations: write" in body, (
            f"{caller.name}: publish-windows needs OIDC + attestation permissions; "
            "a reusable workflow cannot widen the caller's grant"
        )


def test_feed_body_carries_the_windows_client_contract() -> None:
    """latest-win.json must give a client everything it needs."""
    text = _text()
    for field in ('"version"', '"setup"', '"releases"', '"nupkg"', '"sha256"', '"pub_date"'):
        assert field in text, f"latest-win.json is missing {field}"
    # `releases` must be the DIRECTORY Squirrel is handed, not the file: its
    # protocol resolves both RELEASES and each .nupkg relative to that base.
    assert re.search(
        r'"releases": "\$\{\{ vars\.CLI_CDN_BASE \}\}/desktop/\$\{CHANNEL\}/win/"', text
    ), (
        "the feed's `releases` value must be the trailing-slash directory URL "
        "Squirrel resolves against, not a path to the RELEASES file itself"
    )
