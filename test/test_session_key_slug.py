"""Mesh-2435: slot keys must be ASCII so the derived session key is header-safe.

A dashboard session key is ``dashboard:{slot.key}`` and is sent to the gateway
as the ``X-Session-Key`` request header. HTTP header values are latin-1
(RFC 7230), so a slot key containing a character outside latin-1 (e.g. an
em-dash U+2014 from a title-derived slot name) would make ``http.client`` raise
``UnicodeEncodeError`` and abort the tool call.

The fix is at the source: ``get_or_create_slot`` slugs any non-ASCII slot name
to ASCII *before* the lookup/create, so the stored slot key — and therefore the
injected session key — is always header-safe, and create + repeat calls with the
same raw name converge on the one slot. (The transport-layer guard that rejects a
genuinely non-latin-1 session key is ``mcp_core._session_key_header_error``,
landed under Mesh-2241 and covered by ``test_mcp_core.py``.)
"""

from __future__ import annotations

from chat_test_helpers import _make_state

from kiro_claw.dashboard.state import _ascii_slot_key

EM_DASH = "\u2014"


class TestAsciiSlotKey:
    def test_ascii_unchanged(self):
        assert _ascii_slot_key("plain-ascii_1.2") == "plain-ascii_1.2"

    def test_non_ascii_replaced(self):
        assert _ascii_slot_key(f"a{EM_DASH}b") == "a-b"

    def test_control_chars_replaced(self):
        # CR/LF are ASCII, so they slip past an isascii() check, but in a slot
        # key they flow into the X-Session-Key header and enable header
        # injection/splitting. They must be replaced (Mesh-2435 hardening).
        out = _ascii_slot_key("a\r\nX-Evil: 1")
        assert "\r" not in out and "\n" not in out
        assert out == "a--X-Evil: 1"

    def test_idempotent(self):
        once = _ascii_slot_key(f"Plan {EM_DASH} v2")
        assert _ascii_slot_key(once) == once
        once.encode("latin-1")  # the slugged key is header-safe


class TestGetOrCreateSlotSlugging:
    def test_non_ascii_name_is_slugged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        assert slot.key.isascii()
        assert EM_DASH not in slot.key
        # the resulting session key is header-safe
        f"dashboard:{slot.key}".encode("latin-1")

    def test_repeat_call_resolves_same_slot(self, tmp_path):
        """Slugging runs before the lookup, so the same name matches the slot."""
        state = _make_state(tmp_path)
        first = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        again = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        assert again is first

    def test_ascii_name_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("my-session")
        assert slot.key == "my-session"

    def test_auto_generated_key_is_ascii(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot()
        assert slot.key.isascii()
        assert slot.key.startswith("chat-")
