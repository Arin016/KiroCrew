"""Live browse screencast — relay screenshots the agent already takes to the dashboard.

The headless ``[BROWSE]`` Chromium runs on the gateway host; the only window onto
it from a laptop is the dashboard (reachable over the reverse SSH tunnel). This
module gives the dashboard a near-real-time mirror **without opening any debug
port on the browser**.

Design (why this shape):
- The Playwright MCP proxy already intercepts every ``browser_take_screenshot``
  response and re-encodes it to JPEG (``mcp_playwright_proxy._save_screenshot``).
  It additionally POSTs that already-captured frame to the gateway's loopback
  ``/api/browser/frame`` ingress, which rebroadcasts it over the existing WS as a
  ``browser_frame`` event. The ``BrowserLiveView`` panel renders the latest frame.
- This rides Playwright's existing (authenticated, pipe-based) control channel —
  it does **not** add a ``--remote-debugging-port``. An earlier revision attached
  to a CDP debug port for smoother frames; that port was an unauthenticated,
  full-control endpoint on an auth-cookie-bearing browser (a net-new
  local-process-takeover surface), so it was dropped in favour of this design.
- Cadence is sparse — frames arrive only when the agent itself screenshots. A
  follow-up (an active pump) can inject idle-gated screenshots for a
  steady ~1-2 fps if needed; the WS contract here is unchanged by that.

This module is intentionally tiny: the gateway owns no browser connection, only
the WS rebroadcast. ``build_frame_payload`` is a pure helper so the framing
contract is unit-testable without a live browser or proxy.
"""

from __future__ import annotations

import re
from typing import Any

# WS event name the dashboard BrowserLiveView panel listens for.
BROWSER_FRAME_EVENT = "browser_frame"

# Raster formats only. The dashboard renders frames as ``<img src="data:image/
# {format};base64,...">``; "svg" (image/svg+xml) is deliberately excluded because
# an SVG data URI can carry executable script — this allowlist is the load-bearing
# control that keeps the render XSS-safe, so do NOT add "svg" here.
_ALLOWED_FORMATS = {"jpeg", "png", "webp"}

# Standard base64 charset (+ optional padding). ``data`` must match this exactly:
# it structurally excludes ``:`` (so no ``://`` URL), whitespace, and ``<``/``>``
# (so no HTML/script), which is the right boundary control for an image field —
# far better than running text credential/URL redactors on opaque image bytes.
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")

# Slot key the frame belongs to (from the proxy's KIROCREW_SESSION_KEY). Opaque
# id used only as a lookup key client-side — the dashboard renders the resolved
# session *title* from its own slot store, never this raw value — but bound it to
# a safe charset/length anyway so the WS payload can't carry arbitrary text.
_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def valid_session_key(value: Any) -> bool:
    """True if ``value`` is a session key safe to use as a routing/lookup key."""
    return isinstance(value, str) and _SESSION_KEY_RE.fullmatch(value) is not None


def build_frame_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a POSTed frame body into the ``browser_frame`` WS payload.

    ``body`` is the JSON the proxy POSTs to ``/api/browser/frame``:
    ``{"data": "<base64>", "format": "jpeg", "device_width"?, "device_height"?}``.

    Returns the payload dict the dashboard renders, or ``None`` if the body has
    no usable image data (caller should reject with 400). ``data`` is validated
    to the base64 charset at this boundary; no text redaction is applied because
    the field is browser-captured image bytes (not LLM output), and the charset
    check structurally rules out URLs/credentials anyway — unlike
    ``/api/browser-event`` which forwards free-text fields.
    """
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return None
    if not _B64_RE.fullmatch(data):
        return None
    fmt = body.get("format")
    if fmt not in _ALLOWED_FORMATS:
        fmt = "jpeg"
    payload: dict[str, Any] = {"data": data, "format": fmt}
    for dim in ("device_width", "device_height"):
        val = body.get(dim)
        # bool is an int subclass, so {"device_width": true} would pass a bare
        # isinstance(int) check and broadcast device_width=True — which the
        # BrowserLiveView panel treats as 1 in JS aspect/size math (a 1px frame
        # hint). Also bound it to a sane pixel range, matching the module's
        # bound-every-field idiom (format allowlist, _B64_RE, _SESSION_KEY_RE).
        if isinstance(val, int) and not isinstance(val, bool) and 0 < val <= 100_000:
            payload[dim] = val
    # Pass the session key through (bounded) so the panel can label which session
    # it mirrors. The dashboard resolves it to a title from its own slot store.
    sk = body.get("session_key")
    if isinstance(sk, str) and _SESSION_KEY_RE.fullmatch(sk):
        payload["session_key"] = sk
    return payload


# ── Input (dashboard → browser) ────────────────────────────────────────────────
#
# The return leg of the mirror. The panel posts a user gesture; the proxy turns it
# into a Playwright tool call over the SAME authenticated pipe the frames come
# back on. The load-bearing control is this CLOSED VERB ENUM: the wire never
# carries a tool name, so the endpoint cannot be used to reach a tool the caller
# was not granted. That matters specifically because ``browser_evaluate`` requires
# interactive confirmation (cookie-exfiltration risk) — a pass-through tool name
# here would launder it around that gate.
#
# Coordinates are NORMALIZED (0..1 fractions of the frame), never pixels. The
# panel cannot know the browser's live viewport without racing a resize, so the
# proxy multiplies by the capture size it learned from the most recent frame.

_INPUT_VERBS = {"click", "move", "wheel", "drag", "key", "resize"}
_MOUSE_BUTTONS = {"left", "right", "middle"}

# Playwright key names we accept for the ``key`` verb: one printable character, or
# one of these named keys. An allowlist rather than free text so the panel cannot
# post an arbitrary string into a tool argument.
_NAMED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Insert",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Home", "End", "PageUp", "PageDown", "Space",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
}
# Modifier-prefixed combos, e.g. "Control+a" / "Shift+Tab" / "Meta+v".
_MODIFIERS = {"Control", "Shift", "Alt", "Meta"}

# Wheel deltas are CSS pixels (not fractions) — that is the unit the DOM reports
# and Playwright consumes. Bounded so one gesture cannot request an absurd scroll.
_MAX_WHEEL_DELTA = 10_000
# Viewport bounds for the resize verb. The lower bound keeps a collapsing panel
# from driving the page to a degenerate width mid-animation.
_MIN_VIEWPORT = 200
_MAX_VIEWPORT = 10_000


def _norm_fraction(val: Any) -> float | None:
    """A coordinate as a 0..1 fraction of the frame, or None if out of range."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    num = float(val)
    if num != num or num < 0.0 or num > 1.0:  # NaN-safe
        return None
    return num


def _valid_key(val: Any) -> str | None:
    """Validate a Playwright key name against the allowlist."""
    if not isinstance(val, str) or not val or len(val) > 32:
        return None
    base = val
    if "+" in val:
        *mods, base = val.split("+")
        if not mods or any(m not in _MODIFIERS for m in mods):
            return None
    if base in _NAMED_KEYS:
        return val
    # A single printable character (letters, digits, punctuation), no controls.
    if len(base) == 1 and base.isprintable():
        return val
    return None


def build_input_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a POSTed input event, or return ``None`` to reject with 400.

    Every accepted shape is enumerated here; anything else is rejected. The
    returned dict is what the proxy drains, so it is already fully validated by
    the time it reaches the injection site.
    """
    if not isinstance(body, dict):
        return None
    verb = body.get("verb")
    if verb not in _INPUT_VERBS:
        return None

    if verb in ("click", "move"):
        x, y = _norm_fraction(body.get("x")), _norm_fraction(body.get("y"))
        if x is None or y is None:
            return None
        out: dict[str, Any] = {"verb": verb, "x": x, "y": y}
        if verb == "click":
            button = body.get("button", "left")
            if button not in _MOUSE_BUTTONS:
                return None
            count = body.get("clickCount", 1)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 3:
                return None
            out["button"] = button
            out["clickCount"] = count
        return out

    if verb == "drag":
        x, y = _norm_fraction(body.get("x")), _norm_fraction(body.get("y"))
        x2, y2 = _norm_fraction(body.get("x2")), _norm_fraction(body.get("y2"))
        if None in (x, y, x2, y2):
            return None
        return {"verb": "drag", "x": x, "y": y, "x2": x2, "y2": y2}

    if verb == "wheel":
        deltas: dict[str, Any] = {"verb": "wheel"}
        for name in ("dx", "dy"):
            val = body.get(name, 0)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return None
            num = float(val)
            if num != num or abs(num) > _MAX_WHEEL_DELTA:
                return None
            deltas[name] = num
        return deltas

    if verb == "key":
        key = _valid_key(body.get("key"))
        if key is None:
            return None
        return {"verb": "key", "key": key}

    # resize
    width, height = body.get("width"), body.get("height")
    for val in (width, height):
        if isinstance(val, bool) or not isinstance(val, int):
            return None
        if not _MIN_VIEWPORT <= val <= _MAX_VIEWPORT:
            return None
    return {"verb": "resize", "width": width, "height": height}
