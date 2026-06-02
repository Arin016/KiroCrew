"""Source watcher -- polls registered local_file sources for changes."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from kiro_claw.security import is_sensitive_path

from .folder_watcher import FolderWatcher
from .ingestion import IngestionPipeline
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

FOLDER_SOURCE_TYPES = {"local_folder", "obsidian_vault"}


class KnowledgeWatcher:
    """Polls registered local_file sources for file changes and re-ingests."""

    def __init__(self, store: KnowledgeStore, pipeline: IngestionPipeline,
                 interval: int = 300):
        self.store = store
        self.pipeline = pipeline
        self.interval = interval
        self._stop_event = asyncio.Event()
        self._folder_watcher = FolderWatcher(store, pipeline)

    async def start(self):
        logger.info("Source watcher started: interval=%ds", self.interval)
        while not self._stop_event.is_set():
            try:
                await self._scan()
            except Exception:
                logger.exception("Source watcher scan failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._stop_event.set()
        logger.info("Source watcher stopped")

    async def _scan(self):
        """Check all watched sources for changes."""
        # Folder sources (local_folder, obsidian_vault)
        folder_rows = self.store.db.execute(
            "SELECT id, uri, source_type, properties FROM sources WHERE source_type IN ({})".format(
                ",".join("?" for _ in FOLDER_SOURCE_TYPES)),
            tuple(FOLDER_SOURCE_TYPES)).fetchall()
        for row in folder_rows:
            try:
                source = dict(row)
                props = self._parse_props(source.get("properties"))
                if props.get("sync_status") in ("paused", "pending_confirmation"):
                    continue
                stats = await self._folder_watcher.scan_source(source)
                if stats.get("error"):
                    logger.warning("Folder scan error for %s: %s", source["uri"], stats["error"])
                elif any(stats.get(k, 0) for k in ("new", "changed", "deleted")):
                    logger.info("Folder scan %s: +%d ~%d -%d", source["uri"],
                                stats.get("new", 0), stats.get("changed", 0), stats.get("deleted", 0))
            except Exception:
                logger.exception("Error scanning folder source %s", row["uri"])

        # Single-file sources (local_file)
        rows = self.store.db.execute(
            "SELECT id, uri, properties FROM sources WHERE source_type = 'local_file'"
        ).fetchall()

        for row in rows:
            try:
                uri = row["uri"]
                if not uri or uri.startswith(("upload://", "code://", "http://", "https://")):
                    continue
                if is_sensitive_path(uri):
                    logger.warning("Skipping sensitive path: %s", uri)
                    continue
                if not Path(uri).exists():
                    # Mark missing
                    props = self._parse_props(row["properties"])
                    if props.get("sync_status") != "missing":
                        props["sync_status"] = "missing"
                        self.store.update_source(row["id"], properties=json.dumps(props))
                    continue

                mtime = os.stat(uri).st_mtime
                props = self._parse_props(row["properties"])
                stored_mtime = props.get("mtime", 0)

                if mtime > stored_mtime:
                    # Check content hash to avoid re-ingesting touched-but-unchanged files
                    content_hash = await asyncio.get_running_loop().run_in_executor(
                        None, self._hash_file, Path(uri))
                    if content_hash != props.get("content_hash"):
                        logger.info("Source changed: %s", uri)
                        await self.pipeline.ingest_file(
                            uri, source_id=row["id"],
                            namespace=props.get("namespace", "default"),
                        )
                        # Re-read props after ingest (ingest may update them)
                        source = self.store.get_source_by_uri(uri)
                        if source:
                            props = self._parse_props(source.get("properties"))
                    props["mtime"] = mtime
                    props["content_hash"] = content_hash
                    self.store.update_source(row["id"], properties=json.dumps(props))
            except Exception:
                logger.exception("Error checking source %s", row.get("uri", row["id"]))

    @staticmethod
    def _parse_props(raw) -> dict:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw or {}

    @staticmethod
    def _hash_file(path: Path) -> str:
        if is_sensitive_path(str(path)):
            raise PermissionError(f"Refusing to hash sensitive path: {path}")
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
