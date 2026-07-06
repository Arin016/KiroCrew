"""Tests for auto-title prompt construction (chat_title._build_title_prompt)."""

from __future__ import annotations

from kiro_claw.dashboard.chat_title import _build_title_prompt


def test_prompt_isolates_and_delimits_transcript():
    """The title prompt must instruct the model to name ONLY the delimited
    transcript and ignore residual session history — the shared _bg session
    retains a sibling session's context between recycles, which previously
    bled into titles (Mesh-2330)."""
    msgs = [
        {"role": "user", "content": "Update the doc refs to bullseye Set a goal"},
        {"role": "assistant", "content": "Done — the icon is the lucide Goal component."},
    ]
    prompt = _build_title_prompt(msgs)
    assert prompt is not None

    # Isolation instruction present.
    assert "ignore any earlier conversation" in prompt

    # Transcript is fenced and lands strictly between the delimiters.
    assert "===== CONVERSATION TO NAME =====" in prompt
    assert "===== END CONVERSATION =====" in prompt
    body = prompt.split("===== CONVERSATION TO NAME =====", 1)[1].split(
        "===== END CONVERSATION =====", 1
    )[0]
    assert "Update the doc refs" in body
    assert "lucide Goal component" in body


def test_prompt_none_when_no_usable_messages():
    """Contract preserved: empty or non-user/assistant messages yield None."""
    assert _build_title_prompt([]) is None
    assert _build_title_prompt([{"role": "system", "content": "x"}]) is None
