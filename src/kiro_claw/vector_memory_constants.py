"""Lightweight consolidation-cap constants shared with :mod:`kiro_claw.vector_memory`.

Split out so callers that only need the caps (e.g. the consolidation prompt
builder in :mod:`kiro_claw.history`) can import them at module top level
without pulling ``vector_memory``'s heavy transitive deps (snowballstemmer +
the numpy/faiss optional imports) at import time. ``vector_memory`` re-exports
these, so both import paths stay valid.
"""

from __future__ import annotations

_MAX_SEMANTIC_PER_CONSOLIDATION = 20
_MAX_EPISODIC_PER_CONSOLIDATION = 10
# Cap lessons per consolidation: each write_lesson can perform up to 6 blocking
# embeds (1 rule + _MAX_BACKFILLS_PER_CALL lazy backfills), so an uncapped LLM
# lessons array could occupy a worker thread for minutes.
_MAX_LESSONS_PER_CONSOLIDATION = 10
