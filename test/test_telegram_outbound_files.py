"""Telegram's outbound raster upload: the gate, the seal, and the wire.

``test_outbound_files.py`` pins the channel-neutral extractor. This module pins
Telegram's consumer of it -- the renderer's seal-time extraction and
``client.send_photo``'s multipart -- and the invariants a plausible refactor
breaks silently:

* only a SEMANTIC seal extracts; a length rotation seals its chunks verbatim
* the classification input is BYTE-FAITHFUL, so an indented code literal that
  merely looks like an image reference is not uploaded
* live frames never flash a path the seal is about to replace with the picture
* the BYTES travel -- nothing re-opens ``OutboundFile.path``
* a refusal or a failed upload keeps the markup, so the path stays visible
* the caption is redacted at the wire, before truncation can split a secret
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.telegram.client import (
    TELEGRAM_CAPTION_MAX,
    TELEGRAM_MAX_FILES_PER_MESSAGE,
    TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    TELEGRAM_PHOTO_MAX_BYTES,
    TelegramClient,
)
from kiro_crew.telegram.renderer import _UPLOAD_LIMITS, TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

_NO_UPLOAD_CAPS = dataclasses.replace(TELEGRAM_CAPABILITIES, files_outbound=False)


def _utf16(text: str) -> int:
    """Length in UTF-16 code units -- the units Telegram's cap actually counts."""
    return len(text.encode("utf-16-le")) // 2


def _png(pad: int = 0) -> bytes:
    """A real, minimal PNG -- the extractor sniffs magic bytes, not extensions."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        return struct.pack(">I", len(body)) + kind + body + crc

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 4))
        + chunk(b"tEXt", b"pad\x00" + b"x" * pad)
        + chunk(b"IEND", b"")
    )


class FakeClient:
    """Captures the Bot API calls the renderer makes, including sendPhoto."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.photos: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self.rich_sent: list[str] = []
        self.rich_fails = True  # no table in these fixtures; keep the HTML path
        self.photo_results: list[int | None] = []
        self.photo_raises = False
        #: Per-call outcomes for send_message, consumed in order. An Exception
        #: entry is RAISED, None is returned as-is, anything else falls through
        #: to a normal id -- so a fault can be aimed at one specific send.
        self.msg_results: list[Any] = []
        #: ``sent``/``photos`` record what was ATTEMPTED (today's meaning, which
        #: is what proves a later sibling still ran). These two record only what
        #: actually LANDED, which is what reference accounting reads.
        self.msg_delivered: list[str] = []
        self.photo_delivered: list[str] = []
        self._mid = 100

    def _outcome(self, queue: list[Any]) -> Any:
        """Next queued outcome: raise an Exception entry, else return it."""
        if not queue:
            self._mid += 1
            return self._mid
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def send_typing(self, chat_id: int, *, message_thread_id: Any = None) -> None:
        return None

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> int:
        await asyncio.sleep(0)
        # Recorded BEFORE the outcome so an injected raise still proves the call
        # happened -- that is what "chunk k+1 was attempted" is read from.
        self.sent.append(text)
        result = self._outcome(self.msg_results)
        self.msg_delivered.append(text)
        return result

    async def edit_message(self, chat_id: int, message_id: int, text: str, **kw: Any) -> bool:
        await asyncio.sleep(0)
        self.edits.append(text)
        return True

    async def send_rich_message(self, chat_id: int, text: str, **kw: Any) -> int | None:
        self.rich_sent.append(text)
        return None if self.rich_fails else self._mid

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def send_photo(
        self,
        chat_id: int,
        data: bytes,
        mime: str,
        *,
        caption: str = "",
        message_thread_id: Any = None,
    ) -> int | None:
        await asyncio.sleep(0)
        if self.photo_raises:
            raise RuntimeError("network")
        self.photos.append({"data": data, "mime": mime, "caption": caption})
        result = self._outcome(self.photo_results)
        self.photo_delivered.append(caption)
        return result


def _renderer(cli: FakeClient, root: Path, **kw: Any) -> TelegramRenderer:
    caps = kw.pop("capabilities", TELEGRAM_CAPABILITIES)
    return TelegramRenderer(
        cli,  # type: ignore[arg-type]
        55,
        caps,
        session_key="telegram:1:0",
        upload_root=str(root),
        **kw,
    )


def _image(tmp_path: Path, name: str = "chart.png", pad: int = 0) -> Path:
    path = tmp_path / name
    path.write_bytes(_png(pad))
    return path


async def _seal_with(r: TelegramRenderer, files: list[OutboundFile]) -> None:
    """Drive one semantic seal whose extraction yielded exactly ``files``.

    Stubs extraction rather than writing real images, so a fault-injection case
    controls the file set precisely and the assertions read the delivery surface
    instead of the extractor.
    """
    body = "Here they are:"
    r._buf = [body]
    r._extract_uploads = AsyncMock(return_value=(body, files))  # type: ignore[method-assign]
    await r._seal_current()


async def _turn(r: TelegramRenderer, text: str) -> None:
    await r.on_text_chunk(text)
    await r.on_done()


# ── The three-way gate ────────────────────────────────────────────────────────


class TestUploadGate:
    def test_a_reference_is_uploaded_and_its_markup_leaves_the_text(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"Here it is:\n\n![Q3 revenue]({img})"))

        assert [p["mime"] for p in cli.photos] == ["image/png"]
        assert cli.photos[0]["data"] == img.read_bytes()
        assert all(str(img) not in body for body in cli.sent + cli.edits)

    def test_the_capability_flag_is_the_switch(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path, capabilities=_NO_UPLOAD_CAPS)

        asyncio.run(_turn(r, f"Look: ![alt]({img})"))

        # Honest degradation: the path stays printed rather than silently dropped.
        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent + cli.edits)

    def test_a_restricted_session_uploads_nothing(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path, uploads_allowed=False)

        asyncio.run(_turn(r, f"Look: ![alt]({img})"))

        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent + cli.edits)

    @pytest.mark.parametrize("root", ["", "relative/dir"])
    def test_an_untrusted_root_disables_uploads(self, tmp_path: Path, root: str) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        r.authorize_upload_root(root)  # what a bad provider cwd would supply

        asyncio.run(_turn(r, f"Look: ![alt]({img})"))

        assert cli.photos == []

    def test_a_file_outside_the_root_is_refused_with_its_markup_kept(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        outside = tmp_path / "sub"
        outside.mkdir()
        r = _renderer(cli, outside)  # the image sits ABOVE the approved root

        asyncio.run(_turn(r, f"Look: ![alt]({img})"))

        assert cli.photos == []
        landed = "\n".join(cli.sent + cli.edits)
        assert str(img) in landed and "not sent" in landed

    def test_both_outcomes_are_sel_audited_with_path_free_reasons(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        with patch("kiro_crew.telegram.renderer.sel") as sel_mock:
            asyncio.run(_turn(r, f"![ok]({img})\n\n![gone]({tmp_path}/nope.png)"))

        calls = sel_mock.return_value.log_api_access.call_args_list
        outcomes = {c.kwargs["outcome"]: c.kwargs for c in calls}
        assert set(outcomes) == {"allowed", "denied"}
        denied = outcomes["denied"]
        assert denied["operation"] == "telegram_renderer.upload_files"
        assert denied["source"] == "telegram"
        assert denied["error"] == "missing"
        # The destination reaches the CHAT, never the audit log.
        assert all(str(tmp_path) not in str(c.kwargs) for c in calls)


# ── Seal-time extraction ──────────────────────────────────────────────────────


class TestSealTimeExtraction:
    def test_an_indented_code_literal_is_displayed_not_uploaded(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        # Four-space indent = code block. Stripping before classification (the
        # defect this pins) would upload a picture the author only showed.
        asyncio.run(_turn(r, f"    ![alt]({img})"))

        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent + cli.edits)

    def test_a_fenced_reference_is_documentation(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"How to:\n\n```\n![alt]({img})\n```"))

        assert cli.photos == []

    @pytest.mark.parametrize("arrived", [True, False])
    def test_live_frames_never_show_image_markup(self, tmp_path: Path, arrived: bool) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        markup = f"![alt]({img})" if arrived else f"![alt]({img}"

        async def run() -> None:
            await r.on_text_chunk(f"Rendering it now: {markup}")
            await r._stream_live(force=True)

        asyncio.run(run())

        frames = cli.sent + cli.edits
        assert frames, "expected a live frame"
        assert all(str(img) not in frame for frame in frames)

    def test_markup_removal_cannot_reassemble_a_credential(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        # Halves that are separated in the source and contiguous once the image
        # markup between them is cut out.
        secret = "AKIAIOSFODNN7EXAMPLE"
        head, tail = secret[:10], secret[10:]

        asyncio.run(_turn(r, f"{head}![alt]({img}){tail}"))

        assert cli.photos, "the picture should still be delivered"
        assert all(secret not in body for body in cli.sent + cli.edits)

    def test_an_image_only_reply_posts_no_placeholder_bubble(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"![alt]({img})"))

        assert len(cli.photos) == 1
        # The picture IS the answer: no "…" bubble, and nothing was posted at all
        # because live frames already hide the markup.
        assert cli.sent == [] and cli.edits == []

    def test_an_image_only_reply_withdraws_a_live_frame_it_did_post(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        async def run() -> None:
            await r.on_text_chunk(f"![alt]({img})")
            # A tool footer puts a real bubble on screen even though the body is
            # empty; leaving it would strand text the picture replaces.
            await r.on_tool_call("call-1", "grep")
            await r.on_done()

        asyncio.run(run())

        assert len(cli.photos) == 1
        assert cli.deleted, "the superseded live frame should be removed"

    def test_a_rejected_file_keeps_its_markup_and_gains_a_reason(self, tmp_path: Path) -> None:
        cli = FakeClient()
        missing = tmp_path / "absent.png"
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"Chart: ![alt]({missing})"))

        landed = "\n".join(cli.sent + cli.edits)
        assert str(missing) in landed
        assert "not sent" in landed

    def test_extraction_failure_falls_back_to_the_text(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        boom = AsyncMock(side_effect=RuntimeError("extractor down"))

        with patch("kiro_crew.telegram.renderer.extract_local_refs_off_loop", boom):
            asyncio.run(_turn(r, f"Chart: ![alt]({img})"))

        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent + cli.edits)

    def test_the_bytes_travel_not_the_path(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        original = img.read_bytes()
        r = _renderer(cli, tmp_path)

        async def run() -> None:
            _body, files = await r._extract_uploads(f"![alt]({img})")
            # Whoever can write the directory swaps the file between the gated
            # read and the send. Re-opening the path here would ship this.
            img.write_bytes(b"not an image at all")
            await r._upload_photos(files)

        asyncio.run(run())

        assert [p["data"] for p in cli.photos] == [original]

    def test_the_alt_text_becomes_the_caption(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"![Q3 revenue by region]({img})"))

        assert cli.photos[0]["caption"] == "Q3 revenue by region"


# ── Rotation never hands a splitter chunk to the extractor ────────────────────


class TestRotation:
    def test_a_reference_is_held_for_the_semantic_seal(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        prose = "\n\n".join("filler paragraph." for _ in range(400))
        assert len(prose) > r._limit(), "the fixture must actually rotate"

        # The reference sits past the rotation boundary, so a naive rotation
        # would seal it as splitter output -- where extraction is forbidden.
        asyncio.run(_turn(r, f"{prose}\n\n![alt]({img})"))

        assert len(cli.photos) == 1
        assert all(str(img) not in body for body in cli.sent + cli.edits)

    def test_a_length_rotation_seals_its_chunks_without_extracting(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        seen: list[bool] = []
        real = r._seal_current

        async def spy(*, keyboard: Any = None, extract_uploads: bool = True) -> None:
            seen.append(extract_uploads)
            await real(keyboard=keyboard, extract_uploads=extract_uploads)

        r._seal_current = spy  # type: ignore[assignment]
        prose = "\n\n".join("filler paragraph." for _ in range(400))
        asyncio.run(_turn(r, f"{prose}\n\n![alt]({img})"))

        assert False in seen, "expected at least one non-extracting rotation seal"
        assert seen[-1] is True, "the final semantic seal must extract"

    def test_a_dirty_long_line_disables_the_segments_uploads(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        r = _renderer(cli, tmp_path)
        # An unbreakable line longer than the whole budget cannot be cut cleanly,
        # so a reference arriving AFTER that rotation is no longer trustworthy:
        # the segment stops being an extraction candidate for the rest of itself.
        unbreakable = "y" * (r._limit() + 50)

        async def run() -> None:
            await r.on_text_chunk(f"{unbreakable}\n\n" + "z " * 2000)
            assert r._segment_uploads_safe is False, "the dirty cut should have fired"
            await r.on_text_chunk(f"\n\n![alt]({img})")
            await r.on_done()

        asyncio.run(run())

        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent + cli.edits)


# ── Delivery failures are reported, never dropped ─────────────────────────────


class TestPhotoDelivery:
    def test_a_failed_upload_reposts_its_markup(self, tmp_path: Path) -> None:
        cli, img = FakeClient(), _image(tmp_path)
        cli.photo_results = [None]
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"Chart: ![alt]({img})"))

        # Extraction already cut the markdown, so silence would leave the reply
        # referring to a picture with neither the picture nor its path.
        assert any(str(img) in body for body in cli.sent)

    def test_a_raising_upload_is_isolated_and_the_rest_still_go(self, tmp_path: Path) -> None:
        cli = FakeClient()
        first, second = _image(tmp_path, "a.png"), _image(tmp_path, "b.png")
        cli.photo_results = [None, 7]
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"![one]({first})\n\n![two]({second})"))

        assert len(cli.photos) == 2, "the second upload must still be attempted"
        reposted = "\n".join(cli.sent)
        assert str(first) in reposted and str(second) not in reposted

    def test_a_transport_error_does_not_abandon_the_files_behind_it(self, tmp_path: Path) -> None:
        cli = FakeClient()
        cli.photo_raises = True
        img = _image(tmp_path)
        r = _renderer(cli, tmp_path)

        asyncio.run(_turn(r, f"![one]({img})"))

        assert cli.photos == []
        assert any(str(img) in body for body in cli.sent)

    # ── The outbound failure invariant, exhaustively ──────────────────────────
    # (which operation fails) x (how it fails). The invariant under test: no
    # single delivery's failure -- returned OR raised -- may cascade into the
    # loss of a sibling. Every reference must end up accounted for as a
    # delivered photo, as recovered markup, or as a logged loss, and later
    # siblings must still have been attempted. Three consecutive blockers landed
    # in this span by closing one cell at a time, so the cells are enumerated.
    @pytest.mark.parametrize("how", ["returns_none", "raises"])
    @pytest.mark.parametrize("where", ["seal", "photo", "recovery"])
    def test_no_failed_delivery_cascades_to_a_sibling(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str, how: str
    ) -> None:
        cli = FakeClient()
        r = _renderer(cli, tmp_path)
        boom = RuntimeError("proxy returned HTML, so resp.json raised")
        # Two references with distinct alts; long enough that the recovery post
        # needs two chunks, which is what makes "chunk k+1 survived" observable.
        alt = "a" * (r._limit() // 2)
        files = [
            OutboundFile(
                path=f"{tmp_path}/i{i}.png", data=_png(i), alt=f"{alt}{i}", mime="image/png"
            )
            for i in range(2)
        ]
        if where == "seal":
            # The seal's only failure shape is a raise; returning None is its
            # normal contract, so that cell asserts the happy path still holds.
            if how == "raises":
                r._seal_segment_text = AsyncMock(side_effect=boom)  # type: ignore[method-assign]
            else:
                r._seal_segment_text = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elif where == "photo":
            cli.photo_results = [boom if how == "raises" else None, 7]
        else:
            cli.photo_results = [None, None]  # force both into recovery
            # The seal sends first and must SUCCEED here, or the fault lands on
            # the text path instead of the recovery chunk this cell targets. A
            # non-Exception entry passes through to a normal id.
            cli.msg_results = [1, boom if how == "raises" else None]

        with caplog.at_level(logging.WARNING):
            if where == "seal" and how == "raises":
                # The text exception still reaches the caller unchanged...
                with pytest.raises(RuntimeError):
                    asyncio.run(_seal_with(r, files))
            else:
                asyncio.run(_seal_with(r, files))

        # ...and the photos were attempted anyway.
        assert len(cli.photos) == 2, "every photo must be attempted"
        logged = caplog.text
        for item in files:
            delivered = item.alt in cli.photo_delivered
            recovered = any(f"![{item.alt}]({item.path})" in body for body in cli.msg_delivered)
            assert delivered or recovered or logged, (
                f"reference {item.path} vanished silently: not delivered as a photo, "
                f"not in recovered markup, and no loss was logged"
            )
        if where == "recovery":
            # Chunk 1 failed either way; chunk 2 must still have been attempted,
            # and it carries the second reference's only remaining copy.
            assert len(cli.sent) >= 2, "a failed recovery chunk must not abandon the next"
            assert f"![{files[1].alt}]({files[1].path})" in "".join(cli.msg_delivered)

    # One reference per shape/scale, asserting BOTH recovery properties on every
    # case: nothing is dropped (the round-1 invariant) AND no chunk exceeds the
    # cap in the units Telegram actually counts (this round). Two point tests let
    # each property be closed while the other regressed -- the whole failure
    # pattern in this span -- so they are one oracle instead.
    @pytest.mark.parametrize("shape", ["ascii", "astral", "mixed", "bmp"])
    @pytest.mark.parametrize("scale", ["packs", "straddles", "overflows"])
    def test_recovery_is_lossless_and_never_exceeds_the_utf16_cap(
        self, tmp_path: Path, shape: str, scale: str
    ) -> None:
        cli = FakeClient()
        cli.photo_results = [None, None, None]
        r = _renderer(cli, tmp_path)
        limit = r._limit()
        # "astral" costs 2 UTF-16 units per code point, "bmp" only 1 despite
        # being multi-byte in UTF-8 -- so a byte-based budget would fail too.
        unit = {"ascii": "a", "astral": "\U0001f5bc", "mixed": "a\U0001f5bc", "bmp": "\u6f22"}[
            shape
        ]
        target = {"packs": limit // 3, "straddles": limit - 8, "overflows": limit + 400}[scale]
        alt = unit * max(1, target // _utf16(unit))
        files = [
            OutboundFile(path=f"{tmp_path}/i{i}.png", data=_png(), alt=alt, mime="image/png")
            for i in range(3)
        ]

        asyncio.run(r._upload_photos(files))

        assert cli.sent, "expected a recovery post"
        # (b) Telegram counts UTF-16 code units; a chunk over its cap is rejected,
        # and send_message reports that as None rather than raising.
        assert all(_utf16(body) <= limit for body in cli.sent)
        assert all(_utf16(body) <= 4096 for body in cli.sent)
        # (a) Every reference still arrives, character for character. The newlines
        # this path synthesized between references are what message boundaries
        # replace, so they are the only characters allowed to differ.
        expected = "\n".join(f"![{item.alt}]({item.path})" for item in files)
        assert "".join(cli.sent).replace("\n", "") == expected.replace("\n", "")
        for item in files:
            assert f"![{item.alt}]({item.path})" in "".join(cli.sent)


# ── The multipart wire ────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def json(self, content_type: Any = None) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Captures what ``_api`` actually put on the wire."""

    def __init__(self, payloads: list[dict[str, Any]] | None = None) -> None:
        self.closed = False
        self.calls: list[dict[str, Any]] = []
        self._payloads = payloads or [{"ok": True, "result": {"message_id": 42}}]

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(self._payloads[min(len(self.calls) - 1, len(self._payloads) - 1)])


def _client_with(session: _FakeSession) -> TelegramClient:
    client = TelegramClient(token="tok")
    client._session = session  # type: ignore[assignment]
    return client


def _fields(form: Any) -> dict[str, Any]:
    """Field name -> value for an aiohttp FormData, read from its internals."""
    return {opts["name"]: value for opts, _headers, value in form._fields}


class TestMultipartWire:
    def test_posts_the_bytes_with_a_synthetic_filename(self) -> None:
        session = _FakeSession()
        client = _client_with(session)
        data = _png()

        mid = asyncio.run(client.send_photo(555, data, "image/png", caption="Revenue"))

        assert mid == 42
        call = session.calls[0]
        assert call["url"].endswith("/sendPhoto")
        # Multipart, not JSON: a JSON `photo` field can only be a URL or an
        # existing file_id, never bytes we hold.
        assert call["json"] is None
        fields = _fields(call["data"])
        assert fields["photo"] == data
        assert fields["chat_id"] == "555"
        assert fields["caption"] == "Revenue"
        # The name is derived from the sniffed type, so no agent-influenced
        # string reaches a Content-Disposition header.
        names = [
            opts.get("filename") for opts, _h, _v in call["data"]._fields if "filename" in opts
        ]
        assert names == ["image.png"]

    def test_a_forum_topic_and_an_empty_caption_are_omitted(self) -> None:
        session = _FakeSession()
        client = _client_with(session)

        asyncio.run(client.send_photo(555, _png(), "image/jpeg"))

        fields = _fields(session.calls[0]["data"])
        assert "caption" not in fields and "message_thread_id" not in fields

    def test_a_rate_limited_upload_is_retried_with_a_fresh_form(self) -> None:
        session = _FakeSession(
            [
                {"ok": False, "error_code": 429, "parameters": {"retry_after": 0}},
                {"ok": True, "result": {"message_id": 9}},
            ]
        )
        client = _client_with(session)

        mid = asyncio.run(client.send_photo(1, _png(), "image/png"))

        assert mid == 9
        # aiohttp consumes a FormData on write, so a reused body raises on the
        # retry -- the factory has to build a new one per attempt.
        assert len(session.calls) == 2
        assert session.calls[0]["data"] is not session.calls[1]["data"]

    def test_a_failed_upload_returns_none_rather_than_raising(self) -> None:
        session = _FakeSession([{"ok": False, "error_code": 400, "description": "bad"}])

        assert asyncio.run(_client_with(session).send_photo(1, _png(), "image/png")) is None

    def test_the_caption_is_redacted_at_the_sink(self) -> None:
        session = _FakeSession()
        client = _client_with(session)
        secret = "AKIAIOSFODNN7EXAMPLE"

        # The verb is the one boundary every upload crosses, so it redacts rather
        # than trusting a caller to have done it.
        asyncio.run(client.send_photo(1, _png(), "image/png", caption=f"key {secret}"))

        assert secret not in _fields(session.calls[0]["data"])["caption"]

    def test_redaction_runs_before_truncation(self) -> None:
        session = _FakeSession()
        client = _client_with(session)
        secret = "AKIAIOSFODNN7EXAMPLE"
        # Straddles the caption cap: truncating first would cut the secret and
        # let the surviving prefix through.
        caption = "a" * (TELEGRAM_CAPTION_MAX - 8) + secret

        asyncio.run(client.send_photo(1, _png(), "image/png", caption=caption))

        sent = _fields(session.calls[0]["data"])["caption"]
        assert len(sent) <= TELEGRAM_CAPTION_MAX
        assert secret[:12] not in sent

    def test_extract_limits_carry_telegram_ceilings(self) -> None:
        assert _UPLOAD_LIMITS.max_file_bytes == TELEGRAM_PHOTO_MAX_BYTES
        assert _UPLOAD_LIMITS.max_files == TELEGRAM_MAX_FILES_PER_MESSAGE
        assert _UPLOAD_LIMITS.max_total_bytes == TELEGRAM_MAX_TOTAL_UPLOAD_BYTES
        # The aggregate is the memory bound for one seal, so it must sit below
        # files x per-file or it bounds nothing.
        assert TELEGRAM_MAX_TOTAL_UPLOAD_BYTES < (
            TELEGRAM_MAX_FILES_PER_MESSAGE * TELEGRAM_PHOTO_MAX_BYTES
        )

    def test_an_oversize_file_is_refused_while_its_markup_is_still_there(
        self, tmp_path: Path
    ) -> None:
        cli = FakeClient()
        img = _image(tmp_path, pad=4096)
        r = _renderer(cli, tmp_path)
        small = dataclasses.replace(_UPLOAD_LIMITS, max_file_bytes=64)

        with patch("kiro_crew.telegram.renderer._UPLOAD_LIMITS", small):
            asyncio.run(_turn(r, f"Chart: ![alt]({img})"))

        assert cli.photos == []
        landed = "\n".join(cli.sent + cli.edits)
        assert str(img) in landed and "not sent" in landed


# ── The restricted-session gate ───────────────────────────────────────────────


class TestRestrictedGate:
    def _dispatcher(self) -> Any:
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        return TelegramDispatcher.__new__(TelegramDispatcher)

    @pytest.mark.parametrize("key", ["telegram:kirocrew:direct:42", "unified:kirocrew"])
    def test_every_key_this_dispatcher_builds_is_allowed(self, key: str) -> None:
        # `unified` dm_scope drops the channel out of the key, so a gate written
        # as a "telegram:" allowlist would deny every upload for those users.
        assert self._dispatcher()._uploads_restricted(key) is False

    def test_a_dashboard_key_is_denied_and_audited(self) -> None:
        with patch("kiro_crew.telegram.transport_dispatch.sel") as sel_mock:
            assert self._dispatcher()._uploads_restricted("dashboard:slot-1") is True

        kwargs = sel_mock.return_value.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "telegram_dispatch.upload_files"
        assert kwargs["outcome"] == "denied"
        assert kwargs["error"] == "restricted_session"

    def test_the_renderer_requires_all_three_conditions(self, tmp_path: Path) -> None:
        cli = FakeClient()
        assert _renderer(cli, tmp_path)._uploads_enabled() is True
        assert _renderer(cli, tmp_path, uploads_allowed=False)._uploads_enabled() is False
        assert _renderer(cli, tmp_path, capabilities=_NO_UPLOAD_CAPS)._uploads_enabled() is False
        bare = TelegramRenderer(MagicMock(), 1, TELEGRAM_CAPABILITIES)
        assert bare._uploads_enabled() is False, "no authorized root -> no uploads"
