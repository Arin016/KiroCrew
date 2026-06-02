"""HybridRetriever -- FTS5 keyword + graph + optional vector, fused with RRF."""

from __future__ import annotations

import json
import math
import struct
from collections import defaultdict

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from .store import KnowledgeStore


class HybridRetriever:
    """FTS5 keyword + graph traversal + optional vector search, fused with RRF."""

    def __init__(self, store: KnowledgeStore, embedder=None):
        """store: KnowledgeStore instance. embedder: optional callable(str) -> list[float]."""
        self.store = store
        self.embedder = embedder

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Hybrid search with RRF fusion. Returns [{id, title, summary, content, score, source, match_type}]."""
        kw = self._keyword_search(query, limit=limit * 2)
        gr = self._graph_search(query, limit=limit * 2)
        vec = self._vector_search(query, limit=limit * 2)

        fused = self._rrf_fuse(kw, gr, vec)

        # Batch-fetch all candidate items once
        all_ids = [item_id for item_id, _ in fused]
        items_cache: dict[str, dict] = {}
        for item_id in all_ids:
            item = self.store.get_item(item_id)
            if item:
                items_cache[item_id] = item

        # Tie-break by recency (newer docs win)
        def _sort_key(item_score: tuple[str, float]) -> tuple[float, str]:
            item_id, score = item_score
            updated = items_cache.get(item_id, {}).get("updated_at", "")
            return (score, updated)

        fused.sort(key=_sort_key, reverse=True)

        # Track which lists each item appeared in
        kw_ids = {i for i, _ in kw}
        gr_ids = {i for i, _ in gr}
        vec_ids = {i for i, _ in (vec or [])}

        results = []
        for item_id, score in fused[:limit]:
            item = items_cache.get(item_id)
            if not item:
                continue
            types = []
            if item_id in kw_ids:
                types.append("keyword")
            if item_id in gr_ids:
                types.append("graph")
            if item_id in vec_ids:
                types.append("vector")
            results.append({
                "id": item_id,
                "title": item["title"],
                "summary": item.get("summary"),
                "content": item["content"],
                "score": score,
                "source": item.get("source_id"),
                "match_type": "+".join(types),
            })
        return results

    def _keyword_search(self, query: str, limit: int = 20) -> list[tuple[str, int]]:
        """FTS5 search. Returns [(item_id, rank)] where rank is position (1=best)."""
        safe_query = self._sanitize_fts5_query(query)
        if not safe_query:
            return []
        try:
            rows = self.store.db.execute(
                "SELECT i.id FROM items_fts fts "
                "JOIN items i ON i.rowid = fts.rowid "
                "WHERE items_fts MATCH ? ORDER BY fts.rank LIMIT ?",
                (safe_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(row["id"], rank + 1) for rank, row in enumerate(rows)]

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Escape user input for safe FTS5 MATCH usage."""
        tokens = query.split()
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens if t)

    def _graph_search(self, query: str, limit: int = 20) -> list[tuple[str, int]]:
        """Find entities matching query terms, traverse graph, rank items by mention count."""
        words = query.split()
        # Try individual words and consecutive pairs
        candidates = list(words)
        for i in range(len(words) - 1):
            candidates.append(f"{words[i]} {words[i + 1]}")

        entity_ids = set()
        for term in candidates:
            ent = self.store.find_entity(term)
            if ent:
                entity_ids.add(ent["id"])

        if not entity_ids:
            return []

        # Expand via graph neighbors (depth=2)
        all_entity_ids = set(entity_ids)
        for eid in entity_ids:
            for neighbor in self.store.get_neighbors(eid, depth=2):
                all_entity_ids.add(neighbor["id"])

        # Count item mentions
        item_counts: dict[str, int] = defaultdict(int)
        placeholders = ",".join("?" * len(all_entity_ids))
        rows = self.store.db.execute(
            f"SELECT item_id, COUNT(*) as cnt FROM mentions "  # noqa: S608
            f"WHERE entity_id IN ({placeholders}) GROUP BY item_id ORDER BY cnt DESC LIMIT ?",
            (*all_entity_ids, limit),
        ).fetchall()
        for row in rows:
            item_counts[row["item_id"]] = row["cnt"]

        sorted_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)
        return [(item_id, rank + 1) for rank, (item_id, _) in enumerate(sorted_items)]

    def _vector_search(self, query: str, limit: int = 20) -> list[tuple[str, int]] | None:
        """Brute-force cosine similarity against stored embeddings. Returns None if no embedder."""
        if self.embedder is None:
            return None

        query_vec = self.embedder(query)
        if not query_vec:
            return None
        rows = self.store.db.execute(
            "SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND status = 'active'"
        ).fetchall()

        scored = []
        for row in rows:
            item_vec = _bytes_to_floats(row["embedding"])
            if item_vec:
                sim = self._cosine_similarity(query_vec, item_vec)
                scored.append((row["id"], sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [(item_id, rank + 1) for rank, (item_id, _) in enumerate(scored[:limit])]

    @staticmethod
    def _rrf_fuse(*ranked_lists, k: int = 60) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion across all non-None ranked lists."""
        scores: dict[str, float] = defaultdict(float)
        for rlist in ranked_lists:
            if rlist is None:
                continue
            for item_id, rank in rlist:
                scores[item_id] += 1.0 / (k + rank)
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity. Returns 0.0 for zero vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


def _bytes_to_floats(blob: bytes) -> list[float]:
    """Decode embedding blob (binary struct or JSON-encoded list of floats)."""
    if not blob:
        return []
    try:
        # Try JSON format first (legacy)
        result = json.loads(blob)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        pass
    # Try binary format (struct packed floats, must be >8 bytes to avoid false positives)
    if isinstance(blob, bytes) and len(blob) >= 16 and len(blob) % 4 == 0:
        try:
            n = len(blob) // 4
            return list(struct.unpack(f"{n}f", blob))
        except struct.error:
            pass
    return []
