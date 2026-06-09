"""Shared constants used across cli and gateway modules."""

# Retained for backward compatibility; intentionally empty in the public build
# (no Amazon-internal registry/toolbox package). Callers treat empty as "skip".
ARCC_REGISTRY = ""
ARCC_TOOLBOX_PACKAGE = ""

DATA_WARNING = (
    "⚠️  Do not enter sensitive, secret, or regulated data into KiroClaw.\n"
    "   Treat anything you send as potentially logged or processed by the\n"
    "   configured model provider."
)

# Outer wall-clock cap on a single ``_run_chat`` invocation (any dispatch site:
# primary user turn, queue-drain, cron injection, subagent injection, Slack first
# turn). Sized to match the inner ACP ``_DEFAULT_PROMPT_TIMEOUT`` (7200s) in
# ``acp/client.py`` so the dashboard layer doesn't bound below the transport.
# Wedged-session detection is handled by ``_STALE_TURN_TIMEOUT`` (90s, also in
# ``acp/client.py``); this cap is the upper safety ceiling for genuinely runaway
# work, not a "this turn took too long" guard.
CHAT_TURN_TIMEOUT = 7200.0

OLLAMA_DOCKER_CONTAINER = "kiroclaw-ollama"
