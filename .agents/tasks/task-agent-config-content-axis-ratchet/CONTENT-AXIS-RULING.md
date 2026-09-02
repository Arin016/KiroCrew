# Content axis of `PUT /api/agent/config`: open maintainer ruling (#7470)

This brief surfaces a decision that cannot be made from the codebase alone. It
implements **neither** candidate shape. #7089 shipped the deletion (absent) half
only and stopped at this same point for exactly this reason.

## The ruling question

`PUT /api/agent/config` makes on-disk state authoritative for the `<app>:<server>`
region on both directions of *existence*: #6975 stopped it clobbering a bridge the
submission omits (absent axis), and #7089 stopped it resurrecting one the
submission still carries (present-drop axis). One axis is left, and it is about
*content* rather than existence:

> Where the name is on disk **and** in the submission, the submitted row still
> wins, so a stale editor snapshot can revert a bridge definition the platform had
> already corrected (e.g. a `backend.port: "auto"` app whose bridge was rewritten
> to its live allocated port by `_resolve_live_mcp_url`, saved back to the
> manifest's illustrative port by an editor tab that loaded before the rewrite).

**What needs ruling:** may the content axis be reversed at all, and if so, which of
the two candidate shapes below closes it? Reversing it undoes the
editor-snapshot-wins contract kept deliberately in #5899 and re-affirmed for #6664
(maintainer decision 2026-08-30), so it needs a maintainer ruling rather than a fix
decided in review.

## Candidate shapes (costs faithful to the issue)

### Shape 1 — substitute on-disk rows
In `_drop_unbacked_app_entries`, where a submitted namespaced name **is** on disk,
replace the submitted value with the on-disk one.

- **Size:** mechanically about six lines and one changed test assertion (the
  assertion in `test_app_owned_entry_present_in_the_snapshot_is_updated`).
- **Cost:** the contract reversal itself — an app bridge stops being editable
  through the raw editor. (Arguably already the intent: "an app bridge cannot be
  removed through this endpoint" is stated in `_merge_unowned_servers`.)

### Shape 2 — optimistic concurrency
Version/etag the config snapshot and reject a stale `PUT` with a machine-readable
`code` so the editor reloads.

- **Reach:** closes this axis and the two already-closed ones at their real root —
  the PUT accepting a snapshot with no staleness signal at all — and would have
  prevented #6664, #7089 and this one.
- **Cost:** it is a new API contract plus a client reload path, i.e. feature work,
  **and it has no client landing path today:**
  - No `etag`/`If-Match`/version field exists anywhere in the PUT handler
    (`src/kiro_crew/dashboard/handlers/agents.py` — verified: no `etag`/`If-Match`
    match).
  - `saveAgentConfig` sends only `{ config }`
    (`website/src/api/client.ts:2774`).
  - `AgentCfgTab.tsx:18` fills its edit buffer once
    (`useEffect(() => { if (loadedCfg && !cfg) setCfg(loadedCfg) }, ...)`) and
    never refreshes it, so a stale-PUT rejection has nothing to land on.

## Blast radius (why this is worth a decider's attention)

The blast radius is wider than one app's tools. Writing back a port nobody is
listening on produces exactly the dead-looking URL the handler's own docstring
describes: a manifest's illustrative port is a reachable-LOOKING dead URL that
kiro-cli dials on every request and that **"breaks EVERY kiro session, not just
that app's"** (`src/kiro_crew/dashboard/handlers/agents.py:285`).

## Self-heal mitigation (why it is separable / less severe than resurrection)

Unlike a resurrected bridge, this axis **self-heals**. An enabled app's bridge is
re-registered with its live port on the next gateway start via
`reconcile_enabled_app_resources`, so the window is **until the next restart**
rather than indefinite. Resurrection did not self-heal because
`reconcile_enabled_app_resources` only re-registers *enabled* apps. That difference
is the reason the content axis was separable from the deletion half.

## Ground-truth anchors verified on the current branch

Branch `fix/agent-config-content-axis-ratchet-7470`; HEAD when verified: the
region-level two-axis ratchet commit `d0fc4f9e5` on top of `6581a04ee`. (The line
numbers in the issue body are from the older commit `be2ee94`; the anchors below
are the current file:line locations.)

1. **Handler docstring — the deliberate non-goal.**
   `src/kiro_crew/dashboard/handlers/agents.py:287-291`, the
   `_drop_unbacked_app_entries` docstring section
   **"WHAT THIS DELIBERATELY DOES NOT DO: it never rewrites a submitted value."**
   It states the submitted row still wins untouched, cites the #5899/#6664
   editor-snapshot-wins contract and the pinning test, and says
   *"Reversing it needs a maintainer ruling and is tracked separately."*
   (Blast-radius sentence at `:285`; self-heal note for the absent axis at
   `:225-227`.)

2. **Spec decision table — section 1a.**
   `docs/system-specs/modules/app-kit-platform.md`:
   - Decision-table row `` `<app>:<server>` on disk `` → **"persisted as
     submitted — the snapshot still wins where the platform agrees the name
     exists"** (line 265).
   - **"Not closed here:"** paragraph at lines 288-292, ending *"…it
     self-heals on the next gateway start, so it is **left to a separate
     ruling**."*

3. **Pinning test — submitted-row-wins.**
   `test/test_agent_config_merge_on_write.py:1034-1047`,
   `test_app_owned_entry_present_in_the_snapshot_is_updated`, still asserts
   `written["mcpServers"] == {"demo:notes": {"command": "new"}}` (the submitted
   `command: "new"` overwrites the on-disk `command: "old"`).

## Disposition

All three markers are present and consistent with the issue, so the codebase
already reflects the open ruling correctly. **No source change was made** and
**neither candidate shape was implemented** (no etag/If-Match/version on handler or
client; no on-disk row substitution in `_drop_unbacked_app_entries`; the pinning
test's asserted behavior is unchanged). `pytest -q test/test_agent_config_merge_on_write.py`
→ 47 passed, `test_app_owned_entry_present_in_the_snapshot_is_updated` among them.

The region-level two-axis ratchet the issue also asks for (asserting every key of a
non-client-owned region is decided by on-disk state on *both* axes) is the
ruling-independent half and landed separately (commit `d0fc4f9e5`).
