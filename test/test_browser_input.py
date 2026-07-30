"""Tests for the browse-mirror INPUT path (dashboard → browser).

The frame path is covered by ``test_browser_screencast.py``; this file covers the
return leg: the validator that bounds what can be asked for, the queue that holds
it, and the proxy translation/injection that carries it out.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.browser.input_queue import BrowserInputQueue
from kiro_crew.browser.screencast import build_input_payload, valid_session_key


class TestBuildInputPayload:
    """The closed-enum boundary. Everything the wire can express lives here."""

    @pytest.mark.parametrize(
        "event",
        [
            {"verb": "click", "x": 0.5, "y": 0.5},
            {"verb": "click", "x": 0.0, "y": 1.0, "button": "right", "clickCount": 2},
            {"verb": "move", "x": 0.25, "y": 0.75},
            {"verb": "drag", "x": 0.1, "y": 0.1, "x2": 0.9, "y2": 0.9},
            {"verb": "wheel", "dx": 0, "dy": -120},
            {"verb": "key", "key": "Enter"},
            {"verb": "key", "key": "Control+a"},
            {"verb": "key", "key": "x"},
            {"verb": "resize", "width": 800, "height": 600},
        ],
    )
    def test_accepts_valid_events(self, event):
        assert build_input_payload(event) is not None

    def test_rejects_a_tool_name_instead_of_a_verb(self):
        # The whole point of the closed enum: a caller must not be able to name a
        # tool. browser_evaluate requires interactive confirmation (cookie
        # exfiltration), so a pass-through tool name here would launder it around
        # that gate.
        assert build_input_payload({"tool": "browser_evaluate", "x": 0.5, "y": 0.5}) is None
        assert build_input_payload({"verb": "browser_evaluate"}) is None
        assert build_input_payload({"verb": "evaluate"}) is None

    @pytest.mark.parametrize(
        "event",
        [
            {"verb": "click", "x": 1.5, "y": 0.5},      # past the right edge
            {"verb": "click", "x": -0.01, "y": 0.5},    # negative
            {"verb": "click", "x": True, "y": 0.5},     # bool is an int subclass
            {"verb": "click", "x": float("nan"), "y": 0.5},
            {"verb": "click", "x": 0.5},                # missing y
            {"verb": "click", "x": 0.5, "y": 0.5, "button": "evil"},
            {"verb": "click", "x": 0.5, "y": 0.5, "clickCount": 99},
            {"verb": "drag", "x": 0.5, "y": 0.5},       # missing end point
            {"verb": "wheel", "dy": 10**9},             # absurd scroll
            {"verb": "wheel", "dy": "120"},             # string
            {"verb": "key", "key": "\x00"},             # control character
            {"verb": "key", "key": "Bogus+a"},          # bad modifier
            {"verb": "key", "key": "F1" * 40},          # over length
            {"verb": "resize", "width": 10, "height": 10},        # below floor
            {"verb": "resize", "width": 99999, "height": 600},    # above ceiling
            {"verb": "resize", "width": True, "height": 600},
            {},
        ],
    )
    def test_rejects_invalid_events(self, event):
        assert build_input_payload(event) is None

    def test_rejects_non_dict(self):
        assert build_input_payload("nope") is None  # type: ignore[arg-type]

    def test_normalizes_click_defaults(self):
        out = build_input_payload({"verb": "click", "x": 0.5, "y": 0.5})
        assert out == {"verb": "click", "x": 0.5, "y": 0.5, "button": "left", "clickCount": 1}


class TestValidSessionKey:
    def test_accepts_a_slot_key(self):
        assert valid_session_key("chat-70-1785264224")

    @pytest.mark.parametrize("bad", ["", "a b", "x" * 200, "../etc", None, 7])
    def test_rejects_unsafe(self, bad):
        assert not valid_session_key(bad)


class TestBrowserInputQueue:
    def test_push_and_drain_round_trip(self):
        async def run():
            q = BrowserInputQueue()
            q.push("s1", {"verb": "click"})
            return await q.drain("s1", timeout=0.05)

        assert asyncio.run(run()) == [{"verb": "click"}]

    def test_drain_is_scoped_per_session(self):
        """One session must never receive another's input."""

        async def run():
            q = BrowserInputQueue()
            q.push("s1", {"verb": "click"})
            return await q.drain("s2", timeout=0.05), await q.drain("s1", timeout=0.05)

        other, own = asyncio.run(run())
        assert other == []
        assert own == [{"verb": "click"}]

    def test_bounded_drops_oldest(self):
        """A gesture burst must not grow without limit; newest position wins."""

        async def run():
            q = BrowserInputQueue(maxlen=3)
            for i in range(10):
                q.push("s1", {"verb": "move", "i": i})
            return await q.drain("s1", timeout=0.05)

        got = asyncio.run(run())
        assert [e["i"] for e in got] == [7, 8, 9]

    def test_stale_events_expire_instead_of_firing_later(self):
        """Input queued while nobody drained must not fire minutes later."""

        async def run():
            q = BrowserInputQueue(ttl=0.0)
            q.push("s1", {"verb": "click"})
            await asyncio.sleep(0.01)
            return await q.drain("s1", timeout=0.01)

        assert asyncio.run(run()) == []

    def test_drain_waits_then_returns_on_push(self):
        """Long-poll: a click arriving mid-wait is delivered without another poll."""

        async def run():
            q = BrowserInputQueue()

            async def push_soon():
                await asyncio.sleep(0.02)
                q.push("s1", {"verb": "click"})

            task = asyncio.ensure_future(push_soon())
            got = await q.drain("s1", timeout=1.0)
            await task
            return got

        assert asyncio.run(run()) == [{"verb": "click"}]

    def test_drain_times_out_empty(self):
        async def run():
            q = BrowserInputQueue()
            return await q.drain("idle", timeout=0.02)

        assert asyncio.run(run()) == []

    def test_idle_session_bookkeeping_is_released(self):
        """One deque + one Event per session must not accumulate for the gateway's
        lifetime; an idle drain releases them."""

        async def run():
            q = BrowserInputQueue()
            q.push("s1", {"verb": "click"})
            await q.drain("s1", timeout=0.05)      # consumes the event
            tracked_before = q.tracked_sessions()
            await q.drain("s1", timeout=0.02)      # times out empty -> release
            return tracked_before, q.tracked_sessions()

        before, after = asyncio.run(run())
        assert before == 1
        assert after == 0

    def test_release_does_not_drop_a_racing_push(self):
        """The cleanup must not discard an event pushed just before it runs."""

        async def run():
            q = BrowserInputQueue()
            await q.drain("s1", timeout=0.01)   # creates then releases bookkeeping
            q.push("s1", {"verb": "click"})     # arrives after the release
            return await q.drain("s1", timeout=0.05)

        assert asyncio.run(run()) == [{"verb": "click"}]


class TestProxyViewportAuthority:
    """Which size a normalized coordinate is resolved against."""

    def test_requested_viewport_wins_over_frame_size(self):
        """@playwright/mcp may downscale the screenshot it returns, so the encoded
        frame can be smaller than the CSS viewport. Scaling by the frame size would
        bias every click toward the top-left, so the viewport we asked for wins."""
        import kiro_crew.mcp_playwright_proxy as proxy

        saved = (proxy._capture_size, proxy._requested_viewport)
        try:
            proxy._capture_size = None
            proxy._requested_viewport = None
            # A frame arrives already downscaled to 1568 wide.
            proxy._note_capture_size(1568, 882)
            assert proxy._current_capture_size() == (1568, 882)
            # We then drive the viewport to the panel's real size.
            proxy._input_event_to_call({"verb": "resize", "width": 2400, "height": 1350})
            assert proxy._current_capture_size() == (2400, 1350)
            # A click at the far right maps against 2400, not the 1568 frame.
            call = proxy._input_event_to_call({"verb": "click", "x": 1.0, "y": 0.0})
            assert call["arguments"]["x"] == 2399
        finally:
            proxy._capture_size, proxy._requested_viewport = saved

    def test_agent_issued_resize_also_updates_authority(self):
        """The panel is not the only resizer: kiro-cli can call browser_resize
        itself, and that request only passes through the relay. If only our own
        resizes updated the authority, panel clicks would keep mapping against a
        viewport that no longer exists."""
        import kiro_crew.mcp_playwright_proxy as proxy

        saved = (proxy._capture_size, proxy._requested_viewport)
        try:
            proxy._capture_size = None
            proxy._requested_viewport = None
            proxy._note_requested_viewport(2400, 1350)   # panel drove it
            proxy._note_agent_viewport_change({          # agent then drove it
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "browser_resize",
                    "arguments": {"width": 900, "height": 700},
                },
            })
            assert proxy._current_capture_size() == (900, 700)
        finally:
            proxy._capture_size, proxy._requested_viewport = saved

    def test_unrelated_forwarded_calls_do_not_touch_authority(self):
        import kiro_crew.mcp_playwright_proxy as proxy

        saved = (proxy._capture_size, proxy._requested_viewport)
        try:
            proxy._capture_size = None
            proxy._requested_viewport = (800, 600)
            for msg in (
                {"method": "initialize"},
                {"method": "tools/call", "params": {"name": "browser_navigate"}},
                {"method": "tools/call", "params": {
                    "name": "browser_resize",
                    "arguments": {"width": True, "height": 600},
                }},
                {"method": "tools/call", "params": {
                    "name": "browser_resize", "arguments": {"width": 0, "height": 0},
                }},
                {},
            ):
                proxy._note_agent_viewport_change(msg)
            assert proxy._current_capture_size() == (800, 600)
        finally:
            proxy._capture_size, proxy._requested_viewport = saved


class TestProxyInputTranslation:
    """Normalized fractions → Playwright pixel coordinates, done proxy-side."""

    def setup_method(self):
        import kiro_crew.mcp_playwright_proxy as proxy

        self.proxy = proxy
        self.proxy._note_capture_size(1000, 500)

    def test_click_scales_against_the_capture_size(self):
        call = self.proxy._input_event_to_call(
            {"verb": "click", "x": 0.5, "y": 0.5, "button": "left", "clickCount": 1}
        )
        assert call["name"] == "browser_mouse_click_xy"
        assert (call["arguments"]["x"], call["arguments"]["y"]) == (500, 250)

    def test_far_edge_stays_inside_the_viewport(self):
        """A fraction of exactly 1.0 must not address one pixel past the edge."""
        call = self.proxy._input_event_to_call({"verb": "move", "x": 1.0, "y": 1.0})
        assert (call["arguments"]["x"], call["arguments"]["y"]) == (999, 499)

    def test_drag_maps_both_endpoints(self):
        call = self.proxy._input_event_to_call(
            {"verb": "drag", "x": 0.0, "y": 0.0, "x2": 1.0, "y2": 0.5}
        )
        assert call["name"] == "browser_mouse_drag_xy"
        assert call["arguments"] == {"startX": 0, "startY": 0, "endX": 999, "endY": 250}

    def test_wheel_and_key_and_resize_need_no_capture_size(self):
        for event, tool in (
            ({"verb": "wheel", "dx": 0, "dy": -120}, "browser_mouse_wheel"),
            ({"verb": "key", "key": "Enter"}, "browser_press_key"),
            ({"verb": "resize", "width": 800, "height": 600}, "browser_resize"),
        ):
            assert self.proxy._input_event_to_call(event)["name"] == tool

    def test_pointer_event_is_dropped_before_the_first_frame(self):
        """With no frame captured yet there is nothing the user could have aimed at."""
        import kiro_crew.mcp_playwright_proxy as proxy

        saved = proxy._capture_size
        try:
            proxy._capture_size = None
            assert proxy._input_event_to_call({"verb": "click", "x": 0.5, "y": 0.5}) is None
            # …but a resize still works, so the viewport can be set up front.
            assert proxy._input_event_to_call(
                {"verb": "resize", "width": 800, "height": 600}
            ) is not None
        finally:
            proxy._capture_size = saved


class TestProxyInputIdIsolation:
    """The stream-corruption guard: injected ids must never reach the client."""

    def test_input_ids_are_recognised(self):
        import kiro_crew.mcp_playwright_proxy as proxy

        assert proxy._is_input_id("__mc_input_1")
        assert not proxy._is_input_id("__mc_pump_1")
        assert not proxy._is_input_id(7)
        assert not proxy._is_input_id(None)

    def test_input_and_pump_namespaces_do_not_overlap(self):
        """Each injected id needs its OWN prefix and its own demux arm; an id that
        matched both (or neither) would either double-handle or fall through to
        kiro-cli as a reply to a request it never sent."""
        import kiro_crew.mcp_playwright_proxy as proxy

        input_id = f"{proxy._INPUT_ID_PREFIX}1"
        pump_id = f"{proxy._PUMP_ID_PREFIX}1"
        assert proxy._is_input_id(input_id) and not proxy._is_pump_id(input_id)
        assert proxy._is_pump_id(pump_id) and not proxy._is_input_id(pump_id)

    def test_injection_marks_activity_so_frames_keep_flowing(self):
        """Injected input must refresh the browse-activity clock.

        _should_pump gates on a browser_* call completing recently, and injected
        input responses are consumed by the demux arm without passing through
        _note_browse_activity. Without this the mirror would freeze after 20s idle
        even though clicks were landing.
        """
        import kiro_crew.mcp_playwright_proxy as proxy

        class FakeStdin:
            def __init__(self):
                self.written = b""

            def write(self, b):
                self.written += b

            def flush(self):
                pass

        proxy._note_capture_size(800, 600)
        proxy._last_browse_activity = 0.0
        proxy._last_input_at = 0.0
        stdin = FakeStdin()
        assert proxy._inject_input_event(stdin, {"verb": "click", "x": 0.5, "y": 0.5})
        assert b"browser_mouse_click_xy" in stdin.written
        assert proxy._last_browse_activity > 0.0
        assert proxy._pump_bursting(proxy._last_input_at) is True

    def test_extension_mode_disables_the_input_relay(self):
        """In extension mode the user drives their own Chrome; injecting synthetic
        input there would be a surprising side effect."""
        import kiro_crew.mcp_playwright_proxy as proxy

        # Mirrors how _pump_enabled is derived, so the two stay consistent.
        assert proxy._input_enabled == ("--extension" not in proxy.sys.argv)


class TestProxyDrainFailureHandling:
    def test_failed_poll_is_distinguishable_from_an_empty_one(self, monkeypatch):
        """Collapsing the two would add the error backoff to the first gesture
        after every idle period."""
        import kiro_crew.mcp_playwright_proxy as proxy

        def boom(*_a, **_k):
            raise OSError("gateway down")

        monkeypatch.setattr(proxy.urllib.request, "urlopen", boom)
        ok, events = proxy._drain_input_once()
        assert ok is False and events == []
