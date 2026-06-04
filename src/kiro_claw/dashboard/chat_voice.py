"""Voice synthesis endpoints — TTS config and streaming synthesis.

TTS synthesis is optional and routed through ``voice_reply`` (which lazily
imports any cloud TTS backend only when invoked). The endpoints below stay
importable on a vanilla machine; synthesis simply errors gracefully if no
backend is configured.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import tempfile
import time

from aiohttp import web

from kiro_claw.config.loader import config_path
from kiro_claw.dashboard.state import DashboardState
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.slack.handler import _vc
from kiro_claw.voice_reply import VALID_ENGINES, stitch_mp3s, streaming_voice_reply

logger = logging.getLogger(__name__)


async def api_voice_config(request: web.Request) -> web.Response:
    """GET/PUT /api/voice/config — read or update voice settings."""
    if request.method == "GET":
        return web.json_response(
            {
                "enabled": _vc.global_enabled,
                "voice": _vc.default_voice,
                "engine": _vc.default_engine,
                "rate": _vc.default_rate,
                "pitch": _vc.default_pitch,
                "autoSpeak": _vc.global_enabled,
                "aws_profile": _vc.aws_profile,
                "region": _vc.region,
            }
        )

    # PUT — update and persist
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Update in-memory
    if "voice" in body:
        _vc.default_voice = str(body["voice"])
    if "engine" in body and body["engine"] in VALID_ENGINES:
        _vc.default_engine = body["engine"]
    if "rate" in body:
        _vc.default_rate = str(body["rate"])
    if "pitch" in body:
        _vc.default_pitch = str(body["pitch"])
    if "enabled" in body:
        _vc.global_enabled = bool(body["enabled"])
    if "autoSpeak" in body:
        _vc.global_enabled = bool(body["autoSpeak"])
    if "aws_profile" in body:
        _vc.aws_profile = str(body["aws_profile"]).strip()
    if "region" in body:
        _vc.region = str(body["region"]).strip()

    # Persist to config.json
    try:
        cfg_path = config_path()
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg["voice_reply"] = {
            "enabled": _vc.global_enabled,
            "voice_id": _vc.default_voice,
            "engine": _vc.default_engine,
            "rate": _vc.default_rate,
            "pitch": _vc.default_pitch,
            "aws_profile": _vc.aws_profile,
            "region": _vc.region,
        }
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        logger.exception("Failed to persist voice config")

    return web.json_response({"ok": True})


async def api_voice_synthesize(request: web.Request) -> web.Response:
    """POST /api/voice/synthesize — sentence-chunked TTS.

    Synthesizes each sentence in parallel, broadcasts ``voice_chunk``
    WS events with base64 MP3 data for immediate playback, then stitches
    all chunks into a single MP3 and broadcasts ``voice_complete``.
    """

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    text = body.get("text", "").strip()
    slot_key = body.get("slot", "")
    if not text:
        return web.json_response({"error": "text required"}, status=400)

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)

    # Voice config — use defaults from handler config or body overrides
    voice_id = body.get("voice", _vc.default_voice)
    engine = body.get("engine", _vc.default_engine)
    rate = body.get("rate", _vc.default_rate)
    pitch = body.get("pitch", _vc.default_pitch)

    chunk_paths: list[str] = []
    final_path: str | None = None
    try:
        async for idx, sentence, mp3_bytes in streaming_voice_reply(
            text,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
        ):
            # Save chunk for stitching

            fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            with open(chunk_path, "wb") as f:
                f.write(mp3_bytes)
            chunk_paths.append(chunk_path)

            # Broadcast to dashboard for immediate playback
            state.broadcast_ws(
                "voice_chunk",
                {
                    "slot": slot_key,
                    "index": idx,
                    "sentence": sentence,
                    "audio": base64.b64encode(mp3_bytes).decode(),
                },
            )

        # Stitch all chunks into single MP3
        if chunk_paths:
            final_path = await stitch_mp3s(chunk_paths)
            if final_path:
                with open(final_path, "rb") as f:
                    final_bytes = f.read()
                state.broadcast_ws(
                    "voice_complete",
                    {
                        "slot": slot_key,
                        "audio": base64.b64encode(final_bytes).decode(),
                        "chunks": len(chunk_paths),
                    },
                )

        return web.json_response({"ok": True, "chunks": len(chunk_paths)})
    except Exception as exc:
        logger.exception("Voice synthesis failed")
        err_msg, _ = redact_exfiltration_urls(str(exc))
        err_msg, _ = redact_credentials(err_msg)
        state.broadcast_ws(
            "voice_error",
            {"slot": slot_key, "error": err_msg},
        )
        return web.json_response(
            {"ok": False, "error": err_msg}, status=500
        )
    finally:
        if final_path:
            with contextlib.suppress(OSError):
                os.unlink(final_path)
        for p in chunk_paths:
            with contextlib.suppress(OSError):
                os.unlink(p)


# ── Voices list (cached) ──

_voices_cache: list[dict] | None = None
_voices_cache_ts: float = 0.0
_VOICES_CACHE_TTL = 3600  # 1 hour


async def api_voice_voices(request: web.Request) -> web.Response:
    """GET /api/voice/voices — list available Polly voices (cached 1h)."""
    global _voices_cache, _voices_cache_ts

    now = time.time()
    if _voices_cache is not None and (now - _voices_cache_ts) < _VOICES_CACHE_TTL:
        return web.json_response({"voices": _voices_cache})

    # Call aws polly describe-voices
    cmd = ["aws", "polly", "describe-voices", "--output", "json"]
    if _vc.aws_profile:
        cmd += ["--profile", _vc.aws_profile]
    if _vc.region:
        cmd += ["--region", _vc.region]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error("describe-voices failed: %s", err)
            return web.json_response({"error": "Failed to retrieve voices"}, status=502)

        data = json.loads(stdout)
        voices = [
            {
                "id": v["Id"],
                "name": v["Name"],
                "language": v["LanguageName"],
                "languageCode": v["LanguageCode"],
                "gender": v["Gender"],
                "engines": v["SupportedEngines"],
            }
            for v in data.get("Voices", [])
        ]
        voices.sort(key=lambda v: (v["languageCode"], v["name"]))
        _voices_cache = voices
        _voices_cache_ts = now
        return web.json_response({"voices": voices})
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return web.json_response({"error": "timeout"}, status=504)
    except Exception:
        logger.exception("describe-voices error")
        return web.json_response({"error": "Failed to retrieve voices"}, status=500)
