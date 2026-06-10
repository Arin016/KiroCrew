"""Tests for forwarded-message attachment text recovery in slack.events."""

from __future__ import annotations

from kiro_claw.slack.events import _extract_shared_text


class TestExtractSharedText:
    def test_single_share_attachment_returns_text(self):
        event = {"text": "", "attachments": [{"is_share": True, "text": "forwarded body"}]}
        assert _extract_shared_text(event) == "forwarded body"

    def test_is_msg_unfurl_attachment_included(self):
        event = {"attachments": [{"is_msg_unfurl": True, "text": "shared msg"}]}
        assert _extract_shared_text(event) == "shared msg"

    def test_falls_back_to_fallback_field(self):
        event = {"attachments": [{"is_share": True, "fallback": "[10:00] Bob: hi"}]}
        assert _extract_shared_text(event) == "[10:00] Bob: hi"

    def test_multiple_shares_joined(self):
        event = {
            "attachments": [
                {"is_share": True, "text": "first"},
                {"is_share": True, "text": "second"},
            ]
        }
        assert _extract_shared_text(event) == "first\n\nsecond"

    def test_link_unfurl_excluded(self):
        # A pasted-URL unfurl is neither is_share nor is_msg_unfurl.
        event = {"attachments": [{"title": "Some Page", "text": "preview text"}]}
        assert _extract_shared_text(event) == ""

    def test_no_attachments_returns_empty(self):
        assert _extract_shared_text({"text": ""}) == ""

    def test_empty_share_parts_filtered(self):
        event = {
            "attachments": [
                {"is_share": True, "text": ""},
                {"is_share": True, "text": "kept"},
            ]
        }
        assert _extract_shared_text(event) == "kept"
