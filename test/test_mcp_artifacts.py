"""Tests for the artifact MCP tool handlers in :mod:`kiro_claw.mcp_core`.

Covers the dispatch branches for ``artifact_save``, ``artifact_get``,
``artifact_update``, ``artifact_list``, ``artifact_versions`` and
``artifact_delete`` — verifying schema validation, payload construction,
HTTP call shape, and result formatting.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from kiro_claw.mcp_core import _call_tool_inner


class TestArtifactSave:
    def test_minimal_save(self) -> None:
        with patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "my-widget", "version": 1, "name": "My Widget"},
        ) as post:
            result = _call_tool_inner(
                "artifact_save", {"name": "My Widget", "content": "<p>hi</p>"}
            )
        path, body = post.call_args.args
        assert path == "/api/artifacts"
        assert body["name"] == "My Widget"
        assert body["content"] == "<p>hi</p>"
        # Optional fields not in args don't appear.
        assert "slug" not in body
        assert "tags" not in body
        assert "Saved artifact" in result
        assert "my-widget" in result

    def test_optional_fields_passed(self) -> None:
        with patch("kiro_claw.mcp_core._post", return_value={"slug": "x", "version": 1}) as post:
            _call_tool_inner(
                "artifact_save",
                {
                    "name": "X",
                    "content": "<x/>",
                    "slug": "explicit",
                    "kind": "html",
                    "source": "manual",
                    "description": "desc",
                    "tags": ["a", "b"],
                },
            )
        body = post.call_args.args[1]
        assert body["slug"] == "explicit"
        assert body["kind"] == "html"
        assert body["source"] == "manual"
        assert body["description"] == "desc"
        assert body["tags"] == ["a", "b"]

    def test_error_propagated(self) -> None:
        with patch("kiro_claw.mcp_core._post", return_value={"error": "duplicate"}):
            result = _call_tool_inner(
                "artifact_save", {"name": "x", "content": "a"}
            )
        assert "Error: duplicate" in result

    def test_dedup_hint_when_existing_widget_with_same_name(self) -> None:
        # When the agent's about to artifact_save a widget whose name
        # matches an existing chat-source widget, the response includes
        # a duplicate-warning hint pointing at the existing slug. The
        # save still proceeds — we only WARN; we don't block.
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={
                "artifacts": [
                    {
                        "slug": "rules-of-fight-club",
                        "name": "Rules of Fight Club",
                        "updated_at": "2026-05-29T03:24:00Z",
                    },
                ],
            },
        ), patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "rules-of-fight-club-2", "version": 1},
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>v2</div>"},
            )
        assert "Possible duplicate" in result
        assert "rules-of-fight-club" in result
        assert "artifact_update" in result

    def test_dedup_hint_nfc_normalizes_name_match(self) -> None:
        # Agent emits "Café" in NFD form; existing artifact stored in
        # NFC form. Without normalization the byte-different titles
        # would compare as distinct and the hint would silently miss.
        nfd = "Cafe\u0301"  # decomposed
        nfc = "Caf\u00e9"  # composed (= "Café")
        assert nfd != nfc  # sanity
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={
                "artifacts": [
                    {"slug": "cafe", "name": nfc, "updated_at": "2026-05-29T03:00:00Z"},
                ],
            },
        ), patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "cafe-2", "version": 1},
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": nfd, "content": "<div>x</div>"},
            )
        assert "Possible duplicate" in result
        assert "cafe" in result

    def test_dedup_hint_skipped_when_explicit_slug_provided(self) -> None:
        # Agent passing explicit slug=foo means it knows what it's
        # doing — typically re-saving a known artifact. Don't surface
        # a hint that would just be noise. Probe shouldn't even fire.
        with patch("kiro_claw.mcp_core._get") as get, patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "foo", "version": 1},
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>x</div>", "slug": "foo"},
            )
        assert "Possible duplicate" not in result
        get.assert_not_called()

    def test_dedup_hint_skipped_for_default_widget_title(self) -> None:
        # name=="Widget" is the autogenerated fallback used when the
        # agent doesn't set a title — too collision-prone to dedup
        # against (every title-less widget would bind to the first
        # such artifact in the library).
        with patch("kiro_claw.mcp_core._get") as get, patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "x", "version": 1},
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Widget", "content": "<div>x</div>"},
            )
        assert "Possible duplicate" not in result
        get.assert_not_called()

    def test_dedup_hint_skipped_when_kind_is_not_widget(self) -> None:
        # Same-named markdown / html / json artifacts are a different
        # use case — they're often per-source-file or per-document
        # snapshots where collision is normal, not a sign of a mistake.
        with patch("kiro_claw.mcp_core._get") as get, patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "x", "version": 1},
        ):
            _call_tool_inner(
                "artifact_save",
                {"name": "Notes", "content": "# notes", "kind": "markdown"},
            )
        get.assert_not_called()

    def test_dedup_hint_swallows_probe_failure_silently(self) -> None:
        # If the artifact_list probe fails (network blip, store error),
        # we proceed with the save without the hint rather than letting
        # a transient observability concern block legitimate saves.
        with patch(
            "kiro_claw.mcp_core._get",
            side_effect=RuntimeError("probe boom"),
        ), patch(
            "kiro_claw.mcp_core._post",
            return_value={"slug": "x", "version": 1},
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>x</div>"},
            )
        assert "Possible duplicate" not in result
        assert "Saved artifact" in result


class TestArtifactGet:
    def test_get_current(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={
                "slug": "x",
                "name": "X",
                "kind": "widget",
                "version": 3,
                "updated_at": "2026-05-21T16:00:00.000000+00:00",
                "tags": ["foo"],
                "description": "desc",
                "content": "<p>hello</p>",
            },
        ) as get:
            result = _call_tool_inner("artifact_get", {"slug": "x"})
        assert get.call_args.args[0] == "/api/artifacts/x"
        assert "slug: x" in result
        assert "version: 3" in result
        assert "<p>hello</p>" in result

    def test_get_specific_version(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "x", "name": "X", "version": 1, "content": "v1"},
        ) as get:
            _call_tool_inner("artifact_get", {"slug": "x", "version": 1})
        assert get.call_args.args[0] == "/api/artifacts/x/versions/1"

    def test_redacts_credentials_in_content(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={
                "slug": "x",
                "name": "X",
                "version": 1,
                "content": "<p>AKIAIOSFODNN7EXAMPLE</p>",
            },
        ):
            result = _call_tool_inner("artifact_get", {"slug": "x"})
        # Raw credential pattern must not appear.
        assert "AKIAIOSFODNN7EXAMPLE" not in result


class TestArtifactList:
    def test_no_filter(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={
                "artifacts": [
                    {
                        "slug": "a",
                        "name": "A",
                        "kind": "widget",
                        "version": 2,
                        "tags": ["t1"],
                    },
                    {
                        "slug": "b",
                        "name": "B",
                        "kind": "markdown",
                        "version": 1,
                        "tags": [],
                    },
                ]
            },
        ) as get:
            result = _call_tool_inner("artifact_list", {})
        assert get.call_args.args[0] == "/api/artifacts"
        assert "a  v2  widget  [t1]  A" in result
        assert "b  v1  markdown  B" in result

    def test_with_filter(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"artifacts": []},
        ) as get:
            _call_tool_inner("artifact_list", {"tag": "ops", "kind": "widget"})
        url = get.call_args.args[0]
        assert "tag=ops" in url
        assert "kind=widget" in url

    def test_empty(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get", return_value={"artifacts": []}
        ):
            result = _call_tool_inner("artifact_list", {})
        assert result == "No artifacts saved."


class TestArtifactVersions:
    def test_versions(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "x", "versions": [1, 2, 5]},
        ):
            result = _call_tool_inner("artifact_versions", {"slug": "x"})
        assert "v1" in result
        assert "v2" in result
        assert "v5" in result

    def test_no_versions(self) -> None:
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "x", "versions": []},
        ):
            result = _call_tool_inner("artifact_versions", {"slug": "x"})
        assert "No versions" in result


class TestArtifactDelete:
    def test_delete(self) -> None:
        with patch(
            "kiro_claw.mcp_core._delete", return_value={"ok": True}
        ) as delete:
            result = _call_tool_inner("artifact_delete", {"slug": "x"})
        assert delete.call_args.args[0] == "/api/artifacts/x"
        assert "Deleted artifact: x" in result

    def test_delete_error(self) -> None:
        with patch(
            "kiro_claw.mcp_core._delete", return_value={"error": "not found"}
        ):
            result = _call_tool_inner("artifact_delete", {"slug": "x"})
        assert "Error: not found" in result


class TestArtifactUpdate:
    def test_no_op_update_rejected(self) -> None:
        # No content/name/description/tags → error message.
        result = _call_tool_inner("artifact_update", {"slug": "x"})
        assert "nothing to update" in result.lower()


class TestArtifactRevert:
    def test_revert_fetches_target_then_patches_with_reverted_event(self) -> None:
        # The revert tool should: (1) GET the target version's content, then
        # (2) PATCH the artifact with that content, event_type='reverted',
        # and from_version pinned to the target. The handler enforces
        # snapshot=True for reverted updates so the revert always lands as
        # a new version on the timeline.
        target_content = "# v2 content"
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "doc", "version": 2, "content": target_content},
        ) as get_mock, patch(
            "kiro_claw.mcp_core.urllib.request.urlopen"
        ) as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4}'
            )
            result = _call_tool_inner(
                "artifact_revert", {"slug": "doc", "target_version": 2}
            )
        # Step 1: GET the target version
        get_mock.assert_called_once_with("/api/artifacts/doc/versions/2")
        # Step 2: PATCH with reverted event metadata
        req = urlopen_mock.call_args.args[0]
        assert req.method == "PATCH"
        assert req.full_url.endswith("/api/artifacts/doc")
        body = json.loads(req.data)
        assert body["content"] == target_content
        assert body["event_type"] == "reverted"
        assert body["from_version"] == 2
        # Result is human-readable summary.
        assert "Reverted doc to v2" in result
        assert "Live state is now v4" in result

    def test_revert_propagates_get_error(self) -> None:
        # If the target version doesn't exist, the GET fails and we report it.
        with patch(
            "kiro_claw.mcp_core._get", return_value={"error": "version not found"}
        ):
            result = _call_tool_inner(
                "artifact_revert", {"slug": "doc", "target_version": 99}
            )
        assert "cannot fetch version 99" in result

    def test_revert_validates_target_version(self) -> None:
        # Schema enforces target_version is a positive integer.
        from kiro_claw.mcp_core import _call_tool

        result = _call_tool(
            "artifact_revert", {"slug": "doc", "target_version": 0}
        )
        assert "error" in result.lower() or "target_version" in result.lower()

    def test_revert_surfaces_source_path_for_diff_headers(self) -> None:
        # When the artifact is file-backed, the response should include
        # source_path so the calling agent can build a unified-diff header
        # that activates the dashboard's Open file affordance.
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "doc", "version": 2, "content": "v2"},
        ), patch(
            "kiro_claw.mcp_core.urllib.request.urlopen"
        ) as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4, "source_path": "/home/u/notes/doc.md"}'
            )
            result = _call_tool_inner(
                "artifact_revert", {"slug": "doc", "target_version": 2}
            )
        assert "source_path: /home/u/notes/doc.md" in result
        # Guidance for emitting a proper diff is included so the agent
        # surfaces an operable Open file button to the user.
        assert (
            "--- /home/u/notes/doc.md" in result
            and "+++ /home/u/notes/doc.md" in result
        )

    def test_revert_no_source_path_omits_diff_guidance(self) -> None:
        # Chat-backed artifacts (no source_path) shouldn't get the diff
        # guidance — there's no file to open in the side panel.
        with patch(
            "kiro_claw.mcp_core._get",
            return_value={"slug": "doc", "version": 2, "content": "v2"},
        ), patch(
            "kiro_claw.mcp_core.urllib.request.urlopen"
        ) as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4}'  # no source_path
            )
            result = _call_tool_inner(
                "artifact_revert", {"slug": "doc", "target_version": 2}
            )
        assert "source_path" not in result
        assert "--- " not in result


class TestSchemas:
    """Confirm validation rejects bad inputs at the dispatcher."""

    def test_save_rejects_missing_required(self) -> None:
        from kiro_claw.mcp_core import _call_tool

        # Missing 'content' → validation error
        result = _call_tool("artifact_save", {"name": "x"})
        assert "content" in result.lower() or "error" in result.lower()

    def test_save_rejects_invalid_slug(self) -> None:
        from kiro_claw.mcp_core import _call_tool

        result = _call_tool(
            "artifact_save", {"name": "x", "content": "a", "slug": "Has Spaces"}
        )
        assert "error" in result.lower() or "invalid" in result.lower()

    def test_get_rejects_invalid_slug(self) -> None:
        from kiro_claw.mcp_core import _call_tool

        result = _call_tool("artifact_get", {"slug": "BAD/PATH"})
        assert "error" in result.lower() or "invalid" in result.lower()
