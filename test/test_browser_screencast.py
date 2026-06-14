"""Unit tests for the B-minus live-mirror path.

No real browser, CDP port, or gateway is needed: ``build_frame_payload`` is a
pure normalizer and the proxy frame helpers are exercised directly.
"""

from __future__ import annotations

import base64
import os

from kiro_claw.browser.screencast import build_frame_payload


class TestBuildFramePayload:
    def test_valid_jpeg_frame(self):
        out = build_frame_payload({"data": "QUJD", "format": "jpeg"})
        assert out == {"data": "QUJD", "format": "jpeg"}

    def test_missing_data_is_rejected(self):
        assert build_frame_payload({}) is None
        assert build_frame_payload({"data": ""}) is None
        assert build_frame_payload({"data": 123}) is None

    def test_non_base64_data_is_rejected(self):
        # Charset validation rules out anything that isn't standard base64 —
        # so a URL, whitespace, or HTML can never reach the dashboard via this field.
        assert build_frame_payload({"data": "http://evil.example/x"}) is None  # ':' and '.'
        assert build_frame_payload({"data": "QUJD QUJD"}) is None  # whitespace
        assert build_frame_payload({"data": "<svg onload=alert(1)>"}) is None  # html
        assert build_frame_payload({"data": "QU=JD"}) is None  # padding mid-string
        # valid base64 (with padding) still passes
        assert build_frame_payload({"data": "QUJDRA==", "format": "png"}) == {
            "data": "QUJDRA==",
            "format": "png",
        }

    def test_unknown_format_defaults_to_jpeg(self):
        out = build_frame_payload({"data": "QUJD", "format": "tiff"})
        assert out is not None and out["format"] == "jpeg"

    def test_passes_through_integer_dimensions_only(self):
        out = build_frame_payload(
            {"data": "QUJD", "format": "png", "device_width": 1280, "device_height": "tall"}
        )
        assert out == {"data": "QUJD", "format": "png", "device_width": 1280}


class TestProxyFrameHelpers:
    def test_post_frame_to_gateway_never_raises_when_gateway_down(self, monkeypatch):
        # Point at a port nothing is listening on; the threaded POST must swallow
        # the connection error so the agent's screenshot is never affected.
        import kiro_claw.mcp_playwright_proxy as proxy

        monkeypatch.setenv("KIROCLAW_PORT", "1")  # unroutable
        # Must return immediately (spawns a daemon thread) and not raise.
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")

    def test_post_frame_suppressed_in_extension_mode(self, monkeypatch):
        # Extension mode attaches to the user's own visible Chrome — the mirror is
        # redundant, so no frame POST should be spawned at all.
        import kiro_claw.mcp_playwright_proxy as proxy

        spawned = []

        class _FakeThread:
            def __init__(self, *a, **k):
                spawned.append(k.get("target"))

            def start(self):
                spawned.append("start")

        monkeypatch.setattr(proxy.threading, "Thread", _FakeThread)

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", True)
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")
        assert spawned == []  # no POST thread in extension mode

        monkeypatch.setattr(proxy, "_EXTENSION_MODE", False)
        proxy._post_frame_to_gateway(b"\xff\xd8\xff", "jpeg")
        assert spawned  # headless mode still mirrors

    def test_prune_keeps_newest(self, monkeypatch, tmp_path):
        import kiro_claw.mcp_playwright_proxy as proxy

        d = tmp_path / "shots"
        d.mkdir()
        monkeypatch.setattr(proxy, "_SCREENSHOT_DIR", str(d))
        monkeypatch.setattr(proxy, "_SCREENSHOT_KEEP", 3)
        for i in range(6):
            p = d / f"screenshot-{i}.jpeg"
            p.write_bytes(b"x")
            os.utime(p, (i, i))  # ascending mtime
        proxy._prune_screenshot_dir()
        remaining = sorted(os.listdir(d))
        assert remaining == ["screenshot-3.jpeg", "screenshot-4.jpeg", "screenshot-5.jpeg"]

    def test_encode_frame_returns_bytes_and_ext(self):
        import kiro_claw.mcp_playwright_proxy as proxy

        # 1x1 transparent PNG.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMCAQAB"
            "GqQ4QAAAAABJRU5ErkJggg=="
        )
        img_bytes, ext = proxy._encode_frame(png_b64, "image/png")
        assert isinstance(img_bytes, bytes) and img_bytes
        # With PIL present it re-encodes to JPEG; without, it stays png.
        assert ext in ("jpeg", "png")
        # Round-trips as valid base64 input.
        assert base64.b64decode(png_b64)

    def test_internal_secret_read_from_kiroclaw_home(self, monkeypatch, tmp_path):
        import kiro_claw.mcp_playwright_proxy as proxy

        monkeypatch.setenv("KIROCLAW_HOME", str(tmp_path))
        assert proxy._internal_secret() == ""  # absent file -> empty (POST then dropped)
        (tmp_path / ".local_secret").write_text("s3cr3t\n")
        assert proxy._internal_secret() == "s3cr3t"  # stripped
