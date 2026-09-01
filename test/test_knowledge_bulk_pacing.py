"""The knowledge re-embed sweep paces itself; an explicitly-requested one does not.

Mirrors ``test/test_vector_memory_bulk_pacing.py`` for the knowledge path. Locks in
the behaviour that keeps an unattended post-migration rebuild from pinning several
cores for tens of minutes: every bulk row is followed by an idle window
proportional to the work it just did (taken by awaiting off the loop, so it holds
neither the DB lock nor the model), and skipped entirely when a human triggered the
rebuild and is watching its progress bar.

Runs WITHOUT ``test/conftest.py`` and WITHOUT pytest-asyncio: the coroutine is
driven via ``asyncio.run`` inside plain sync tests, the store is a minimal fake,
and the per-item DB-write helpers are monkeypatched so no SQLite is touched.
"""

from __future__ import annotations

import asyncio

from kiro_crew import embeddings as _embeddings
from kiro_crew.knowledge import embedder as emb
from kiro_crew.knowledge import ingestion as ing

PRIORITY_NORMAL = _embeddings.PRIORITY_NORMAL
PRIORITY_BULK = _embeddings.PRIORITY_BULK


class _FakeEmbedder:
    """Records the priority each row was embedded at; returns a fixed vector."""

    model = "fake-model:0.1b"
    content_budget = 1000

    def __init__(self, vector):
        self._vector = vector
        self.priorities: list[int] = []

    def embed_for_item(self, title, summary, content=None, *, priority=None):
        self.priorities.append(priority)
        return self._vector


class _FakeStore:
    """Only needs a ``.db`` whose ``commit()`` is a no-op; every read/write helper
    that touches ``store.db`` is monkeypatched out in the tests."""

    class _DB:
        def commit(self):
            pass

    def __init__(self):
        self.db = self._DB()


def _install_rows(monkeypatch, rows):
    """Feed ``rows`` to the loop as a single page, then stop.

    ``rebuild_embeddings`` pages via ``_fetch_rebuild_page`` until it returns an
    empty list, so the first call returns all rows and the second returns ``[]``.
    Also stub out every per-item DB write helper so the fake store is never hit.
    """
    pages = [list(rows), []]

    def _fetch(store, page_where, params_tail, last_id):
        return pages.pop(0) if pages else []

    writes: list[str] = []

    def _write(store, item_id, blob, sig, now_iso, snap):
        writes.append(item_id)
        return True

    monkeypatch.setattr(ing, "_fetch_rebuild_page", _fetch)
    monkeypatch.setattr(ing, "_write_item_embedding", _write)
    monkeypatch.setattr(ing, "_stamp_embed_attempt", lambda *a, **k: None)
    monkeypatch.setattr(ing, "_commit_rebuild_progress", lambda *a, **k: None)
    return writes


def _row(item_id):
    return {
        "id": item_id,
        "title": f"title {item_id}",
        "summary": f"summary {item_id}",
        "content": f"content {item_id}",
        "updated_at": "2024-01-01T00:00:00",
    }


def _record_sleeps(monkeypatch):
    """Capture pace pauses instead of really sleeping."""
    recorded: list[float] = []

    async def _sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(ing.asyncio, "sleep", _sleep)
    return recorded


def _run(store, embedder, **kwargs):
    return asyncio.run(ing.rebuild_embeddings(store, embedder, **kwargs))


def test_each_bulk_row_is_paced(monkeypatch):
    """The default (watcher self-heal) path idles once per row at PRIORITY_BULK."""
    monkeypatch.setattr(ing, "bulk_pace_delay", lambda elapsed: 0.125)
    writes = _install_rows(monkeypatch, [_row("a"), _row("b"), _row("c")])
    sleeps = _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder([0.5, 0.5, 0.5, 0.5])

    assert _run(_FakeStore(), embedder) == 3
    assert writes == ["a", "b", "c"]
    assert sleeps == [0.125, 0.125, 0.125]
    assert embedder.priorities == [ing.PRIORITY_BULK] * 3


def test_pace_false_never_sleeps(monkeypatch):
    """The dashboard trigger path (pace=False) never idles and stays PRIORITY_NORMAL."""
    monkeypatch.setattr(ing, "bulk_pace_delay", lambda elapsed: 0.125)
    _install_rows(monkeypatch, [_row("a"), _row("b"), _row("c")])
    sleeps = _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder([0.5, 0.5, 0.5, 0.5])

    assert _run(_FakeStore(), embedder, pace=False) == 3
    assert sleeps == []
    assert embedder.priorities == [ing.PRIORITY_NORMAL] * 3


def test_zero_delay_does_not_call_sleep(monkeypatch):
    """Pacing off (duty 1.0 -> zero delay) must not add a sleep per row."""
    monkeypatch.setattr(ing, "bulk_pace_delay", lambda elapsed: 0.0)
    _install_rows(monkeypatch, [_row("a"), _row("b")])
    sleeps = _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder([0.5, 0.5, 0.5, 0.5])

    assert _run(_FakeStore(), embedder) == 2
    assert sleeps == []
    assert embedder.priorities == [ing.PRIORITY_BULK] * 2


def test_a_failed_row_is_still_paced_by_measured_time(monkeypatch):
    """A row returning no vector still ran the model; pace on elapsed, not success."""
    monkeypatch.setattr(ing, "bulk_pace_delay", lambda elapsed: 0.05)
    _install_rows(monkeypatch, [_row("a"), _row("b")])
    sleeps = _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder(None)  # embed fails -> None

    # No vector landed, so nothing is counted as processed.
    assert _run(_FakeStore(), embedder) == 0
    # But each failed row is still paced by its measured elapsed time.
    assert sleeps == [0.05, 0.05]
    assert embedder.priorities == [ing.PRIORITY_BULK] * 2


def test_delay_is_derived_from_the_row_s_own_elapsed_time(monkeypatch):
    """bulk_pace_delay is fed the row's own measured elapsed time, once per row."""
    seen: list[float] = []

    def _record(elapsed):
        seen.append(elapsed)
        return 0.0

    monkeypatch.setattr(ing, "bulk_pace_delay", _record)
    _install_rows(monkeypatch, [_row("a")])
    _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder([0.5, 0.5, 0.5, 0.5])

    _run(_FakeStore(), embedder)
    assert len(seen) == 1
    assert seen[0] >= 0.0


def test_pace_false_does_not_measure_or_pace(monkeypatch):
    """The attended path never calls bulk_pace_delay at all."""
    calls: list[float] = []
    monkeypatch.setattr(ing, "bulk_pace_delay", lambda elapsed: calls.append(elapsed) or 0.1)
    _install_rows(monkeypatch, [_row("a"), _row("b")])
    sleeps = _record_sleeps(monkeypatch)
    embedder = _FakeEmbedder([0.5, 0.5, 0.5, 0.5])

    _run(_FakeStore(), embedder, pace=False)
    assert calls == []
    assert sleeps == []


class _FakeBackend:
    """A shared-embedder stand-in that records the priority landing on ``embed``.

    Reports ready (so ``InProcessEmbedder.embed`` skips the availability probe
    embed and proceeds to the real forwarding call) and returns a fixed vector
    so ``embed`` does not treat the row as a failure.
    """

    model_id = "fake-backend:0.1b"

    def __init__(self):
        self.priorities: list[int] = []

    def is_ready(self):
        return True

    def embed(self, text, *, priority=PRIORITY_NORMAL):
        self.priorities.append(priority)
        return [0.5, 0.5, 0.5, 0.5]


def test_embed_for_item_forwards_bulk_priority_to_backend(monkeypatch):
    """The priority requested at ``embed_for_item`` must reach ``backend.embed``.

    This closes the gap the ``ingestion`` tests leave open: they assert the
    priority reaching ``embed_for_item``, but nothing verifies
    ``embed_for_item``/``embed`` FORWARD it into the shared backend's
    ``embed(text, priority=...)`` — the link that actually sizes the bulk pool
    via ``bulk_embed_threads()``. A regression dropping ``priority=priority`` in
    either ``embedder.py`` call site would slip past the ingestion tests but
    fail here.
    """
    backend = _FakeBackend()
    # Module-attribute lookup (matches InProcessEmbedder._get_embedder).
    monkeypatch.setattr(_embeddings, "get_shared_embedder", lambda: backend)

    vec = emb.InProcessEmbedder().embed_for_item("t", "s", "c", priority=PRIORITY_BULK)

    assert vec == [0.5, 0.5, 0.5, 0.5]
    assert backend.priorities == [PRIORITY_BULK]


def test_embed_for_item_defaults_to_normal_priority_on_backend(monkeypatch):
    """With no priority arg, ``embed_for_item`` must land ``PRIORITY_NORMAL``."""
    backend = _FakeBackend()
    monkeypatch.setattr(_embeddings, "get_shared_embedder", lambda: backend)

    emb.InProcessEmbedder().embed_for_item("t", "s", "c")

    assert backend.priorities == [PRIORITY_NORMAL]


def test_embed_forwards_priority_to_backend(monkeypatch):
    """The lower-level ``embed`` forwards its priority to ``backend.embed`` too."""
    backend = _FakeBackend()
    monkeypatch.setattr(_embeddings, "get_shared_embedder", lambda: backend)

    inst = emb.InProcessEmbedder()
    inst.embed("hello", priority=PRIORITY_BULK)
    inst.embed("world")  # default

    assert backend.priorities == [PRIORITY_BULK, PRIORITY_NORMAL]
