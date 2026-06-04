"""Auto-research backend — campaign CRUD, validation, stagnation, file-based interface."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_claw.autonudge import get_instance as _autonudge_instance
from kiro_claw.knowledge.llm_pool import LLMPool

try:
    from kiro_claw.security import redact_credentials, redact_exfiltration_urls

    _HAS_SECURITY = True
except ImportError:
    _HAS_SECURITY = False

try:
    from kiro_claw.sel import sel
except ImportError:
    sel = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RESEARCH_DIR = Path.home() / ".kiroclaw" / "workspace" / "research"
DB_PATH = Path.home() / ".kiroclaw" / "apps" / "auto-research" / "campaigns.db"
MAX_CYCLES_HARD_CAP = 100
POLL_INTERVAL = 5
# Default seconds between cycles (until the next nudge fires). The watchdog's
# inactivity timeout is idle_secs * 2; the first cycle gets a longer startup
# grace (it can't produce anything until the first nudge + a full work turn).
DEFAULT_IDLE_SECS = 120
_FIRST_CYCLE_GRACE_SECS = 600
# Worker auto-approve is capped at 24h; past this the watchdog pauses the
# campaign to NEEDS_INPUT and it must be resumed (re-authorized) to continue.
_TRUST_TTL_SECS = 24 * 3600
_CAMPAIGN_ID_RE = re.compile(r"^[a-f0-9]{8}$")


def _unresponsive_deadline(idle_secs: int) -> int:
    """Idle seconds (no slot activity AND no new finding) before unresponsive.

    Generous floor: a deep research cycle can take minutes (web fetches +
    synthesis), so a tight idle_secs*2 window falsely fails healthy slow cycles.
    The watchdog also resets this timer whenever the worker slot is actively
    running a turn, so this only bounds genuine no-activity stalls.
    """
    return max(idle_secs * 2, _FIRST_CYCLE_GRACE_SECS)


class CampaignStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STAGNANT = "stagnant"
    NEEDS_INPUT = "needs_input"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


# Terminal statuses cannot transition to any other status.
_TERMINAL_STATUSES = (CampaignStatus.COMPLETE, CampaignStatus.STOPPED)

# Per-cycle trigger injected by the autonudge loop. The full methodology lives in
# the kiroclaw-research agent's system prompt, so this only needs to name the cycle.
_RESEARCH_AGENT = "kiroclaw-research"
_RESEARCH_NUDGE = (
    "Run the next research cycle for campaign {cid} "
    "(dir ~/.kiroclaw/workspace/research/{cid}/). Follow your per-cycle research "
    "protocol and end the turn when done."
)


# --- Path safety ---


def _validate_campaign_id(campaign_id: str) -> bool:
    """Reject IDs that could cause path traversal."""
    return bool(_CAMPAIGN_ID_RE.match(campaign_id))


def _safe_campaign_dir(campaign_id: str) -> Path | None:
    """Return campaign dir only if it resolves within RESEARCH_DIR."""
    if not _validate_campaign_id(campaign_id):
        return None
    d = (RESEARCH_DIR / campaign_id).resolve()
    if not d.is_relative_to(RESEARCH_DIR.resolve()):
        return None
    return d


# --- Database ---


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
        sub_questions TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]',
        max_cycles INTEGER NOT NULL DEFAULT 30, idle_secs INTEGER NOT NULL DEFAULT 120,
        status TEXT NOT NULL DEFAULT 'ready',
        created_at REAL NOT NULL, started_at REAL, completed_at REAL,
        total_cycles INTEGER NOT NULL DEFAULT 0, error_message TEXT,
        success_criteria TEXT, auto_approve INTEGER NOT NULL DEFAULT 0)"""
    )
    # Migrate DBs created before later columns were added.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    if "success_criteria" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN success_criteria TEXT")
    if "auto_approve" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


# --- Redaction ---


def _redact_finding(finding: dict) -> dict:
    """Redact credentials and exfiltration URLs from finding data."""
    if not _HAS_SECURITY:
        # Fail-closed: recursively mask every string value (incl. nested
        # lists/dicts) when the security module is unavailable.
        def _mask(val: Any) -> Any:
            if isinstance(val, str):
                return "[REDACTED]"
            if isinstance(val, list):
                return [_mask(item) for item in val]
            if isinstance(val, dict):
                return {k: _mask(v) for k, v in val.items()}
            return val

        return {k: _mask(v) for k, v in finding.items()}

    def _redact_str(s: str) -> str:
        cleaned, _ = redact_credentials(s)
        cleaned, _ = redact_exfiltration_urls(cleaned)
        return cleaned

    def _redact_value(val: Any) -> Any:
        if isinstance(val, str):
            return _redact_str(val)
        elif isinstance(val, list):
            return [_redact_value(item) for item in val]
        elif isinstance(val, dict):
            return {k2: _redact_value(v2) for k2, v2 in val.items()}
        return val

    return {k: _redact_value(v) for k, v in finding.items()}


# --- SEL audit ---


def _audit(operation: str, campaign_id: str, **extra: Any) -> None:
    """Emit SEL audit event for campaign lifecycle actions."""
    if sel is None:
        logger.warning(
            "SEL module unavailable — audit event for %s/%s not recorded",
            operation,
            campaign_id,
        )
        return
    try:
        sel().log_api_access(
            caller="auto_research",
            operation=operation,
            outcome="success",
            resources=campaign_id,
            **extra,
        )
    except Exception as exc:
        logger.warning("SEL audit failed for %s/%s: %s", operation, campaign_id, exc)


# --- Validation ---


def validate_campaign(config: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(config.get("question", "")) < 20:
        errors.append("Question too vague — provide more context (min 20 characters)")
    if len(config.get("sub_questions", [])) < 2:
        warnings.append("Consider decomposing into sub-questions for better coverage")
    if not config.get("sources"):
        errors.append("Select at least one source type")

    max_cycles = config.get("max_cycles", 30)
    if max_cycles > MAX_CYCLES_HARD_CAP:
        errors.append(f"Max cycles cannot exceed {MAX_CYCLES_HARD_CAP}")
    elif max_cycles > 50:
        low, high = max_cycles * 0.10, max_cycles * 0.30
        warnings.append(
            f"High cycle count ({max_cycles}). " f"Estimated cost: ~${low:.2f}–${high:.2f}"
        )

    db = _get_db()
    active = db.execute(
        "SELECT id, name FROM campaigns WHERE status IN (?, ?, ?, ?)",
        (
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
        ),
    ).fetchone()
    db.close()
    if active:
        clean_name = _redact_finding({"v": active["name"]})["v"]
        errors.append(f"Campaign '{clean_name}' is already active. Stop it first.")

    return {
        "can_start": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "estimated_cycles": max_cycles,
        "estimated_duration_min": max_cycles * 2,
    }


# --- Stagnation ---


def check_stagnation(campaign_id: str) -> bool:
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return False
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return False
    files = sorted(findings_dir.glob("cycle_*.json"))
    if len(files) < 5:
        return False
    for f in files[-5:]:
        try:
            if json.loads(f.read_text()).get("new_findings_count", 0) > 0:
                return False
        except (json.JSONDecodeError, OSError):
            return False
    return True


# --- File interface ---


def _campaign_dir(campaign_id: str) -> Path:
    """Create and return campaign dir. Only call with validated IDs."""
    d = RESEARCH_DIR / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "findings").mkdir(exist_ok=True)
    return d


def _questions_path(campaign_id: str) -> Path | None:
    """Path to the agent's pending clarification question (if any)."""
    d = _safe_campaign_dir(campaign_id)
    return (d / "questions.json") if d else None


def _pending_question(campaign_id: str) -> str | None:
    """Read the agent's pending clarification question text, if present."""
    p = _questions_path(campaign_id)
    if not p or not p.exists():
        return None
    try:
        return str(json.loads(p.read_text()).get("question", "")) or None
    except (json.JSONDecodeError, OSError):
        return None


def write_status(campaign_id: str, status: str, **extra: Any) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    (d / "status.json").write_text(
        json.dumps(
            {"status": status, "campaign_id": campaign_id, "ts": time.time(), **extra},
            indent=2,
        )
    )


def write_guidance(campaign_id: str, text: str) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    (d / "guidance.txt").write_text(text)


def get_findings(campaign_id: str) -> list[dict]:
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return []
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return []
    results = []
    for f in sorted(findings_dir.glob("cycle_*.json")):
        try:
            results.append(_redact_finding(json.loads(f.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return results


# --- CRUD ---


def create_campaign(config: dict) -> dict:
    campaign_id = uuid.uuid4().hex[:8]
    name = config.get("name") or config["question"][:50].strip()
    db = _get_db()
    db.execute(
        "INSERT INTO campaigns (id,name,question,sub_questions,sources,"
        "max_cycles,idle_secs,success_criteria,auto_approve,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            campaign_id,
            name,
            config["question"],
            json.dumps(config.get("sub_questions", [])),
            json.dumps(config.get("sources", [])),
            config.get("max_cycles", 30),
            config.get("idle_secs", DEFAULT_IDLE_SECS),
            config.get("success_criteria") or None,
            int(bool(config.get("auto_approve", False))),
            CampaignStatus.READY,
            time.time(),
        ),
    )
    db.commit()
    db.close()
    write_status(campaign_id, CampaignStatus.READY)
    _audit("campaign_created", campaign_id)
    return {"id": campaign_id, "name": name, "status": CampaignStatus.READY}


def update_campaign_status(campaign_id: str, new_status: str, **kwargs: Any) -> dict:
    if not _validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    db = _get_db()
    row = db.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        db.close()
        return {"error": "campaign not found"}
    current = row["status"]
    if current in _TERMINAL_STATUSES and new_status != current:
        db.close()
        return {"error": f"invalid transition: {current} -> {new_status}"}
    sets: list[str] = ["status = ?"]
    vals: list[Any] = [new_status]
    if new_status == CampaignStatus.RUNNING:
        sets.append("started_at = ?")
        vals.append(time.time())
        kwargs.setdefault("error_message", None)  # clear stale failure on (re)start
    if new_status in (CampaignStatus.COMPLETE, CampaignStatus.STOPPED, CampaignStatus.FAILED):
        sets.append("completed_at = ?")
        vals.append(time.time())
    if "error_message" in kwargs:
        sets.append("error_message = ?")
        vals.append(kwargs["error_message"])
    vals.append(campaign_id)
    db.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    db.close()
    write_status(campaign_id, new_status, **kwargs)
    _audit(f"campaign_{new_status}", campaign_id)
    return {"id": campaign_id, "status": new_status}


def _redact_campaign(campaign: dict) -> dict:
    """Redact user/LLM-generated fields in campaign metadata."""
    for field in ("question", "name", "error_message", "success_criteria", "pending_question"):
        if isinstance(campaign.get(field), str):
            campaign[field] = _redact_finding({"v": campaign[field]})["v"]
    # sub_questions/sources are JSON-encoded lists — decode, redact, re-encode.
    for field in ("sub_questions", "sources"):
        raw = campaign.get(field)
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            campaign[field] = json.dumps(_redact_finding({"v": items})["v"])
    return campaign


def get_campaign(campaign_id: str) -> dict | None:
    if not _validate_campaign_id(campaign_id):
        return None
    db = _get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    if not row:
        return None
    return _redact_campaign(
        {
            **dict(row),
            "findings": get_findings(campaign_id),
            "pending_question": _pending_question(campaign_id),
        }
    )


def list_campaigns() -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
    db.close()
    return [_redact_campaign(dict(r)) for r in rows]


def delete_campaign(campaign_id: str) -> dict:
    """Delete a campaign's DB row and its research dir (findings + report)."""
    if not _validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    db = _get_db()
    rows = db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,)).rowcount
    db.commit()
    db.close()
    if rows == 0:
        return {"error": "campaign not found"}
    d = _safe_campaign_dir(campaign_id)
    if d and d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return {"id": campaign_id, "deleted": True}


# --- Watchdog ---

_watchdog_task: asyncio.Task | None = None
_SSE_QUEUE_MAXSIZE = 256
_sse_queues: list[asyncio.Queue] = []


def _emit_sse(event: dict) -> None:
    for q in _sse_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Drop events for slow consumers


def _should_pause_for_question(cid: str, auto_approve: bool) -> bool:
    """Decide what to do with a pending questions.json.

    Returns True only when the campaign should pause to NEEDS_INPUT (attended
    mode with a question waiting). Unattended mode NEVER pauses: any stray
    question (the agent was not given a questions directive) is discarded so
    "unattended" is a code-enforced guarantee, not reliant on the LLM obeying
    a prompt. Returns False when there's no question or it was discarded.
    """
    qp = _questions_path(cid)
    if not (qp and qp.exists()):
        return False
    if auto_approve:
        qp.unlink(missing_ok=True)
        _audit("campaign_unattended_question_discarded", cid)
        return False
    return True


async def _watchdog_loop(app: web.Application | None = None) -> None:
    state = app.get("state") if app is not None else None
    last_counts: dict[str, int] = {}
    last_ts: dict[str, float] = {}
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            db = _get_db()
            active = db.execute(
                "SELECT id, idle_secs, max_cycles, started_at, auto_approve FROM campaigns WHERE status = ?",
                (CampaignStatus.RUNNING,),
            ).fetchall()
            db.close()
            for row in active:
                cid = row["id"]
                slot = state._slots.get(f"research-{cid}") if state is not None else None
                # 24h auto-approve cap: expire trust and require re-authorization.
                started = row["started_at"]
                if started and time.time() - started > _TRUST_TTL_SECS:
                    if slot is not None:
                        slot._trust = False
                    qpath = _questions_path(cid)
                    if qpath:
                        qpath.write_text(
                            json.dumps(
                                {
                                    "question": "Auto-approval expired after 24h. Resume to "
                                    "re-authorize and continue."
                                }
                            )
                        )
                    update_campaign_status(cid, CampaignStatus.NEEDS_INPUT)
                    _audit("campaign_trust_expired", cid)
                    _emit_sse({"type": "needs_input", "campaign_id": cid})
                    continue
                # Re-establish worker trust each cycle (restart-durable; bounded above).
                if slot is not None and not slot._trust:
                    slot._trust = True
                    _audit("campaign_trust_reestablished", cid)
                # Attended: pause for the user. Unattended: discard the stray
                # question + keep running (code-enforced; see helper).
                if _should_pause_for_question(cid, bool(row["auto_approve"])):
                    update_campaign_status(cid, CampaignStatus.NEEDS_INPUT)
                    _emit_sse({"type": "needs_input", "campaign_id": cid})
                    continue
                findings = get_findings(cid)
                count = len(findings)
                if cid not in last_counts or last_ts.get(cid, 0.0) < (started or 0):
                    # Seed on first observation, AND re-seed after a (re)start:
                    # a resumed campaign (NEEDS_INPUT/paused -> running) refreshes
                    # started_at, so a stale pre-pause last_ts must not instantly
                    # trip the unresponsive deadline the moment it resumes.
                    last_counts[cid] = count
                    last_ts[cid] = time.time()
                    continue
                prev = last_counts[cid]
                if count > prev:
                    last_counts[cid] = count
                    last_ts[cid] = time.time()
                    _emit_sse({"type": "new_finding", "campaign_id": cid, "finding": findings[-1]})
                    db2 = _get_db()
                    db2.execute(
                        "UPDATE campaigns SET total_cycles=? WHERE id=?",
                        (count, cid),
                    )
                    db2.commit()
                    db2.close()
                    verified = findings[-1].get("verification")
                    if isinstance(verified, dict) and verified.get("passed") is True:
                        update_campaign_status(cid, CampaignStatus.COMPLETE)
                        _emit_sse({"type": "complete", "campaign_id": cid})
                    elif count >= row["max_cycles"]:
                        update_campaign_status(cid, CampaignStatus.COMPLETE)
                        _emit_sse({"type": "complete", "campaign_id": cid})
                    elif check_stagnation(cid):
                        update_campaign_status(cid, CampaignStatus.STAGNANT)
                        _emit_sse({"type": "stagnant", "campaign_id": cid})
                elif cid in last_ts:
                    if slot is not None and slot.running:
                        # Agent is actively working this cycle (deep research can
                        # take minutes) — alive, not unresponsive. Refresh liveness.
                        last_ts[cid] = time.time()
                    elif time.time() - last_ts[cid] > _unresponsive_deadline(row["idle_secs"]):
                        update_campaign_status(
                            cid,
                            CampaignStatus.FAILED,
                            error_message="No activity — research stalled. Resume to continue.",
                        )
                        await _stop_loop(cid, remove=True)  # tear down so Resume re-arms cleanly
                        last_counts.pop(cid, None)
                        last_ts.pop(cid, None)
                        _emit_sse({"type": "failed", "campaign_id": cid})
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("auto_research watchdog error")
            await asyncio.sleep(POLL_INTERVAL)


# --- Auth helper ---


def _require_auth(request: web.Request) -> web.Response | None:
    """Defense-in-depth auth check. Returns 401 response if unauthorized, None if OK.

    Primary auth is enforced by the gateway _auth_middleware in server.py which
    validates tokens against the session store and sets request["user"] on
    success. This check rejects any request where middleware did not run (e.g.
    misconfigured proxy bypass) — we trust only the middleware-set user, never
    a raw token string, to avoid a fail-open bypass.
    """
    if request.get("user") is not None:
        return None
    return web.json_response({"error": "Unauthorized"}, status=401)


# --- Campaign worker loop (autonudge-backed) ---


async def _launch_loop(request: web.Request, cid: str) -> None:
    """Arm an autonudge loop that drives the research cycles for this campaign.

    Best-effort: if autonudge or dashboard state is unavailable, the status
    change still stands but no worker is launched (logged for visibility).
    """
    state = request.app.get("state")
    svc = _autonudge_instance()
    if state is None or svc is None:
        logger.warning(
            "auto_research: cannot launch loop for %s (autonudge/state unavailable)", cid
        )
        return
    db = _get_db()
    row = db.execute(
        "SELECT question, sub_questions, sources, max_cycles, idle_secs, success_criteria, "
        "auto_approve FROM campaigns WHERE id = ?",
        (cid,),
    ).fetchone()
    db.close()
    if row is None:
        return
    _write_brief(cid, row)
    slot = state.get_or_create_slot(
        name=f"research-{cid}", agent=_RESEARCH_AGENT, app="auto-research"
    )
    # The worker runs autonomously — auto-approve its tools so the loop never
    # stalls on per-tool approval prompts (brakes: max_cycles, Stop, sandbox,
    # deny-list). The slot is app-owned, so it's hidden from the chat sidebar.
    # NOTE: slot._trust is the PER-SLOT trust flag (same mechanism as the
    # interactive "trust this session" in chat_handlers.py and gateway scoped
    # trust) — NOT the global _yolo_mode that safety_override() governs, which is
    # a single process-wide toggle and cannot express per-campaign grants. The
    # grant is instead bounded per campaign: the watchdog expires it after
    # _TRUST_TTL_SECS and forces NEEDS_INPUT re-authorization (see _watchdog_loop).
    slot._trust = True
    _audit("campaign_auto_approve", cid)
    state.push_slots_update()  # surface the app-owned worker slot so the UI filters it
    await svc.add(
        slot_key=slot.key,
        message=_RESEARCH_NUDGE.format(cid=cid),
        idle_secs=int(row["idle_secs"] or DEFAULT_IDLE_SECS),
        max_cycles=int(row["max_cycles"] or 0),
        stop_sentinel_path=str(_campaign_dir(cid) / "STOP"),
    )


def _write_brief(cid: str, row: Any) -> None:
    """Write the campaign brief (raw question + sub-questions) the agent reads each cycle.

    Local file in the campaign dir (the agent's file-based interface) — not an
    external surface, so the user's own question text is written as-is.
    """
    subs = json.loads(row["sub_questions"] or "[]")
    srcs = json.loads(row["sources"] or "[]")
    lines = ["# Research Brief", "", f"**Question:** {row['question']}", "", "**Sub-questions:**"]
    lines += [f"- {s}" for s in subs] or ["- (none — derive your own from the question)"]
    lines += [
        "",
        f"**Sources allowed:** {', '.join(srcs) or 'any'}",
        f"**Max cycles:** {row['max_cycles']}",
    ]
    if not row["auto_approve"]:
        lines += [
            "",
            "**Questions allowed:** if the goal or scope is genuinely ambiguous in a "
            "way that would materially change your research direction, you MAY ask ONE "
            "high-leverage clarification question (first-principle: state what you know, "
            "the specific decision, and the options). Keep the bar high — proceed on a "
            "best-reasoned assumption for anything minor or self-resolvable. Write "
            '{"question": ..., "why": ...} to questions.json and end the turn — the '
            "campaign pauses for the user, who answers via Nudge.",
        ]
    if row["success_criteria"]:
        lines += [
            "",
            f"**Definition of Done:** {row['success_criteria']}",
            "Verify against this each cycle using your tools (run tests, review, eval); "
            "when met, set verification.passed=true in the finding.",
        ]
    lines += [
        "",
        "Adapt direction each cycle from prior findings; pursue the highest-value open "
        "lead toward the question.",
    ]
    _campaign_dir(cid).joinpath("brief.md").write_text("\n".join(lines))


async def _stop_loop(cid: str, *, remove: bool) -> None:
    """Pause (remove=False) or tear down (remove=True) a campaign's autonudge loop."""
    svc = _autonudge_instance()
    if svc is None:
        return
    loop = svc.get_by_slot(f"research-{cid}")
    if not loop:
        return
    if remove:
        await svc.remove(loop.id)
    else:
        await svc.update(loop.id, active=False)


# --- HTTP handlers ---


async def _handle_validate(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    _audit("campaign_validate", "*")
    body = await request.json()
    return web.json_response(validate_campaign(body))


_SUGGEST_PROMPT = (
    "Propose sub-questions that make researching this question thorough — each "
    "capturing a DISTINCT perspective or angle needed to investigate it well "
    "(e.g. technical feasibility, trade-offs, alternatives, risks, cost, prior "
    "art, stakeholder needs, success criteria). Reason from first principles: "
    "fundamental, non-overlapping angles, not generic restatements. Output ONLY "
    "a JSON array of concise sub-question strings (no prose), at most 20."
    "\n\nQuestion: {q}"
)


def _parse_subquestions(raw: str) -> list[str]:
    """Extract a JSON array of sub-question strings from an LLM reply (cap 20)."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    out = [str(s).strip() for s in items if isinstance(s, str) and s.strip()]
    return out[:20]


async def _handle_suggest(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await request.json()
    question = (body.get("question") or "").strip()
    if len(question) < 20:
        return web.json_response({"error": "Question too short"}, status=400)
    _audit("campaign_suggest", "*")
    pool = request.app.get("auto_research_llm_pool")
    if pool is None:
        return web.json_response({"sub_questions": []})
    try:
        raw = await pool.send(_SUGGEST_PROMPT.format(q=question), timeout=45.0)
        subs = _parse_subquestions(raw)
    except Exception as exc:
        logger.warning("auto_research suggest failed: %s", exc)
        subs = []
    return web.json_response({"sub_questions": _redact_finding({"v": subs})["v"]})


async def _handle_create(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await request.json()
    v = validate_campaign(body)
    if not v["can_start"]:
        return web.json_response({"error": "Validation failed", **v}, status=400)
    result = create_campaign(body)
    result["name"] = _redact_finding({"v": result["name"]})["v"]
    return web.json_response(result, status=201)


async def _handle_list(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    _audit("campaign_list", "*")
    return web.json_response(list_campaigns())


async def _handle_get(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_get", cid)
    c = get_campaign(cid)
    return web.json_response(c) if c else web.json_response({"error": "Not found"}, status=404)


def _read_report(campaign_id: str) -> str:
    """Read the agent's cumulative FINDINGS.md report (empty if none yet)."""
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return ""
    p = d / "FINDINGS.md"
    try:
        return p.read_text() if p.exists() else ""
    except OSError:
        return ""


async def _handle_report(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_report", cid)
    # FINDINGS.md is agent-authored — redact before serving to the dashboard.
    report = _redact_finding({"v": _read_report(cid)})["v"]
    return web.json_response({"report": report})


async def _handle_action(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    body = await request.json()
    action = body.get("action")
    status_map = {
        "start": CampaignStatus.RUNNING,
        "pause": CampaignStatus.PAUSED,
        "resume": CampaignStatus.RUNNING,
        "stop": CampaignStatus.STOPPED,
    }
    if action not in status_map:
        return web.json_response({"error": f"Unknown action: {action}"}, status=400)
    # Guard invalid source-state transitions (e.g. start on a running campaign,
    # which would reset started_at and relaunch a duplicate worker loop).
    allowed = {
        "start": {CampaignStatus.READY},
        "resume": {
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
            CampaignStatus.FAILED,
        },
        "pause": {CampaignStatus.RUNNING},
        "stop": {
            CampaignStatus.READY,
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
        },
    }
    db = _get_db()
    srow = db.execute("SELECT status FROM campaigns WHERE id = ?", (cid,)).fetchone()
    db.close()
    if srow is None:
        return web.json_response({"error": "Not found"}, status=404)
    if srow["status"] not in allowed[action]:
        return web.json_response(
            {"error": f"Cannot {action} a campaign in '{srow['status']}' state"}, status=409
        )
    result = update_campaign_status(cid, status_map[action])
    if "error" in result:
        return web.json_response(result, status=404)
    if action in ("start", "resume"):
        await _launch_loop(request, cid)
    elif action == "pause":
        await _stop_loop(cid, remove=False)
    elif action == "stop":
        await _stop_loop(cid, remove=True)
    return web.json_response(result)


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    await _stop_loop(cid, remove=True)  # tear down any running worker first
    result = delete_campaign(cid)
    if "error" in result:
        return web.json_response(result, status=404)
    _audit("campaign_deleted", cid)
    return web.json_response(result)


async def _handle_nudge(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    write_guidance(cid, text)
    # If the agent paused awaiting input, clear the question and resume.
    qp = _questions_path(cid)
    if qp and qp.exists():
        qp.unlink()
        update_campaign_status(cid, CampaignStatus.RUNNING)
    _audit("campaign_nudge", cid)
    return web.json_response({"ok": True})


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not _validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    _audit("campaign_stream", cid)
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    _sse_queues.append(q)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event.get("campaign_id") == cid:
                    # Findings are already redacted at the source
                    # (get_findings -> _redact_finding); avoid re-redacting.
                    data = json.dumps(event)
                    await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _sse_queues.remove(q)
    return resp


# --- Route registration ---


def register_routes(app: web.Application) -> None:
    app.router.add_post("/api/apps/auto-research/validate", _handle_validate)
    app.router.add_post("/api/apps/auto-research/suggest", _handle_suggest)
    app.router.add_post("/api/apps/auto-research/campaigns", _handle_create)
    app.router.add_get("/api/apps/auto-research/campaigns", _handle_list)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}", _handle_get)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/report", _handle_report)
    app.router.add_patch("/api/apps/auto-research/campaigns/{id}", _handle_action)
    app.router.add_delete("/api/apps/auto-research/campaigns/{id}", _handle_delete)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/nudge", _handle_nudge)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/stream", _handle_stream)

    async def _start_watchdog(_app: web.Application) -> None:
        global _watchdog_task
        # Dedicated LLM pool for the suggest endpoint — isolated from the
        # Knowledge Library's pool so the two apps don't share workers.
        _app["auto_research_llm_pool"] = LLMPool(pool_size=1)
        _watchdog_task = asyncio.create_task(_watchdog_loop(_app))

    async def _stop_watchdog(_app: web.Application) -> None:
        if _watchdog_task and not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass
        pool = _app.get("auto_research_llm_pool")
        if pool is not None:
            await pool.shutdown()

    app.on_startup.append(_start_watchdog)
    app.on_shutdown.append(_stop_watchdog)
