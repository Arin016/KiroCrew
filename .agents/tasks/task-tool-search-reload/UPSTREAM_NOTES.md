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

### Scope decision (base.py)

`reload_tool_search` was added to `AcpProvider` ONLY. It was NOT added to the
`LLMProvider` base interface in `src/kiro_crew/providers/base.py`, because this
change wires no caller through the base type - the method is invoked only where
the provider is already known to be an `AcpProvider` (the same shape as the
existing `change_effort` lever). Per FEAT-002 step 4, the base default is added
only if a caller dispatches polymorphically through `LLMProvider`. If a future
dashboard/API surface needs to call this on an arbitrary provider, add a default
`async def reload_tool_search(self, *, min_pct=None, min_tokens=None) -> bool:
return False` to `LLMProvider` at that time so non-ACP backends stay inert.

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
