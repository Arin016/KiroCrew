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

CHAT_TURN_TIMEOUT = 600.0

OLLAMA_DOCKER_CONTAINER = "kiroclaw-ollama"
