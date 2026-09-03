# Tool Search deferral after /compact - KiroCrew fix and upstream work

Reference: GitHub issue #8082 (MCP Tool Search deferred tools become
permanently unavailable in a session after context compaction).

## The problem

kiro-cli's MCP Tool Search loads tool specs on demand ("search-and-call")
instead of sending every spec each turn. It DEFERS a spec once the tool set
grows past the activation thresholds (`toolSearch.minPct` /
`toolSearch.minTokens`). A deferred spec is absent from the model's tool list.

When a long session runs `/compact`, the context shrinks back below the
threshold, but kiro-cli does NOT re-inject the previously deferred specs. Those
tools stay invisible to the model for the rest of the session, so a tool that
was available before compaction can never be called again without starting a
new session.

The deferral engine - deciding to defer a spec, and re-injecting it after the
context shrinks - lives in kiro-cli, on the far side of the ACP boundary. It is
not implemented in this repo and cannot be fixed or unit-tested here.

## What was fixed here (KiroCrew side)

Added `AcpProvider.reload_tool_search(*, min_pct=None, min_tokens=None) -> bool`
in `src/kiro_crew/providers/acp.py`. It is the manual per-session escape hatch,
the only lever available on this side of the boundary:

1. It rewrites the per-session Tool Search overlay
   (`<work_dir>/.kiro/settings/cli.json`) through the existing
   `_apply_tool_search_overlay` / `_write_tool_search_overlay` path, optionally
   applying a per-call `min_pct` / `min_tokens` override.
2. It RESTARTS the kiro-cli backend (`await self.shutdown()` then
   `await self.start()`), so kiro-cli re-reads the overlay on the fresh spawn
   and recomputes deferral against the current (post-`/compact`) context,
   re-injecting the specs that now fit.

Its shape mirrors the existing `change_effort` / `clear_effort` live-lever
precedent:

- Gated on `ACP_BACKENDS_KIRO_SLASH_COMMANDS` membership (only the kiro family
  reads the overlay). Returns `False` without restarting for the claude backend.
- Returns `False` without restarting when `self._tool_search` is `None`
  (overlay unmanaged) or `False` (KiroCrew defers nothing, so a reload restores
  nothing).
- Returns `False` without restarting when a turn is active
  (`has_active_turn()`), so the restart never races a streaming prompt (the same
  mid-turn hazard the effort/model-switch levers refuse).
- Snapshots the thresholds, and on any exception during the restart rolls them
  back and re-applies the overlay so a failed reload never strands poisoned
  thresholds on future respawns.

Tests: `test/test_acp_tool_search_reload.py` (all seven cases, asserting real
`cli.json` contents and spying `shutdown`/`start`).

### Dashboard entry point (making the lever reachable)

`reload_tool_search` is surfaced to the dashboard through
`POST /api/chat/slots/{slot}/reload-tool-search`
(`api_chat_slot_reload_tool_search` in
`src/kiro_crew/dashboard/chat_handlers.py`, registered in
`src/kiro_crew/dashboard/routes/chat.py` next to the effort route). This mirrors
how `change_effort` is exposed via `api_chat_slot_reasoning_effort`:

- Resolves the live provider from `effective_session_key(slot)` (never the
  dashboard-prefixed key, which would name a session a channel/cron slot never
  ran under) and only acts when it is an `AcpProvider`; a cold slot or non-ACP
  provider reports `reloaded=False` rather than 404.
- Runs the whole sequence under `slot._lock` and re-checks `has_active_turn()`
  under that lock before delegating. This is the serialization
  `reload_tool_search`'s own check-then-act turn guard depends on: because
  starting a turn takes the same lock, a prompt cannot slip in between the
  handler's turn check and the provider's `shutdown()`/`start()`. An active turn
  is surfaced as a retryable `409 turn_in_flight` (the effort route defers
  instead, but a Tool Search reload is a hard restart, so it refuses like the
  session reload route does).
- Validates the optional `min_pct` / `min_tokens` override (non-negative ints;
  the provider clamps further) and forwards it; a `reload_tool_search` that
  raises on a failed restart is surfaced as a `500 reload_failed` (the provider
  has already rolled its thresholds back).

Handler tests: `test/test_chat_slot_reload_tool_search.py` (delegation,
`reloaded` true/false, override forwarding + validation, active-turn 409,
no-live-session, unknown slot 404, restart-failure 500).

### Scope decision (base.py)

`reload_tool_search` was added to `AcpProvider` ONLY. It was NOT added to the
`LLMProvider` base interface in `src/kiro_crew/providers/base.py`, because the
only caller (the dashboard handler above) invokes it after an
`isinstance(provider, AcpProvider)` check - it dispatches on the concrete type,
not polymorphically through `LLMProvider` (the same shape as the existing
`change_effort` lever, which is also gated by `isinstance(..., AcpProvider)` in
`api_chat_slot_reasoning_effort`). Per FEAT-002 step 4, the base default is added
only if a caller dispatches through `LLMProvider`. If a future surface needs to
call this on an arbitrary provider, add a default
`async def reload_tool_search(self, *, min_pct=None, min_tokens=None) -> bool:
return False` to `LLMProvider` at that time so non-ACP backends stay inert.

### Threshold override lifetime (intended session policy)

A successful reload with a `min_pct` / `min_tokens` override keeps those
thresholds as the session's standing policy: every later respawn re-applies them
via `_apply_tool_search_overlay` in `start()`. This is deliberate for the
escape-hatch use case (an operator lowers the threshold to force deferred tools
back, and that intent should hold for the rest of the session rather than revert
on the next model swap or reload). There is intentionally NO companion
"reset to configured defaults" lever the way `clear_effort` complements
`change_effort`: the dashboard owns session lifecycle and recreates the session
from the configured defaults when the operator wants the originals back, so a
dedicated reset would be redundant surface for a rarely-hit case. The only reset
in the method is the rollback-on-failure snapshot, which exists solely so a
FAILED restart cannot poison future respawns.

## What remains upstream (kiro-cli)

The durable fix belongs in kiro-cli, not here. Ideally kiro-cli should either:

- Automatically re-inject deferred tool specs when the context shrinks back
  below `toolSearch.minPct` / `toolSearch.minTokens` after a `/compact`, with no
  explicit reload required; or
- Expose an in-session reload that recomputes deferral WITHOUT a full backend
  restart (the KiroCrew escape hatch has to restart the whole kiro-cli process,
  which drops warm state and interrupts the session).

## Recommendation

A KiroCrew maintainer should decide whether to file the upstream kiro-cli issue
capturing the automatic re-injection / in-session reload request above,
cross-referencing issue #8082. KiroCrew's `reload_tool_search` is the interim
mitigation until that upstream work lands.
