"""Security Event Log — immutable, tamper-evident audit trail for tool invocations.

Records structured JSON events for every tool/MCP action with:
- Timestamp (ISO 8601 UTC)
- Caller identity (session key, agent, source interface)
- Operation type (tool_call, tool_approved, tool_rejected, tool_denied, mcp_call)
- Resources affected (tool name, tool kind, arguments summary)
- Outcome (approved, rejected, denied, completed, failed)
- Downstream service (MCP server name if applicable)
- HMAC-SHA256 integrity chain (each entry signs over previous hash)

Storage: ``~/.kiroclaw/security_events.jsonl`` (append-only JSONL)
Retention: configurable, default 365 days per Amazon Security Event Logging Standard.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".kiroclaw"
_SEL_FILE = "security_events.jsonl"
_RETENTION_DAYS = 365
_HMAC_KEY_FILE = "sel_hmac.key"
_MAX_ARG_LEN = 500


@dataclass
class SecurityEvent:
    """A single auditable security event."""

    event_id: str
    timestamp: str  # ISO 8601 UTC
    event_type: str  # tool_invocation, tool_approval, tool_denial, mcp_call, api_access
    caller_identity: str  # session key or user identifier
    agent: str  # agent name (kiroclaw, custom, etc.)
    source: str  # slack, dashboard, cli, cron, subagent, taskrunner, background
    operation: str  # tool name or API operation
    tool_kind: str = ""  # execute_bash, fs_write, mcp, etc.
    outcome: str = ""  # approved, rejected, denied, completed, failed
    resources: str = ""  # affected resources summary (truncated)
    downstream_service: str = ""  # MCP server name if applicable
    request_id: str = ""  # ACP permission request ID
    error: str = ""
    prev_hash: str = ""  # HMAC chain — hash of previous entry
    entry_hash: str = ""  # HMAC of this entry (computed on write)
    metadata: dict = field(default_factory=dict)


class SecurityEventLog:
    """Append-only, HMAC-chained security event log.

    Thread-safe. Singleton pattern — all callers share one instance.
    """

    _instance: SecurityEventLog | None = None
    _init_lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, base_dir: Path | None = None) -> SecurityEventLog:
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self, base_dir: Path | None = None) -> None:
        if self._initialized:
            return
        self._dir = base_dir or _DEFAULT_DIR
        self._path = self._dir / _SEL_FILE
        self._lock = threading.Lock()
        self._hmac_key = self._load_or_create_hmac_key()
        self._last_hash = self._read_last_hash()
        self._forward_callback: Callable[[dict], None] | None = None
        self._initialized = True

    def set_forward_callback(self, callback: Callable[[dict], None] | None) -> None:
        """Register an optional callback to forward events to a centralized log system."""
        with self._lock:
            self._forward_callback = callback

    def _load_or_create_hmac_key(self) -> bytes:
        key_path = self._dir / _HMAC_KEY_FILE
        self._dir.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            return key_path.read_bytes()
        key = os.urandom(32)
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key

    def _read_last_hash(self) -> str:
        if not self._path.exists():
            return ""
        try:
            # Read last non-empty line
            with open(self._path, "rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                if pos == 0:
                    return ""
                # Scan backward for last newline
                buf = b""
                while pos > 0:
                    pos = max(pos - 4096, 0)
                    f.seek(pos)
                    buf = f.read() + buf
                    lines = buf.split(b"\n")
                    for line in reversed(lines):
                        line = line.strip()
                        if line:
                            data = json.loads(line)
                            return data.get("entry_hash", "")
            return ""
        except Exception:
            return ""

    def _compute_hash(self, event: SecurityEvent) -> str:
        # Hash over all fields except entry_hash itself
        d = asdict(event)
        d.pop("entry_hash", None)
        payload = json.dumps(d, sort_keys=True).encode()
        return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()

    def log(self, event: SecurityEvent) -> None:
        """Append an event to the log with HMAC chain integrity."""
        with self._lock:
            event.prev_hash = self._last_hash
            event.entry_hash = self._compute_hash(event)
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event)) + "\n")
            self._last_hash = event.entry_hash
            callback = self._forward_callback
        if callback:
            try:
                from kiro_claw.security import redact

                def _redact_deep(obj: object) -> object:
                    if isinstance(obj, str):
                        return redact(obj)
                    if isinstance(obj, dict):
                        return {k: _redact_deep(v) for k, v in obj.items()}
                    if isinstance(obj, (list, tuple)):
                        return type(obj)(_redact_deep(i) for i in obj)
                    return obj

                callback(_redact_deep(asdict(event)))  # type: ignore[arg-type]
            except Exception:
                logger.warning("forward_callback failed", exc_info=True)

    def log_tool_invocation(
        self,
        *,
        session_key: str,
        agent: str = "kiroclaw",
        source: str = "",
        tool_name: str,
        tool_kind: str = "",
        outcome: str,
        request_id: str | int = "",
        downstream_service: str = "",
        resources: str = "",
        error: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Convenience: log a tool invocation event."""
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="tool_invocation",
                caller_identity=session_key,
                agent=agent,
                source=source or _infer_source(session_key),
                operation=tool_name,
                tool_kind=tool_kind,
                outcome=outcome,
                request_id=str(request_id),
                downstream_service=downstream_service,
                resources=resources[:_MAX_ARG_LEN] if resources else "",
                error=error[:_MAX_ARG_LEN] if error else "",
                metadata=metadata or {},
            )
        )

    def log_api_access(
        self,
        *,
        caller: str,
        operation: str,
        outcome: str,
        source: str = "dashboard",
        resources: str = "",
        error: str = "",
    ) -> None:
        """Convenience: log a dashboard/API access event."""
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="api_access",
                caller_identity=caller,
                agent="",
                source=source,
                operation=operation,
                outcome=outcome,
                resources=resources[:_MAX_ARG_LEN] if resources else "",
                error=error[:_MAX_ARG_LEN] if error else "",
            )
        )

    def verify_integrity(self) -> tuple[int, int]:
        """Verify HMAC chain. Returns (total_entries, valid_entries)."""
        if not self._path.exists():
            return 0, 0
        total = 0
        valid = 0
        prev_hash = ""
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                data = json.loads(line)
                stored_hash = data.pop("entry_hash", "")
                if data.get("prev_hash", "") != prev_hash:
                    logger.warning("SEL chain break at entry %d", total)
                    prev_hash = stored_hash
                    continue
                payload = json.dumps(data, sort_keys=True).encode()
                expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(stored_hash, expected):
                    valid += 1
                else:
                    logger.warning("SEL HMAC mismatch at entry %d", total)
                prev_hash = stored_hash
            except (json.JSONDecodeError, Exception):
                logger.warning("SEL parse error at entry %d", total)
        return total, valid

    def recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent events."""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        result = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(result) >= limit:
                break
        return result

    def prune(self, keep_days: int = _RETENTION_DAYS) -> int:
        """Remove entries older than keep_days. Returns count removed."""
        if not self._path.exists():
            return 0
        from datetime import timedelta

        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
        cutoff_str = cutoff_dt.isoformat()

        kept_lines: list[str] = []
        removed = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("timestamp", "") < cutoff_str:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                removed += 1
                continue
            kept_lines.append(line)

        if removed:
            with self._lock:
                from kiro_claw.atomic_write import atomic_write

                atomic_write(self._path, "\n".join(kept_lines) + "\n" if kept_lines else "")
                self._last_hash = self._read_last_hash()
            logger.info("SEL pruned %d entries older than %d days", removed, keep_days)
        return removed


def _infer_source(session_key: str) -> str:
    """Infer the source interface from a session key."""
    if session_key.startswith("dashboard:"):
        return "dashboard"
    if session_key.startswith("cron:"):
        return "cron"
    if session_key.startswith("subagent:"):
        return "subagent"
    if session_key.startswith("taskrunner"):
        return "taskrunner"
    if session_key == "_bg":
        return "background"
    if session_key == "cli_chat":
        return "cli"
    return "slack"


def sel() -> SecurityEventLog:
    """Module-level accessor for the singleton SEL instance."""
    return SecurityEventLog()
