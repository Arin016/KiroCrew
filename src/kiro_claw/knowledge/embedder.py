"""Ollama embedding client with graceful fallback.

Calls Ollama's /api/embeddings endpoint. Returns None silently if Ollama
is not running or the model isn't available — no errors, no degraded UX.
"""
from __future__ import annotations

import json
import logging
import struct
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Qwen3-Embedding-0.6B (1024d) — dedicated embedding model, not the generative LLM.
# Pulled from the public Ollama registry: `ollama pull qwen3-embedding:0.6b`.
# Documented fallback for smaller installs: `nomic-embed-text` (768d).
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_BASE_URL = "http://localhost:11434"
TIMEOUT = 10  # seconds
NEGATIVE_CACHE_TTL = 300  # seconds before re-checking failed availability
# Max chunk-content chars folded into an item embedding. Chunks are ~400 tokens
# (~1600 chars) by the heading-aware chunker, so this covers a full chunk with
# headroom while bounding the embed request well within the model's context.
_EMBED_CONTENT_BUDGET = 2000


class OllamaEmbedder:
    """Embed text via Ollama. Returns None on any failure (graceful degradation)."""

    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._available: bool | None = None  # cached availability check
        self._last_check: float = 0.0

    def is_available(self) -> bool:
        """Check if Ollama is reachable. Caches positive result; negative cached with TTL."""
        if self._available is True:
            return True
        if self._available is False and (time.time() - self._last_check) < NEGATIVE_CACHE_TTL:
            return False
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._available = resp.status == 200
        except Exception:
            self._available = False
        self._last_check = time.time()
        if not self._available:
            logger.info("Ollama not available at %s — embeddings disabled", self.base_url)
        return bool(self._available)

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text. Returns float list or None on failure."""
        if not text.strip():
            return None
        if not self.is_available():
            return None
        try:
            payload = json.dumps({"model": self.model, "prompt": text}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read())
            return data.get("embedding")
        except Exception as e:
            logger.debug("Ollama embed failed: %s", e)
            self._available = None  # invalidate so next call re-checks
            return None

    def embed_for_item(
        self, title: str, summary: str | None, content: str | None = None
    ) -> list[float] | None:
        """Embed title + summary + chunk content for knowledge items.

        Content is included so vector search matches on body text, not just the
        title/summary. It is appended last and truncated to ``_EMBED_CONTENT_BUDGET``
        chars to bound the embedding request (the model has a fixed context window;
        title and summary carry the highest-signal terms and must not be crowded out).
        """
        parts = [title]
        if summary:
            parts.append(summary)
        if content:
            parts.append(content[:_EMBED_CONTENT_BUDGET])
        return self.embed(" ".join(parts))


def floats_to_bytes(vec: list[float]) -> bytes:
    """Serialize float list to compact binary for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_floats(data: bytes) -> list[float]:
    """Deserialize binary BLOB back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def create_embedder_from_config(config: dict) -> OllamaEmbedder | None:
    """Create embedder from shared memory embedding config. Returns None if disabled.

    Uses the same config as Vector Memory (memory.embedding_provider/model/url)
    so knowledge and memory share one embedding setup.
    """
    memory_cfg = config.get("memory", {})
    if memory_cfg.get("embedding_provider") != "ollama":
        return None
    model = memory_cfg.get("embedding_model", DEFAULT_MODEL)
    base_url = memory_cfg.get("embedding_url", DEFAULT_BASE_URL)
    return OllamaEmbedder(model=model, base_url=base_url)
