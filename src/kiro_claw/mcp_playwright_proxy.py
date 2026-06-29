"""Playwright MCP Proxy — compresses accessibility tree responses.

Sits between the agent backend and the real Playwright MCP server,
intercepting responses that contain large accessibility trees and
compressing them to compact outlines with element refs (~95% token
reduction).

Runs as ``kiroclaw mcp-playwright-proxy [playwright-args...]``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_KEEP_PATTERN = re.compile(
    r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
    r"|img|image|navigation|main|banner|contentinfo|search|alert"
    r"|dialog|listitem|row|cell|ref=)",
    re.IGNORECASE,
)

_TREE_INDICATOR = re.compile(r"^\s*-\s+(link|button|heading|navigation|main|textbox|img)\b")

_MAX_OUTLINE_LINES = 150


def _is_accessibility_tree(text: str) -> bool:
    """Heuristic: does this text look like a Playwright accessibility snapshot?"""
    lines = text.split("\n", 20)
    tree_lines = sum(1 for line in lines if _TREE_INDICATOR.match(line))
    return tree_lines >= 3


def _compress_to_outline(text: str) -> str:
    """Compress accessibility tree to compact outline with refs."""
    lines = text.split("\n")
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if _KEEP_PATTERN.search(stripped):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= _MAX_OUTLINE_LINES:
                outline.append(f"... (truncated at {_MAX_OUTLINE_LINES} lines)")
                break

    if not outline:
        return text

    total = len([ln for ln in lines if ln.strip()])
    header = f"[Compressed: {total} elements → {len(outline)} interactive]\n"
    return header + "\n".join(outline)


_SCREENSHOT_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "kiroclaw-screenshots")


def _encode_frame(data: str, media_type: str) -> tuple[bytes, str]:
    """Decode a base64 image; downscale + JPEG-encode if PIL is available.

    Returns ``(bytes, ext)``. Shared by the on-disk save and the live-frame POST
    so the (relatively expensive) decode/resize/encode runs once per screenshot.
    """
    img_bytes = base64.b64decode(data)
    ext = "jpeg" if ("jpeg" in media_type or "jpg" in media_type) else "png"
    if _HAS_PIL:
        try:
            img = Image.open(io.BytesIO(img_bytes))
            max_width = 1200
            if img.width > max_width:
                ratio = max_width / img.width
                resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", None))
                img = img.resize((max_width, int(img.height * ratio)), resample)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return buf.getvalue(), "jpeg"
        except Exception:
            pass
    return img_bytes, ext


_SCREENSHOT_KEEP = 200


def _prune_screenshot_dir() -> None:
    """Keep at most ``_SCREENSHOT_KEEP`` newest screenshots; best-effort.

    The dir grows one file per agent screenshot and was previously never pruned
    (a latent unbounded-disk leak). Ring-trim the oldest on each save.
    """
    try:
        entries = [
            os.path.join(_SCREENSHOT_DIR, f) for f in os.listdir(_SCREENSHOT_DIR)
        ]
        if len(entries) <= _SCREENSHOT_KEEP:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        for stale in entries[: len(entries) - _SCREENSHOT_KEEP]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except OSError:
        pass


def _write_screenshot(img_bytes: bytes, ext: str) -> str:
    """Write pre-encoded image bytes to the screenshot dir; prune; return path."""
    os.makedirs(_SCREENSHOT_DIR, mode=0o700, exist_ok=True)
    ts = int(time.time() * 1000)
    filepath = os.path.join(_SCREENSHOT_DIR, f"screenshot-{ts}.{ext}")
    with open(filepath, "wb") as f:
        f.write(img_bytes)
    _prune_screenshot_dir()
    return filepath


def _gateway_frame_url() -> str:
    """Loopback gateway endpoint that rebroadcasts a browse frame to the dashboard."""
    port = os.environ.get("KIROCLAW_PORT", "5476")
    return f"http://127.0.0.1:{port}/api/browser/frame"


def _internal_secret() -> str:
    """Read the per-session IPC secret the gateway requires on internal paths.

    Same source as ``mcp_core._internal_secret`` (``$KIROCLAW_HOME/.local_secret``,
    default ``~/.kiroclaw``). Read directly to avoid importing the gateway into
    this stdio proxy. Returns "" if unreadable — the POST then fails the gate and
    is silently dropped (frames are best-effort).
    """
    home = os.environ.get("KIROCLAW_HOME", os.path.expanduser("~/.kiroclaw"))
    try:
        with open(os.path.join(home, ".local_secret"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# Extension mode attaches to the user's own (visible) Chrome rather than launching
# a headless browser, so the live mirror is redundant — they already see the real
# window. setup.py passes ``--extension`` to this proxy in that mode, so we read it
# from our own argv and suppress the frame POST (the screenshot is still saved to
# disk for the agent's Read tool; only the dashboard mirror is skipped).
_EXTENSION_MODE = "--extension" in sys.argv


def _post_frame_to_gateway(img_bytes: bytes, fmt: str) -> None:
    """Best-effort POST of a browse frame to the gateway for the live dashboard mirror.

    Runs on a daemon thread so it never blocks the JSON-RPC relay (a synchronous
    POST in the relay loop would add latency to the agent's own screenshot call).
    Swallows every error: frames are non-critical, and the gateway may be down,
    on a different port, or unreachable — the agent's screenshot must not depend
    on the mirror succeeding. The ``/api/browser/frame`` ingress is a loopback +
    internal-secret path, so we send the same ``X-Internal-Secret`` header the
    other MCP-side callers use.

    No-op in extension mode: the user is watching their own Chrome, so mirroring a
    sparse, downscaled copy to the dashboard adds load with no benefit.
    """
    if _EXTENSION_MODE:
        return

    def _send() -> None:
        try:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            body = json.dumps({"data": b64, "format": fmt}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            secret = _internal_secret()
            if secret:
                headers["X-Internal-Secret"] = secret
            req = urllib.request.Request(
                _gateway_frame_url(),
                data=body,
                headers=headers,
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2).close()
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _save_screenshot(data: str, media_type: str) -> str:
    """Encode, persist, and mirror a screenshot. Returns the on-disk path.

    Encodes once, writes the file (for the agent's Read tool), and fires a
    best-effort live-frame POST to the gateway (for the dashboard mirror).
    """
    img_bytes, ext = _encode_frame(data, media_type)
    filepath = _write_screenshot(img_bytes, ext)
    _post_frame_to_gateway(img_bytes, ext)
    return filepath


def _maybe_compress_response(msg: dict[str, Any]) -> dict[str, Any]:
    """Compress accessibility trees and save screenshots to files."""
    result = msg.get("result")
    if not isinstance(result, dict):
        return msg
    content = result.get("content")
    if not isinstance(content, list):
        return msg
    new_content = []
    for item in content:
        if not isinstance(item, dict):
            new_content.append(item)
            continue
        if item.get("type") == "image":
            data = item.get("data", "")
            media_type = item.get("mimeType", "image/png")
            if data:
                filepath = _save_screenshot(data, media_type)
                new_content.append({
                    "type": "text",
                    "text": f"Screenshot saved: {filepath}\nUse Read tool to view it if needed.",
                })
            else:
                new_content.append(item)
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > 5000 and _is_accessibility_tree(text):
                item["text"] = _compress_to_outline(text)
        new_content.append(item)
    result["content"] = new_content
    return msg


def _read_message(stream) -> dict[str, Any] | None:
    """Read one JSON-RPC message from a binary stream."""
    while True:
        line = stream.readline()
        if not line:
            return None
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        if line_str.lower().startswith("content-length:"):
            try:
                length = int(line_str.split(":", 1)[1].strip())
                while True:
                    sep = stream.readline()
                    if sep.strip() == b"":
                        break
                body = stream.read(length)
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    return parsed
                continue
            except (ValueError, json.JSONDecodeError):
                continue
        try:
            parsed = json.loads(line_str)
            if isinstance(parsed, dict):
                return parsed
            continue
        except json.JSONDecodeError:
            continue


_client_uses_content_length: bool | None = None


def _read_message_from_client(stream) -> dict[str, Any] | None:
    """Read from client (kiro-cli/probe), detecting framing style."""
    global _client_uses_content_length
    while True:
        line = stream.readline()
        if not line:
            return None
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        if line_str.lower().startswith("content-length:"):
            _client_uses_content_length = True
            try:
                length = int(line_str.split(":", 1)[1].strip())
                while True:
                    sep = stream.readline()
                    if sep.strip() == b"":
                        break
                body = stream.read(length)
                return json.loads(body.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
        try:
            if _client_uses_content_length is None:
                _client_uses_content_length = False
            return json.loads(line_str)
        except json.JSONDecodeError:
            continue


def _write_message(stream, msg: dict[str, Any]) -> None:
    """Write a JSON-RPC message, mirroring the client's framing style."""
    body = json.dumps(msg).encode("utf-8")
    if _client_uses_content_length:
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        stream.write(header + body)
    else:
        stream.write(body + b"\n")
    stream.flush()


def _write_message_to_subprocess(stream, msg: dict[str, Any]) -> None:
    """Write to the Playwright MCP subprocess — bare JSON lines (Node expects this)."""
    body = json.dumps(msg).encode("utf-8")
    stream.write(body + b"\n")
    stream.flush()


_PENDING_REQUESTS: dict[Any, dict[str, Any]] = {}


def _forward_stdin_to_subprocess_tracked(client_stdin, proc_stdin) -> None:
    """Forward client→subprocess, tracking in-flight IDs to synthesize errors if subprocess dies."""
    while True:
        msg = _read_message_from_client(client_stdin)
        if msg is None:
            proc_stdin.close()
            break
        req_id = msg.get("id")
        if req_id is not None:
            _PENDING_REQUESTS[req_id] = msg
        _write_message_to_subprocess(proc_stdin, msg)


def _drain_pending_with_error() -> None:
    """Send error responses for all pending requests when subprocess dies."""
    extension_mode = "--extension" in sys.argv
    if extension_mode:
        hint = (
            "Playwright MCP connection closed. Chrome may not be running or "
            "the Playwright extension is not active. Open Chrome and verify "
            "the extension icon shows the correct token."
        )
    else:
        hint = "Playwright MCP subprocess exited unexpectedly."

    for req_id in list(_PENDING_REQUESTS.keys()):
        error_resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": hint},
        }
        _write_message(sys.stdout.buffer, error_resp)
    _PENDING_REQUESTS.clear()


def _resolve_playwright_cmd() -> str | None:
    """Find the public ``@playwright/mcp`` CLI, resolving via PATH/npx.

    Resolution order:
      1. ``KIROCLAW_PLAYWRIGHT_CMD`` override (explicit path/command).
      2. A ``mcp-server-playwright``/``playwright-mcp`` binary on PATH.
      3. ``npx`` — the public ``@playwright/mcp`` package is launched via
         ``npx @playwright/mcp`` when no standalone binary is installed.

    Returns ``None`` when no launcher is resolvable (e.g. Node/npm absent),
    so callers can fail gracefully rather than spawning a missing binary.
    """
    override = os.environ.get("KIROCLAW_PLAYWRIGHT_CMD")
    if override:
        return override
    for binary in ("mcp-server-playwright", "playwright-mcp"):
        found = shutil.which(binary)
        if found:
            return found
    if shutil.which("npx"):
        return "npx"
    return None


def run_proxy(args: list[str]) -> None:
    """Main proxy loop."""
    playwright_cmd = _resolve_playwright_cmd()
    if playwright_cmd is None:
        error_resp = {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {
                "code": -32000,
                "message": (
                    "Playwright MCP not available: install the public "
                    "@playwright/mcp package (e.g. `npx @playwright/mcp`) "
                    "or set KIROCLAW_PLAYWRIGHT_CMD."
                ),
            },
        }
        _write_message(sys.stdout.buffer, error_resp)
        sys.exit(1)
    if playwright_cmd.endswith(".js"):
        cmd = ["node", playwright_cmd] + args
    elif os.path.basename(playwright_cmd) == "npx":
        cmd = [playwright_cmd, "@playwright/mcp"] + args
    else:
        cmd = [playwright_cmd] + args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=os.environ,
        )
    except (OSError, FileNotFoundError) as exc:
        error_resp = {
            "jsonrpc": "2.0",
            "id": 0,
            "error": {"code": -32000, "message": f"Cannot start Playwright MCP: {exc}"},
        }
        _write_message(sys.stdout.buffer, error_resp)
        sys.exit(1)

    stdin_thread = threading.Thread(
        target=_forward_stdin_to_subprocess_tracked,
        args=(sys.stdin.buffer, proc.stdin),
        daemon=True,
    )
    stdin_thread.start()

    while True:
        msg = _read_message(proc.stdout)
        if msg is None:
            break
        req_id = msg.get("id")
        if req_id is None and "error" in msg:
            continue
        if req_id is not None:
            _PENDING_REQUESTS.pop(req_id, None)
        msg = _maybe_compress_response(msg)
        _write_message(sys.stdout.buffer, msg)

    _drain_pending_with_error()
    proc.wait()
    sys.exit(proc.returncode or 0)


def main() -> None:
    """Entry point for ``kiroclaw mcp-playwright-proxy``."""
    run_proxy(sys.argv[1:])


if __name__ == "__main__":
    main()
