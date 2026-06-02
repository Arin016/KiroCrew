"""Unit tests for /tk slash command handler."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.slack.events import _handle_tk_note


@pytest.mark.asyncio
async def test_tk_normal_note(tmp_path):
    """Normal note is saved."""
    notes_file = tmp_path / "quick-notes.json"
    orch = MagicMock()

    with patch("kiro_claw.slack.events._TK_NOTES_FILE", notes_file), \
         patch("kiro_claw.slack.events.is_allowed_user", return_value=True), \
         patch("kiro_claw.slack.events.sel"), \
         patch("aiohttp.ClientSession") as mock_sess:
        mock_sess.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock()))
        mock_sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await _handle_tk_note(orch, {
            "text": "follow up with Sarah",
            "user_id": "U123",
            "response_url": "https://hooks.slack.com/x",
            "channel_id": "C456",
            "channel_name": "general",
        })

    notes = json.loads(notes_file.read_text())
    assert len(notes) == 1
    assert notes[0]["text"] == "follow up with Sarah"
