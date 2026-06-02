"""Ingestion pipeline -- orchestrates read -> chunk -> extract -> store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from kiro_claw.security import redact_credentials, redact_exfiltration_urls

from .chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE, HeadingAwareChunker
from .embedder import floats_to_bytes
from .extractor import EntityExtractor
from .readers import FileReader
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

CODE_EXTS = {
    '.py', '.java', '.ts', '.js', '.rs', '.go', '.rb', '.c', '.cpp', '.h',
    '.sh', '.cs', '.kt', '.swift', '.scala',
}

MARKDOWN_EXTS = {'.md', '.docx'}


def _redact(text: str | None) -> str | None:
    """Redact LLM-derived text before storing."""
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _job_status(processed: int, total: int) -> str:
    if processed == 0 and total > 0:
        return 'failed'
    if processed < total:
        return 'partial'
    return 'completed'


def _first_line_title(content: str) -> str:
    """Extract title from first non-empty line, stripped of markdown markers."""
    for line in content.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return ""


class IngestionPipeline:
    """Orchestrates: read file -> chunk -> extract entities -> store."""

    def __init__(self, store: KnowledgeStore, extractor: EntityExtractor,
                 chunker: HeadingAwareChunker, reader: FileReader, embedder=None):
        self.store = store
        self.extractor = extractor
        self.chunker = chunker
        self.reader = reader
        self.embedder = embedder

    async def ingest_file(self, path: str, on_progress=None, original_name: str = "", namespace: str = "default", source_id: str = "", old_item_ids: list[str] | None = None) -> str | None:
        """Full pipeline. Returns job_id, or None if content hash unchanged.

        If source_id is provided, ingests into that existing source instead of
        creating a new one (used for remote source sync).
        If old_item_ids is provided, only those items are replaced (folder sources).
        Otherwise all items for the source are replaced (single-file sources).
        """
        p = Path(path)
        display_name = original_name or p.name
        ext = (Path(original_name).suffix if original_name else p.suffix).lower()

        # 1. Read
        if on_progress:
            on_progress('reading', 0, 1)
        text, meta = self.reader.read(path)
        if meta.get('format') == 'error':
            raise RuntimeError(f"Failed to read {path}: {meta.get('error')}")

        # 2. Hash check + source resolution
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        _old_item_ids: list[str] = old_item_ids if old_item_ids is not None else []
        if source_id:
            # Ingest into existing source (remote sync path)
            if old_item_ids is None:
                # Single-file/remote source: replace all items for this source
                _old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            props: dict[str, object] = {}
            existing: dict | None = {"id": source_id}  # sentinel — source already exists
            src_row = self.store.db.execute(
                "SELECT uri, properties FROM sources WHERE id = ?", (source_id,)).fetchone()
            uri = src_row["uri"] if src_row else display_name
            props = json.loads(src_row["properties"] or "{}") if src_row else {}
        else:
            # Local file path: find or create source by URI
            uri = str(p.resolve())
            existing = self.store.get_source_by_uri(uri)
            if existing:
                props = json.loads(existing.get('properties', '{}')) if isinstance(existing.get('properties'), str) else existing.get('properties', {})
                if props.get('content_hash') == content_hash:
                    return None
                source_id = existing['id']
                _old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            else:
                props = {}
                source_id = self.store.add_source(
                    name=display_name, source_type='local_file', uri=uri,
                    properties={'content_hash': content_hash, **meta},
                )

        # 3. Job record
        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) VALUES (?, ?, 'processing', ?, ?)",
            (job_id, source_id, now, now))
        self.store.db.commit()

        # 4. Chunk (use per-source chunk size if configured)
        chunker = self.chunker
        chunk_size = props.get("chunk_size")
        chunk_overlap = props.get("chunk_overlap")
        if chunk_size or chunk_overlap:
            chunker = HeadingAwareChunker(
                target_size=int(chunk_size) if isinstance(chunk_size, (int, float, str)) else CHUNK_TOKEN_SIZE,
                overlap=int(chunk_overlap) if isinstance(chunk_overlap, (int, float, str)) else CHUNK_OVERLAP,
            )
        is_markdown = ext in MARKDOWN_EXTS
        if ext == '.pptx':
            chunks = chunker.chunk_slides(text)
        elif ext in CODE_EXTS:
            chunks = chunker.chunk_code(text, language=ext.lstrip('.'))
        elif is_markdown:
            chunks = chunker.chunk_markdown(text)
        else:
            chunks = chunker.chunk(text, source_uri=uri)

        total = len(chunks)
        self.store.db.execute("UPDATE ingestion_jobs SET items_total = ? WHERE id = ?", (total, job_id))
        self.store.db.commit()

        # 5. Extract all chunks in batch via pool
        # Capture existing items before ingestion (for safe partial-failure rollback)
        _before_ids = {r["id"] for r in self.store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}
        chunk_contents = [chunk['content'] for chunk in chunks]
        extractions = await self.extractor.extract_batch(chunk_contents)

        processed = 0
        for i, (chunk, extraction) in enumerate(zip(chunks, extractions)):
            try:
                extraction['summary'] = _redact(extraction.get('summary'))
                for ent in extraction.get('entities', []):
                    ent['name'] = _redact(ent.get('name')) or ''
                    ent['description'] = _redact(ent.get('description'))
                item_title = (
                    _redact(extraction.get('title'))
                    or _first_line_title(chunk['content'])
                    or f"{Path(display_name).stem} chunk {i}"
                )
                item_tags = ['content_type:markdown'] if is_markdown else None
                item_id = self.store.add_item(
                    title=item_title,
                    content=chunk['content'],
                    item_type=extraction.get('category', 'document'),
                    source_id=source_id,
                    chunk_index=chunk.get('chunk_index', i),
                    summary=extraction.get('summary'),
                    namespace=namespace,
                    tags=item_tags,
                )
                self.store.add_source_location(
                    item_id=item_id, source_id=source_id,
                    chunk_range=f"{chunk.get('line_start', 0)}-{chunk.get('line_end', 0)}",
                    section_title=chunk.get('section_title'),
                )
                self._store_entities(extraction, item_id)
                await self._embed_item(item_id, item_title, extraction.get('summary'))
                processed += 1
            except Exception:
                logger.exception("Failed to process chunk %d of %s", i, path)
            if on_progress:
                on_progress('extracting', i + 1, total)

        # 6. Finalize
        now = datetime.now().isoformat()
        if processed == total:
            self.store.delete_items_batch(_old_item_ids)
            if existing:
                self.store.update_source(source_id, properties=json.dumps({**props, 'content_hash': content_hash, **meta}))
            self.store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
            self.store.update_source(source_id, last_synced=now)
            # Generate file-level summary from chunk summaries
            try:
                await self.generate_source_summary(source_id)
            except Exception:
                logger.debug("Source summary generation skipped for %s", source_id, exc_info=True)
        elif processed < total:
            # Partial failure: remove only items created during THIS ingestion call
            after_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,),
            ).fetchall()}
            new_ids = list(after_ids - _before_ids)
            self.store.delete_items_batch(new_ids)
            self.store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
        self.store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, items_processed = ?, updated_at = ? WHERE id = ?",
            (_job_status(processed, total), processed, now, job_id))
        self.store.db.commit()
        return job_id

    async def ingest_text(self, text: str, title: str, source_type: str = 'manual',
                          source_id: str | None = None) -> str | None:
        """Ingest raw text (dashboard drop or chat). Skips file reading."""
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        uri = f"{source_type}://{content_hash[:16]}"

        # Dedup: check if source already exists
        old_item_ids: list[str] = []
        if source_id is None:
            existing = self.store.get_source_by_uri(uri)
            if existing:
                props = json.loads(existing.get('properties', '{}')) if isinstance(existing.get('properties'), str) else existing.get('properties', {})
                if props.get('content_hash') == content_hash:
                    return None  # unchanged
                source_id = existing['id']
                old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            else:
                source_id = self.store.add_source(
                    name=title, source_type=source_type, uri=uri,
                    properties={'content_hash': content_hash},
                )
        else:
            # source_id provided externally (e.g. sync) — collect old items for replacement
            old_item_ids = [row['id'] for row in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]

        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) VALUES (?, ?, 'processing', ?, ?)",
            (job_id, source_id, now, now))
        self.store.db.commit()

        chunks = self.chunker.chunk(text)
        total = len(chunks)

        chunk_contents = [chunk['content'] for chunk in chunks]
        extractions = await self.extractor.extract_batch(chunk_contents)

        processed = 0
        for i, (chunk, extraction) in enumerate(zip(chunks, extractions)):
            try:
                extraction['summary'] = _redact(extraction.get('summary'))
                for ent in extraction.get('entities', []):
                    ent['name'] = _redact(ent.get('name')) or ''
                    ent['description'] = _redact(ent.get('description'))
                item_id = self.store.add_item(
                    title=chunk.get('section_title') or f"{title} chunk {i}",
                    content=chunk['content'],
                    item_type=extraction.get('category', 'document'),
                    source_id=source_id,
                    chunk_index=chunk.get('chunk_index', i),
                    summary=extraction.get('summary'),
                )
                self.store.add_source_location(
                    item_id=item_id, source_id=source_id,
                    chunk_range=f"{chunk.get('line_start', 0)}-{chunk.get('line_end', 0)}",
                    section_title=chunk.get('section_title'),
                )
                self._store_entities(extraction, item_id)
                await self._embed_item(item_id, chunk.get('section_title') or f"{title} chunk {i}", extraction.get('summary'))
                processed += 1
            except Exception:
                logger.exception("Failed to process chunk %d of text '%s'", i, title)

        now = datetime.now().isoformat()
        if processed == total:
            self.store.delete_items_batch(old_item_ids)
            self.store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
            self.store.update_source(source_id, last_synced=now)
            try:
                await self.generate_source_summary(source_id)
            except Exception:
                logger.debug("Source summary generation skipped for %s", source_id, exc_info=True)
        elif processed < total:
            # Partial failure: remove newly created items to avoid duplicates
            new_ids = [r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,),
            ).fetchall() if r["id"] not in set(old_item_ids)]
            self.store.delete_items_batch(new_ids)
            self.store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
        self.store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, items_total = ?, items_processed = ?, updated_at = ? WHERE id = ?",
            (_job_status(processed, total), total, processed, now, job_id))
        self.store.db.commit()
        return job_id

    def get_job_status(self, job_id: str) -> dict | None:
        row = self.store.db.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _store_entities(self, extraction: dict, item_id: str):
        """Deduplicate and store entities, mentions, and relations."""
        entity_map: dict[str, str] = {}  # name -> entity_id
        for ent in extraction.get('entities', []):
            name = ent.get('name', '').strip()
            if not name:
                continue
            existing = self.store.find_entity(name)
            if existing:
                eid = existing['id']
            else:
                eid = self.store.add_entity(
                    name=name,
                    entity_type=ent.get('type', 'concept'),
                    description=ent.get('description'),
                )
            entity_map[name] = eid
            self.store.add_mention(item_id, eid, context=ent.get('description'))

        for rel in extraction.get('relations', []):
            src_name = rel.get('source', '').strip()
            tgt_name = rel.get('target', '').strip()
            src_id = entity_map.get(src_name)
            tgt_id = entity_map.get(tgt_name)
            if src_id and tgt_id:
                self.store.add_entity_relation(
                    source_id=src_id, target_id=tgt_id,
                    relation_type=_redact(rel.get('type', 'uses')) or 'uses',
                    description=_redact(rel.get('description')),
                    source_item_id=item_id,
                )

    async def _embed_item(self, item_id: str, title: str, summary: str | None) -> None:
        """Generate and store embedding for an item. No-op if embedder is None."""
        if not self.embedder:
            return
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(None, self.embedder.embed_for_item, title, summary)
        if vec:
            self.store.db.execute(
                "UPDATE items SET embedding = ? WHERE id = ?",
                (floats_to_bytes(vec), item_id))
            self.store.db.commit()

    async def generate_source_summary(self, source_id: str) -> None:
        """Generate a file-level summary from chunk summaries via LLM pool. No-op if pool unavailable."""
        if not self.extractor._pool:
            return
        rows = self.store.db.execute(
            "SELECT summary FROM items WHERE source_id = ? AND summary IS NOT NULL AND summary != '' ORDER BY chunk_index",
            (source_id,)).fetchall()
        if not rows:
            return
        chunk_summaries = "\n".join(r["summary"] for r in rows)
        # Cap input to avoid token overflow (~2000 tokens max)
        if len(chunk_summaries) > 4000:
            chunk_summaries = chunk_summaries[:4000]
        prompt = (
            "Given these section summaries from a document, produce a JSON object with:\n"
            '- "topic": a single sentence (max 30 words) describing the document\n'
            '- "themes": an array of 3-5 short theme tags\n\n'
            f"Sections:\n{chunk_summaries}\n\n"
            "Respond with ONLY the JSON object, no markdown."
        )
        try:
            response = await self.extractor._pool.send(prompt, timeout=30.0)
            # Parse JSON from response
            m = re.search(r'\{[\s\S]*\}', response)
            if m:
                data = json.loads(m.group())
                topic = _redact(data.get("topic", ""))
                themes = json.dumps([r for t in data.get("themes", [])[:5] if (r := _redact(t))])
                self.store.db.execute(
                    "UPDATE sources SET summary_topic = ?, summary_themes = ? WHERE id = ?",
                    (topic, themes, source_id))
                self.store.db.commit()
        except Exception:
            logger.debug("Source summary generation failed for %s", source_id, exc_info=True)
