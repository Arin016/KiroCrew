# MeshClaw → KiroClaw — Left-Out Commit Provenance

> Companion to [`last-synced.txt`](./last-synced.txt). That file records the sync
> **boundary** (what has been triaged); this file records, per commit, **what was
> deliberately NOT ported and why**. Compiled 2026-06-14 from the shipped batch
> CR descriptions + their "Full per-commit provenance" comments + sync commit
> bodies.

## What this is

This repo's current history was produced by two separate events, each of which
"left out" commits. This document records **both** populations:

1. **The MeshClaw→KiroClaw content syncs** (batches 1–18) — the bulk of this doc.
   The fork shares **no git history** with its upstreams
   ([MeshClaw](https://code.amazon.com/packages/MeshClaw) backend +
   [MeshClawWebsite](https://code.amazon.com/packages/MeshClawWebsite) frontend);
   every fix is ported **by content**, so a "left-out" commit is one a sync batch
   triaged and consciously declined to port.
2. **The 2026-06-14 history replacement** — the original 49-commit KiroClaw
   package history was wiped and replaced with the de-Amazoned fork's history.
   Those 49 commits are enumerated in
   [The 49 wiped original-KiroClaw commits](#the-49-wiped-original-kiroclaw-commits).

This is the consolidated record so the decisions (and their reasons) survive even
if the source CRs or the backup branch are pruned.

### Independent coverage audit (2026-06-15)

The lists below are sourced from the sync *records* (CRs + commit bodies). To
check whether those records have **silent gaps** — upstream commits that were
never triaged at all, appearing in neither the ported nor the left-out set — an
independent package-diff audit was run against the actual upstream repos:

- **Method:** enumerate every non-merge commit in the upstream universe
  (`MeshClaw` backend `9f5bb6b6..570a9ccf` = 116 commits; `MeshClawWebsite`
  frontend `2026-06-02 snapshot..ecc6e5a5` = 126 commits), then check each
  upstream SHA's 7-char prefix against the union of all ported provenance (fork
  commit bodies) + all left-out records (this doc + `last-synced.txt`).
- **Result: 0 unaccounted, both repos.** Every one of the 242 in-window upstream
  commits is accounted for as either ported or a documented left-out. (Validated:
  no intra-universe 7-prefix collisions, so the short-SHA match is unambiguous.)

So this record is **comprehensive over the full batch-1→18 window**, not just over
what the CRs happened to write down. The residual honesty caveats below
(pre-snapshot, batch-17, the 1 unnamed batch-18 DEFER) stand — they concern
*detail granularity*, not missing commits.

**Un-triaged tail (NOT gaps — newer than the last sync):** since the batch-18
boundary, upstream has moved on. As of 2026-06-15 there are **7 new backend**
commits (`570a9ccf..5de9411e`) and **8 new frontend** commits
(`ecc6e5a5..314a69e`) that no batch has triaged yet — they postdate batch-18.
These are the next sync's input, not omissions. (e.g. backend: Code Review Sage
built-in app, folder emoji, artifact comments, `subagent_auto_max` config;
frontend: folder icon picker, paste-token chips, artifact-comments CX, masonry
guard.)

### Source of truth & a caveat on completeness

Per the meshclaw-sync skill, exhaustive per-commit provenance was meant to live
in a **CR comment** on each batch CR. In practice that comment was posted for
some batches and **not** others:

| Batch | Own CR | Provenance comment present? | Left-out source used here |
|---|---|---|---|
| 1–3 | [CR-280626986](https://code.amazon.com/reviews/CR-280626986) | ❌ empty | CR description + sync commit body |
| 4–7 | [CR-280672548](https://code.amazon.com/reviews/CR-280672548) | ❌ empty | CR description + boundary commits |
| 8 | [CR-280853988](https://code.amazon.com/reviews/CR-280853988) | ❌ empty | CR description ("Left out: None") |
| 9 | _(none — SKIP-only boundary advance, commit `b62394c`)_ | n/a | sync commit body |
| 10–11 | [CR-280980741](https://code.amazon.com/reviews/CR-280980741) | ✅ 21 KB comment | **full per-commit table** |
| 12 | [CR-281070110](https://code.amazon.com/reviews/CR-281070110) | ❌ (only an AutoSDE bot comment) | CR description + boundary commit |
| 13 | [CR-281120970](https://code.amazon.com/reviews/CR-281120970) | ✅ comment | full per-commit table |
| 14 | [CR-281228622](https://code.amazon.com/reviews/CR-281228622) | — (no-ports batch) | boundary commit `7b1530a` |
| 15 | [CR-281319232](https://code.amazon.com/reviews/CR-281319232) | ✅ comment | nothing left out (8/8 ported) |
| 16 | [CR-281392951](https://code.amazon.com/reviews/CR-281392951) | ✅ comment | full per-commit table |
| 17 | [CR-281529650](https://code.amazon.com/reviews/CR-281529650) | **ABANDONED / unmerged** | re-ported in batch-18 |
| 18 | [CR-281902310](https://code.amazon.com/reviews/CR-281902310) | ❌ empty | CR description + boundary file |

**Gaps to be honest about:**
- **Pre-2026-06-02 is undocumented.** The fork began as a hand-built content
  snapshot on ~2026-06-02; whatever upstream history predates that snapshot was
  never enumerated. This document starts at the first post-snapshot sync (batch 1–3).
- **Batches 12 and 18** intended an exhaustive comment that was never posted, so
  their left-out lists here are the description-level summaries (high-signal, but
  not guaranteed to name every sub-hunk).
- **Batch 17** was abandoned before merge; its candidates were re-triaged from
  scratch in batch 18, so it has no independent left-out record.

## Verdict categories

| Verdict | Meaning |
|---|---|
| **SKIP_INTERNAL** | Touches a subsystem **absent** from the de-Amazoned fork (mcp_gateway, secretary, writing-review, auto-research, promptfarm, GitFarm/Cloud-Sync, AIM, team_manager, RUM telemetry, Harmony Artifactory, LCARS/Bikini-Bottom themes, etc.). 100% confined — nothing generic to salvage. |
| **SKIP_NONKIROACP** | Specific to a deleted LLM provider (Bedrock / Claude Code). Meaningless under the fork's single-provider `agent.provider` enum `["acp"]`. |
| **SKIP** | Other deliberate skip (e.g. Brazil-`Config`-only hunk that the setuptools build ignores; a Midway-stub-only change; a generic helper with zero fork consumers per the anti-miss check). |
| **ALREADY_PRESENT** | The fork already carries the change's post-image (often a restore/revert of a regression the fork never had). Porting = no-op. |
| **DEFER** | Technically portable but deliberately held for a separate scoped change or human review. **See the [Human-decision section](#human-decision-items-defer--flagged) — these are the live ones.** |
| **NA_INTERNAL** | Early-batch label equivalent to SKIP_INTERNAL (Amazon-internal-only dependency). |

---

## Human-decision items (DEFER / flagged)

**These are the commits that were NOT skipped on a clear rule — they need a
human call.** Everything in the [SKIP catalogue below](#full-skip-catalogue) is
mechanically out of scope (absent subsystem / wrong provider / already present);
the items here are portable-in-principle and were held back, or flagged as
judgement calls.

### Still open (pending a decision)

| Upstream SHA | Repo | Batch | Status | What it is / why it needs a human |
|---|---|---|---|---|
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | 18 | **DEFER — open** | Artifacts page **masonry layout** rewrite: +572 lines on a hard-diverged 253-line page + a new `@virtuoso.dev/masonry` dependency, with **zero fix/security value** (pure layout). Batch-17's human review also deferred it. Decision needed: adopt the masonry rewrite (and the new dep) onto the fork's diverged Artifacts page, or drop permanently. |
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | 18 | **SKIP_INTERNAL — flagged** | SharePoint/Loop **redaction carve-out**. Skipped because it targets Amazon-corp M365 hosts only (precedent: `e62422ae`), but it was **explicitly flagged for human review** rather than cleanly out of scope — a reviewer should confirm the fork wants no SharePoint/Loop redaction path. |

### Resolved (deferred earlier, later ported — recorded for the audit trail)

| Upstream SHA | Repo | Deferred in | Resolution |
|---|---|---|---|
| [`38864fd9`](https://code.amazon.com/packages/MeshClaw/commits/38864fd98f4fc7fabd81487b6e91ae6a49f0ebf1) (+ `d17306e1`) | backend | batch 1–3 (DEFERRED: 4331-line multi-instance SSH tunnels, no UI consumer) | **PORTED (PARTIAL) in batch-10** — kept the generic multi-instance registry / port-allocator / plain-OpenSSH tunnel manager + UI; **dropped** the Midway SSH-cert watchdog (`instances/midway.py`) and its `~/.ssh` carve-out (forbidden by `MIGRATION_PLAN.md`). |
| [`b490c7e8`](https://code.amazon.com/packages/MeshClaw/commits/b490c7e8) | backend | batch 6–7 (DEFERRED: dynamic sub-agent concurrency cap; depended on absent `mcp_gateway.pool`) | **PORTED in batch-8** — relocated the ~50-line stdlib `/proc`-subtree RSS/CPU helpers into `subagent.py`; the absent-import objection was overcome. |
| [`96c39b8`](https://code.amazon.com/packages/MeshClawWebsite/commits/96c39b8) | frontend | batch 18 (initially DEFER) | **RESCUED to KEEP in batch-18** — its backend pair `7b66e2e3` (MLX Whisper STT) was a keeper the same batch, so the UI was ported backend-first. |

---

## Full SKIP catalogue (mechanical — absent subsystem / provider / already present)

These required no judgement: each targets something that does not exist in the
fork, belongs to a deleted provider, or is already present. Grouped by batch.

### Batch 1–3 — [CR-280626986](https://code.amazon.com/reviews/CR-280626986) (24 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`cf5f017a`](https://code.amazon.com/packages/MeshClaw/commits/cf5f017a) | backend | NA_INTERNAL | PromptFarm skills — depends on Midway `McsRequestsHook` auth + a CodeArtifact-only dep + a hardcoded `*.prompt-farm.payments.amazon.dev` endpoint. |
| [`a7a03199`](https://code.amazon.com/packages/MeshClaw/commits/a7a03199) | backend | NA_INTERNAL | Writing-review scanner sync — `writing_review/` dir absent in the fork. |
| `38864fd9` | backend | DEFER → later ported | See [Human-decision / resolved](#resolved-deferred-earlier-later-ported--recorded-for-the-audit-trail). |

### Batch 4–7 — [CR-280672548](https://code.amazon.com/reviews/CR-280672548) (8 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`ea6348d5`](https://code.amazon.com/packages/MeshClaw/commits/ea6348d5) | backend | SKIP | `fix(midway): cache midway_status` — fork `midway.py` is a no-op OSS stub; no real-mwinit path to cache. (Also the held boundary for the `b490c7e8` deferral.) |
| [`63ee7fde`](https://code.amazon.com/packages/MeshClaw/commits/63ee7fde) | backend | SKIP | `config`→`configuration` rename motivated by a Brazil `Config` case-collision; the fork has no Brazil build, so no analog. |
| `b490c7e8` | backend | DEFER → later ported | See [Human-decision / resolved](#resolved-deferred-earlier-later-ported--recorded-for-the-audit-trail). |

### Batch 8 — [CR-280853988](https://code.amazon.com/reviews/CR-280853988) (3 ported, 0 newly left out)

Cleared the deferral backlog (`b490c7e8` ported). The only non-ported edge
items: `11973f4c` (ALREADY_PRESENT — ported in batch 7) and `ea6348d5` (SKIP —
midway stub, already past the boundary).

### Batch 9 — _(no CR; SKIP-only boundary advance, commit `b62394c`)_

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`e62422ae`](https://code.amazon.com/packages/MeshClaw/commits/e62422ae) | backend | SKIP_INTERNAL | `fix(writing-review): send …` — writing-review subsystem absent. Sole candidate; boundary advanced with no port. |

### Batch 10–11 — [CR-280980741](https://code.amazon.com/reviews/CR-280980741) (67 ported, **36 left out** — full table below)

103 upstream commits triaged → 67 ported, 36 left out (28 SKIP_INTERNAL · 6
ALREADY_PRESENT · 2 SKIP_NONKIROACP). `[b11]` = batch-11 cron run.

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`1408ca73`](https://code.amazon.com/packages/MeshClaw/commits/1408ca73) | backend | SKIP_INTERNAL | Real tunnel-probe machinery the fork omits entirely. |
| [`33658c12`](https://code.amazon.com/packages/MeshClaw/commits/33658c12) | backend | SKIP_INTERNAL | `app-registry.json` entry pointing to internal GitFarm repo MeshClawApp-SystemMonitor. |
| [`437262df`](https://code.amazon.com/packages/MeshClaw/commits/437262df) | backend | SKIP_INTERNAL | Entirely Midway/internal-SSH connectivity docs + scripts. |
| [`599d6f64`](https://code.amazon.com/packages/MeshClaw/commits/599d6f64) | backend | SKIP_INTERNAL | `api_skill_publish_to_promptfarm` handler — promptfarm absent. `[b11]` |
| [`5c7b2c93`](https://code.amazon.com/packages/MeshClaw/commits/5c7b2c93) | backend | SKIP_INTERNAL | Changes only `test_sync_module.py` (GitProvider) — sync module absent. |
| [`37ca5898`](https://code.amazon.com/packages/MeshClawWebsite/commits/37ca5898) | frontend | ALREADY_PRESENT | Fork already at post-image + its prereq `202e3224` (CSS Highlight API find). |
| [`9fa9b796`](https://code.amazon.com/packages/MeshClawWebsite/commits/9fa9b796) | frontend | ALREADY_PRESENT | Pure deletions stripping Cloud Sync entry points the fork never had. |
| [`a673571`](https://code.amazon.com/packages/MeshClawWebsite/commits/a673571) | frontend | ALREADY_PRESENT | Fork ChatPage already routes `m.role=="mcp_oauth"`. `[b11]` |
| [`d3daac16`](https://code.amazon.com/packages/MeshClawWebsite/commits/d3daac16) | frontend | ALREADY_PRESENT | Fork carries the restore target (post-image), not the reverted regression. |
| [`dfbc99cd`](https://code.amazon.com/packages/MeshClawWebsite/commits/dfbc99cd) | frontend | ALREADY_PRESENT | Both restored features present + wired (embed mode + Browser settings tab). |
| [`eec9c679`](https://code.amazon.com/packages/MeshClawWebsite/commits/eec9c679) | frontend | ALREADY_PRESENT | Fork SettingsPage already has the substance this restores (Provider tab). |
| [`47934af9`](https://code.amazon.com/packages/MeshClawWebsite/commits/47934af9) | frontend | SKIP_INTERNAL | Confined to absent mcp_gateway (`McpPoolableServers` → `/api/mcp-gateway/*`). |
| [`4bb769ff`](https://code.amazon.com/packages/MeshClawWebsite/commits/4bb769ff) | frontend | SKIP_INTERNAL | Pure CloudWatch RUM telemetry; fork `rum.ts` is an inert stub. |
| [`4be5e549`](https://code.amazon.com/packages/MeshClawWebsite/commits/4be5e549) | frontend | SKIP_INTERNAL | Gated on absent mcp_gateway backend (`SharedMcpGatewayToggle`/`McpGatewayCard`). |
| [`58c73651`](https://code.amazon.com/packages/MeshClawWebsite/commits/58c73651) | frontend | SKIP_INTERNAL | Confined to `apps/code-reviewer/` — absent. |
| [`5948404c`](https://code.amazon.com/packages/MeshClawWebsite/commits/5948404c) | frontend | SKIP_INTERNAL | New `lcars/` theme subsystem — absent; cosmetic. |
| [`6fd9ba51`](https://code.amazon.com/packages/MeshClawWebsite/commits/6fd9ba51) | frontend | SKIP_INTERNAL | Bikini-Bottom parody-theme refactor — cosmetic. |
| [`7342c6e`](https://code.amazon.com/packages/MeshClawWebsite/commits/7342c6e) | frontend | SKIP_INTERNAL | `SecretaryPage.tsx` only — secretary absent. `[b11]` |
| [`79785150`](https://code.amazon.com/packages/MeshClawWebsite/commits/79785150) | frontend | SKIP_INTERNAL | PromptFarm (SkillsTab remote-skill install) — absent. |
| [`7b68031f`](https://code.amazon.com/packages/MeshClawWebsite/commits/7b68031f) | frontend | SKIP_INTERNAL | writing-review (`WritingReviewPage` + `wrScanners*` api) — absent. |
| [`7e1d2a1c`](https://code.amazon.com/packages/MeshClawWebsite/commits/7e1d2a1c) | frontend | SKIP_INTERNAL | AIM auto-update (`/api/settings/aim-update`) — absent. |
| [`7eb7d6c`](https://code.amazon.com/packages/MeshClawWebsite/commits/7eb7d6c) | frontend | SKIP_INTERNAL | `SyncPanel.tsx` (Cloud Sync/GitFarm) — absent. `[b11]` |
| [`97ed5548`](https://code.amazon.com/packages/MeshClawWebsite/commits/97ed5548) | frontend | SKIP_INTERNAL | Secretary (`SecretaryPage` + advance-on-dismiss) — absent. |
| [`a0b29564`](https://code.amazon.com/packages/MeshClawWebsite/commits/a0b29564) | frontend | SKIP_INTERNAL | Absent `apps/auto-research` (`ResearchLabPage`/`GrillTree`). |
| [`a232e5bc`](https://code.amazon.com/packages/MeshClawWebsite/commits/a232e5bc) | frontend | SKIP_INTERNAL | Iterates the absent `apps/auto-research/ResearchLabPage`. |
| [`a4207ecd`](https://code.amazon.com/packages/MeshClawWebsite/commits/a4207ecd) | frontend | SKIP_INTERNAL | Wholly in absent `apps/auto-research/` (`grillTree`/`GrillTree`). |
| [`aaf7cfe3`](https://code.amazon.com/packages/MeshClawWebsite/commits/aaf7cfe3) | frontend | SKIP_INTERNAL | GitFarm/Bindle workspace-sync — absent. |
| [`cdf5566c`](https://code.amazon.com/packages/MeshClawWebsite/commits/cdf5566c) | frontend | SKIP_INTERNAL | Secretary — absent. |
| [`d2d2b110`](https://code.amazon.com/packages/MeshClawWebsite/commits/d2d2b110) | frontend | SKIP_INTERNAL | writing-review (`WritingReviewPage` + WR* types/api) — absent. |
| [`d3b5fcb2`](https://code.amazon.com/packages/MeshClawWebsite/commits/d3b5fcb2) | frontend | SKIP_INTERNAL | Secretary (`SecretaryPage`/`secretarySlice`) — absent. |
| [`e06b2dd7`](https://code.amazon.com/packages/MeshClawWebsite/commits/e06b2dd7) | frontend | SKIP_INTERNAL | Harmony Artifactory share UI (`/api/artifacts/*/publish`) — absent. |
| [`e83c0bd7`](https://code.amazon.com/packages/MeshClawWebsite/commits/e83c0bd7) | frontend | SKIP_INTERNAL | Harmony Artifactory browse/fork (`/api/artifactory/*`) — absent. |
| [`ed7c87f`](https://code.amazon.com/packages/MeshClawWebsite/commits/ed7c87f) | frontend | SKIP_INTERNAL | PromptFarm publish UI in SkillsTab — absent. `[b11]` |
| [`ff8b9047`](https://code.amazon.com/packages/MeshClawWebsite/commits/ff8b9047) | frontend | SKIP_INTERNAL | `McpGatewayCard` (Shared MCP gateway) — mcp_gateway absent. |
| [`d7271865`](https://code.amazon.com/packages/MeshClawWebsite/commits/d7271865) | frontend | SKIP_NONKIROACP | Bedrock-specific image-limit downscaling (`BEDROCK_IMAGE_LIMITS`); not generic. |
| [`e42cb331`](https://code.amazon.com/packages/MeshClawWebsite/commits/e42cb331) | frontend | SKIP_NONKIROACP | Flips `agentTemplates` on the Claude Code adapter — CC provider surface. |

### Batch 12 — [CR-281070110](https://code.amazon.com/reviews/CR-281070110) (4 ported, 2 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`527459a0`](https://code.amazon.com/packages/MeshClaw/commits/527459a0) | backend | SKIP_INTERNAL | config-baseline regen. The CR description says "only promptfarm + AIM schema", but the actual diff also adds `instances.*` (multi-instance SSH schema), `slack.show_thinking`, and a `/api/status`→`/api/health` probe help-text fix. Skipping the whole generated baseline is still correct (it mirrors loader.py changes that were themselves not ported), but the description **understates** the commit's contents. |
| [`641a6f0c`](https://code.amazon.com/packages/MeshClaw/commits/641a6f0c) | backend | SKIP_NONKIROACP | Per-agent provider override — re-introduces the deleted multi-provider dispatch factory (`_build_provider_factory`/`_resolve_agent_provider` with bedrock/claude_code branches); meaningless under `enum ["acp"]`. (Mesh-1766) |

### Batch 13 — [CR-281120970](https://code.amazon.com/reviews/CR-281120970) (7 ported, 1 full + 1 partial skip)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`39d22f8a`](https://code.amazon.com/packages/MeshClaw/commits/39d22f8a) | backend | SKIP_INTERNAL | mcp_gateway backend-lifecycle hardening — 100% confined to absent `src/mesh_claw/mcp_gateway/` + its tests. |
| [`3a017786`](https://code.amazon.com/packages/MeshClaw/commits/3a017786) (Brazil `Config` hunk only) | backend | SKIP (partial) | The Brazil `Config` hunk of the python-docx floor bump; public build is setuptools. The generic `python-docx>=1,<2` floor was ported to `setup.cfg` (commit PARTIAL, not fully left out). |

### Batch 14 — [CR-281228622](https://code.amazon.com/reviews/CR-281228622) (0 ported — SKIP-only boundary advance)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`80c564f5`](https://code.amazon.com/packages/MeshClaw/commits/80c564f5) | backend | SKIP_INTERNAL | team-manager standup-cron leak fix — `apps/builtins/team_manager/` absent; `reconcile_schedule_crons` has no fork analogue. |
| [`7c25d7ef`](https://code.amazon.com/packages/MeshClaw/commits/7c25d7ef) | backend | SKIP | v2.7 docs + AGENTS (33 files) — bulk doc snapshot for absent subsystems; fork keeps diverged specs. (Anti-miss: ran fork's 4 `TestDoctorOllamaDocker` → pass.) |
| [`78f8f60c`](https://code.amazon.com/packages/MeshClaw/commits/78f8f60c) | backend | ALREADY_PRESENT | `validate_enterprise` test patch — fork's `validate_enterprise` is default-OPEN, tests already green by a more fundamental divergence. (Mesh-2072) |
| [`9099180`](https://code.amazon.com/packages/MeshClawWebsite/commits/9099180) | frontend | SKIP | frontend AGENTS.md v2.7 — diverged convention doc; fork's `website/AGENTS.md` is its own de-Amazoned voice. |

### Batch 15 — [CR-281319232](https://code.amazon.com/reviews/CR-281319232) (8 ported, **0 left out**)

The largest real-port batch — all 8 candidates (7 backend + 1 frontend) ported.

### Batch 16 — [CR-281392951](https://code.amazon.com/reviews/CR-281392951) (9 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`ebee95e7`](https://code.amazon.com/packages/MeshClaw/commits/ebee95e7) | backend | SKIP_INTERNAL | macOS gateway **service** installer — confined to absent `scripts/darwin-gateway-service/` (Midway-gateway LaunchAgent + FDA Swift helper + own DMG builder). Not the fork's `packaging/build-desktop.sh` DMG flow. |
| [`b9036ac4`](https://code.amazon.com/packages/MeshClaw/commits/b9036ac4) | backend | SKIP_INTERNAL | Mocks `summarize_standup` in standup async tests — team_manager/standup absent. |
| [`1ae3c85`](https://code.amazon.com/packages/MeshClawWebsite/commits/1ae3c85) | frontend | SKIP | Generic `useVisibilityInterval` hook, but its only consumer is the absent midway-ttl topbar countdown; anti-miss grep found no other fork caller → would be dead code. |

### Batch 21 — CR pending (3 ported, 2 SKIP_INTERNAL)

Window: backend `b35c496b..59ec6e1d`, frontend `fdfe158b..ca99bb4`. Ported:
`59ec6e1d` (loopback-WS, → `2092347`), `d10750e2` (app-config self-heal, →
`5891deb`), `ca99bb4` (voice prewarm — the batch-20 DEFER straggler, → `d6856a2`).

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`5d99a8d4`](https://code.amazon.com/packages/MeshClaw/commits/5d99a8d4198a5904f91eacff87b02380bf781bcb) | backend | SKIP_INTERNAL | feat(security) MCS-Jail Midway AgentContext (Mesh-1517) — Midway/MCS coupling; `jail.py` absent, MCS-Jail Brazil dep. Its one generic hunk (`sandbox.py` `userns_available()` public alias) has **no fork consumer** — only the absent `jail.py` calls it (anti-miss (b); same precedent as batch-16 `useVisibilityInterval`). `cli.py --no-jail`, `agent.jail` enum, cli_doctor jail status, config-baseline all confined to the jail. |
| [`afed9312`](https://code.amazon.com/packages/MeshClaw/commits/afed93127c82625d7287735202eef6d449ee01da) | backend | SKIP_INTERNAL | fix(mcp-gateway) raise pooled-backend cap 20→64 — 100% confined to `mcp_gateway/` (`manager.py` `GatewaySpec` + `McpGatewayConfig` in loader.py). Fork has **no `McpGatewayConfig`** (grep empty) — no anchor. |

> **Batches 19 & 20 were not back-filled into this table** — their exhaustive
> per-commit left-out provenance lives in the published CR comments on
> [CR-282682422](https://code.amazon.com/reviews/CR-282682422) (batch-19) and
> [CR-283464369](https://code.amazon.com/reviews/CR-283464369) (batch-20, rev 2).

### Batch 18 — [CR-281902310](https://code.amazon.com/reviews/CR-281902310) (44 ported, 2 DEFER + 13 SKIP_INTERNAL)

> ⚠️ **The exhaustive provenance comment was NEVER posted for this batch**
> (`allComments` is empty), and the CR description/commit bodies **name none of
> the 13 SKIP_INTERNAL SHAs** — they list only categories. The SHA list below is
> therefore reconstructed from the boundary file (`last-synced.txt`) and is the
> **only surviving record** of these SHAs. The 2 DEFER/flagged items are in the
> [Human-decision section](#human-decision-items-defer--flagged).

| Upstream SHA(s) | Repo | Verdict | Reason |
|---|---|---|---|
| `2b95f6ac`, `fc06e9ab` | — | SKIP_INTERNAL | AIM auto_update — absent. |
| `6d07a290`, `45821ff` | — | SKIP_INTERNAL | writing-review — absent. |
| `24745b38` | frontend | SKIP_INTERNAL | oncall-radar App Store entry — `app-registry.json` is `[]` by design. |
| `231cb2dc` | backend | SKIP_INTERNAL | code-approvers — Brazil infra. |
| `a6a7c2db` | frontend | SKIP_INTERNAL | AppSenseAIUsage telemetry — RUM stub. |
| `24f23968` | frontend | SKIP_INTERNAL | Knight Rider *world* — parody theme (like LCARS), absent substrate. |
| `7c9c140f`, `92a1d3bb`, `94196d1a`, `fedf6e4d` | — | SKIP_INTERNAL | changelog/version bumps to 3.x — fork is 0.1.0. |
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | SKIP_INTERNAL (**flagged**) | SharePoint/Loop redaction carve-out — see [Human-decision](#still-open-pending-a-decision). |
| [`3396e112`](https://code.amazon.com/packages/MeshClaw/commits/3396e112) | backend | SKIP | **(recovered on re-audit, not in the original 13)** Byte-identical twin of the ported `3ef2bdbc` (same upstream CR-281616797); intentionally not ported to avoid a duplicate. |
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | **DEFER** | artifacts masonry — see [Human-decision](#still-open-pending-a-decision). |
| _(unnamed)_ | — | **DEFER** | ⚠️ The CR claims **2 DEFER** but names only `cd6730f`. The second DEFER SHA is named nowhere in the CR or boundary file and is **unrecoverable** from existing sources. |

**Also in batch-18: 16 dropped sub-hunks inside PARTIAL ports.** These are not
standalone left-out commits — each is a deliberately-dropped piece of an
otherwise-ported upstream commit, recorded in the port commit's own body. Most
drop a CHANGELOG/version-bump hunk, an absent-subsystem hunk
(`writing_review`/`mcp_gateway`/`cc_session`), or a KIROCLAW-branding/placeholder
hunk the fork overrides. Examples: `570a9ccf` dropped its `acp-client.md` spec
hunk; `73fb9dd0` dropped `writing_review` hunks + tests; `b674cd5a` dropped 3
theme hunks (kiro-dark/light, bikini-bottom); `e7730da7` dropped the
`validate_enterprise` removals. The full per-port table lives in the individual
commit bodies (`git show <port-sha>`) — not duplicated here.

---

## Recurring reasons at a glance

Almost every mechanical SKIP traces to a subsystem the de-Amazoning **deleted or
stubbed**. If you're wondering why a class of upstream commit never lands:

- **mcp_gateway** (shared MCP pool) — deleted.
- **secretary**, **writing-review**, **research-lab**, **team_manager /
  standup**, **code-reviewer**, **oncall-radar** — builtin apps absent. The fork
  ships only `auto_research` + `file_explorer` under `apps/builtins/`, and
  `apps/app-registry.json` is `[]` by design. (Note: the upstream `auto-research`
  *frontend* `ResearchLabPage`/`GrillTree` UI is absent even though the backend
  `auto_research` app exists — those frontend commits are still SKIP_INTERNAL.)
- **promptfarm** (skill publish) — internal, deleted.
- **GitFarm / Cloud-Sync / Bindle workspace-sync**, **AIM auto-update**,
  **Harmony Artifactory** — internal infra, deleted.
- **RUM / AppSenseAIUsage telemetry** — inert stub.
- **Midway** (mwinit, gateway service, SSH-cert watchdog) — stubbed; `~/.ssh`
  carve-outs forbidden by `MIGRATION_PLAN.md`.
- **Bedrock / Claude Code** provider surfaces — `agent.provider` is fixed to
  `["acp"]`; multi-provider dispatch was deleted (the dormant `ACP_BACKEND_CLAUDE`
  seam is intentionally kept but not re-wired).
- **LCARS / Bikini-Bottom / Knight-Rider-world themes** — cosmetic parody
  subsystems, absent.
- **Brazil `Config`** hunks — public build is setuptools; the root `Config` was
  dropped entirely.
- **3.x changelog/version bumps** — fork is at `0.1.0`.

See [`../../CLAUDE.md`](../../CLAUDE.md) ("do not re-introduce Amazon-internal
couplings") and `DEAMAZON_REPORT.md` for the authoritative deleted/stubbed list.

---

## The 49 wiped original-KiroClaw commits

Separate from the sync left-outs above: on **2026-06-14** the `KiroClaw` Brazil
package's original 49-commit history was **replaced** with the de-Amazoned public
fork's 207-commit history (the package took ownership of the fork's content). The
original 49 commits were a *different, earlier* codebase that happened to share
the `KiroClaw` name — an internal kiro-cli **agent-pool** architecture (Slack ↔
ACP pool, FastAPI + MCP task scheduler, Taskei orchestrator, Ralph loop), not the
fork's setuptools `kiro_claw` package.

**Why wiped:** the two codebases are unrelated — the original was an ancestor
prototype, the fork is the current product. Rather than merge, the package was
repointed wholesale to the fork (your explicit decision). The original
architecture (ralph, taskei orchestrator, bang-command system, `agent_manager`/
`agent_instance` pool) is **confirmed absent** from the current tree — the fork
uses different concepts (`subagent.py`, `cron.py`/`taskrunner.py`, handler
keywords + MCP tools, memory/vector_memory).

**Where they survive:** local branch `backup/pre-external-migration` (tip
`557a9ba`) and the `backup/bolichen` remote namespace. Recoverable via
`git log backup/pre-external-migration`. They are **not** reachable from the
current `mainline`.

### The 49 commits (oldest → newest)

Grouped by theme; all dated 2026-03-08 → 2026-05-02.

**Bootstrap / scaffold (2):** `93cd428` BuilderHub Create · `93e777a` initial commit

**Slack / ACP chat core (6):** `9f79f96` Slack images as ACP vision blocks ·
`91ffa04` INTROSPECTION prompt section · `9f54b05` persistent memory file + fix
load_session stuck · `2307e47` task completion Slack notification + bot-echo fix ·
`78e9d7e` spawn agent in new Slack thread + msg_too_long fix · `22d0c66`
app_mention handler + channel gate + PostStreamer fallback

**Bang commands (2):** `233c572` exec + task bang handlers · `877ccd6` `!context`
bang command + `/context` ACP schema doc

**Task / session plumbing (7):** `25a76b5` thread session_key through MCP/tasks/
bang · `fa22221` task result retrieval + stream completions · `2e45eec` periodic
cleanup of history/tasks/jobs/sessions · `1fbd430` move session tracking into
AgentManager · `f310f63` task list last_run/created_at · `fbfc33a` job list API +
mrkdwn task output · `f7d2dc0` `kiroclaw reset` command

**CLI / packaging (3):** `9610ae9` unified CLI with uv · `13ac86c` auto-discover
agent-browser skill from npm · `6148f7d` bundle agent-browser wrapper + PATH

**Ralph autonomous loop (3):** `005f4c6` ralph skill (PRD-driven loop) · `941ef61`
ralph tmux management (start/stop/status/logs/attach) · `50b74fb` +`557a9ba`
ralph-executor prompt updates (default model claude-opus-4.6, loops forever)

**Taskei orchestrator (6):** `bc5afe2` Taskei coordinator (FSM, orchestrator, MCP
client) · `65acb69` orchestrator→agent via TaskScheduler · `7759ce7` `!taskei`
bang command + multi-room + session routing · `a8cd8d6` concurrency control +
unified recovery + immediate stop · `c273da1` per-task workspace dirs + context.md
· `938afcd` bootstrap taskei skill + taskei-planner agent spec

**Bootstrap refactor — `.ralph` US-001…US-010 (13):** `50ce66d` extract
`_build_skills_section` · `42ced52` US-001 path constants · `4d6d756` US-002 tool/
MCP constants · `28d3079` US-003 AgentDef + AGENTS registry · `e116979` US-004
non-destructive skill copy · `6801054` US-005 skill source registry · `f48cea9`
US-006 agent→skill mapping · `faf0a88` US-007 prompt templates to files · `a2bae05`
US-008 generic write_agent · `7a045b8` US-009 unit tests · `0964426` US-010
integration test · `f7d2dc0`/`f48cea9` (above) · plus `b69aa5c` MCP merge order +
ralph improvements

**Type/quality (3):** `207113a` resolve all mypy errors (22 files) · `966855e`
type annotations · `d600256` AGENTS.md ralph/runtime-layout docs

**Later features (4):** `ae6c317` cancel button to stop agent mid-response ·
`6911d6f` ACP proxy for TUI event interception · `d7f9a0a` synchronous spawn_agent
mode for subagent delegation · `0a4a6f9` deep-research skill + agent-pool race fix

> Note: a few SHAs span themes (e.g. `f7d2dc0`, `f48cea9`); counts above are
> thematic groupings of the same 49 commits, not additive. Authoritative list:
> `git log --reverse --oneline backup/pre-external-migration ^mainline`.
