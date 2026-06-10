---
name: meshclaw-sync
description: How to sync fixes from the upstream MeshClaw mainline into this de-Amazoned KiroClaw fork. Use for porting commits, upstream sync, picking fixes from MeshClaw, daily merge, cherry-pick from internal.
always: false
triggers: meshclaw, upstream, sync, port, cherry-pick, mainline, internal fork, de-amazon, deamazon, pick fixes, merge upstream
---
# Syncing fixes from MeshClaw → KiroClaw

KiroClaw is the **public, de-Amazoned fork** of the internal `MeshClaw` package.
The two repos **share no git history** (KiroClaw was created fresh, not cloned),
so you **cannot** `git cherry-pick`/`git merge`/`git apply` between them. Every
fix must be ported **by content**, path- and symbol-mapped, and re-verified.

This skill is the repeatable daily workflow for picking up upstream fixes
without re-introducing the Amazon-internal couplings the fork deliberately
removed.

## Repo locations

The fork bundles **two** upstream packages into one repo, so a full sync tracks
**both**:

- **Backend upstream (internal):** `/Volumes/workplace/MeshClaw/src/MeshClaw`
  (package `mesh_claw`) → fork `src/kiro_claw/`.
- **Frontend upstream (internal):** `/Volumes/workplace/MeshClaw/src/MeshClawWebsite`
  (the React/Vite SPA, package dir `src/`) → fork `website/src/`. The `mesh_claw`
  backend ships only a server-rendered `static/dashboard.html`; the fork's SPA's
  real upstream is **MeshClawWebsite**, a separate package — sync it too or the
  dashboard silently drifts behind.
- **Fork (this repo):** `/Volumes/workplace/KiroClawExternal/src/KiroClawExternal`
  (package `kiro_claw` + `website/`).

## Step 1 — Find the candidate commits

There is no merge-base. Both upstreams track **`origin/beta-braveheart`** (beta
lands fixes before mainline). Sync is **incremental**: bound the candidate set
by the last-synced tips in `skills/meshclaw-sync/last-synced.txt` (one SHA per
tracked repo/branch). **Scan BOTH repos every run.**

### Backend (`mesh_claw`) — SHA-range incremental
The fork shares the backend's content lineage, so a plain SHA range works:

```bash
cd /Volumes/workplace/MeshClaw/src/MeshClaw
git fetch -q
STATE=/Volumes/workplace/KiroClawExternal/src/KiroClawExternal/skills/meshclaw-sync/last-synced.txt
BETA=$(grep '^beta ' "$STATE" | awk '{print $2}')
MAIN=$(grep '^mainline ' "$STATE" | awk '{print $2}')
git log --no-merges --oneline "$BETA"..origin/beta-braveheart      # new beta commits
git log --no-merges --oneline "$MAIN"..origin/mainline             # new mainline-only commits
```

A mainline-only commit (not reachable from beta) is also a candidate — check
`git merge-base --is-ancestor <sha> origin/beta-braveheart`.

### Frontend (`MeshClawWebsite`) — CONTENT window, not a clean SHA range
The fork's `website/` is a **diverged partial content-snapshot** taken ~2026-06-02
(it was hand-built, not cloned — some post-snapshot upstream commits are present,
some pre-snapshot ones are absent). So **do NOT** trust a SHA range to mean
"not yet present." Instead:

```bash
cd /Volumes/workplace/MeshClaw/src/MeshClawWebsite
git fetch -q
FE=$(grep '^frontend-beta ' "$STATE" | awk '{print $2}')
# Candidate window = commits since the last triaged frontend tip:
git log --no-merges --oneline "$FE"..origin/beta-braveheart
# First-ever frontend sync (or to re-baseline): use the snapshot DATE as the lower bound
# git log --no-merges --oneline origin/beta-braveheart --since='2026-06-02 00:00'
```

For **every** frontend candidate, decide ALREADY_PRESENT vs MISSING **by content**
(read the fork file under `website/src/`), never by SHA reachability. The fork
pre-image often differs from upstream's (divergence), so apply intent, not a patch.

For each candidate in either repo, get the touched files: `git show --stat <sha>`.

**At the END of every sync, update `last-synced.txt`** for BOTH repos (`beta`,
`mainline`, `frontend-beta`) to the new tips. (The fork was originally cut from
the backend v2.6.0 release merge `72301c08`; that is history — the state file is
the live boundary.)

## Step 2 — Triage each commit (KEEP vs SKIP)

**SKIP** — anything that only touches Amazon-internal subsystems the fork
removed or stubbed. These have no public-fork equivalent:

| Internal subsystem | Why skip |
|---|---|
| Brazil / `Config` / `AUTOSDE.yaml` / toolbox bundler / `npm-pretty-much` | public build is setuptools + npm/Vite |
| Midway / `mwinit` / MCS / Kerberos / federate / AEA tunnel | auth stubs (`midway.py`, `browser/auth.py`, `tunnel/manager.py`) |
| `builder-mcp` / `arcc` / Quip / Taskei / SIM / mimir | removed integrations |
| `writing_review/` + `dashboard/handlers_writing_review.py` | dir ABSENT in fork (deleted subsystem) |
| `mcp_gateway/` + `promptfarm/` | dirs ABSENT in fork |
| `code_reviewer` / `secretary` / `taskkeeper` | deleted; `sync_aim_packages` is a no-op stub (`return None`) |
| CodeArtifact / vendored `claude-agent-acp` | fork uses **public** `npm i -g @agentclientprotocol/claude-agent-acp` |
| Cognito / RUM ids / AEA | removed identity/telemetry |
| **non-KiroACP providers**: `providers/claude_code.py` (`ClaudeCodeProvider`), `providers/bedrock.py` (`BedrockProvider`), `cc_agent.py`, `mirror.py` | **KiroClaw is KiroACP (kiro-cli) ONLY.** These modules + the config `claude_code`/`bedrock` factory branches, the `cc_*`/`bedrock_*` `AgentConfig` fields, and the `provider` enum beyond `["acp"]` were deleted. Any upstream commit confined to them is SKIP/NA_INTERNAL. |

### Frontend (`MeshClawWebsite`) SKIP rubric

The SPA mirrors the backend's removals. **SKIP** a frontend commit confined to
any of these (confirm ABSENT by `ls website/src/...`):

| Frontend area | Why skip |
|---|---|
| `apps/code-reviewer/`, `apps/mimir/`, `apps/team-manager/`, `apps/writing-review/`, `apps/auto-research/` | builtin-app dirs ABSENT in fork (their backends are absent/stubbed) |
| `pages/SecretaryPage*`, `pages/writing-review/`, `*Secretary*` slices/tests | Secretary/writing-review absent |
| `meshclaw-ui/` Claude-Code panels, `providers/adapters/claude-code.ts`, `providers/adapters/bedrock*`, Bedrock image/model UI, "agent picker on the Claude Code backend" | **SKIP_NONKIROACP** — fork is kiro-cli only; the provider selector must offer only `acp`. (Porting a commit that *removes* Bedrock from the UI is KEEP — it aligns the SPA to the backend enum `["acp"]`.) |
| `McpGatewayCard`/`SharedMcpGatewayToggle`/`McpPoolable*` (Shared MCP gateway UI) | `mcp_gateway/` backend ABSENT |
| GitFarm workspace-sync (`SyncPanel`, `/api/workspace-sync`), AIM auto-update toggle | absent/stubbed subsystems |
| Harmony Artifactory artifact browse/share UI (`/api/artifactory/*`, `/api/artifacts/*/publish`) | absent subsystem |
| `lcars/` theme, Bikini-Bottom/parody theme refactors, RUM telemetry (`rum.ts` is an inert stub) | cosmetic/internal, no generic core fix |

A commit that adds a **generic SPA mechanism** (a surface, a hook, a renderer)
plus an absent-app wiring line is **PARTIAL**: port the generic part, drop the
absent-app hunk (e.g. a `builtinRegistry.ts` change — port only the lines for
apps the fork HAS, like `/file-explorer`).

**Confirm ABSENT by `ls`, not memory** — a commit confined to an absent dir is
SKIP/NA_INTERNAL. A commit that merely *mentions* an internal name in a
docstring/comment or in an exact-match allowlist of tool-name strings (e.g.
`HEARTBEAT_SAFE_TOOLS` listing `TaskeiGetTask`, `search_arcc`,
`BrazilBuildAnalyzerTool`) is still KEEP — those literals are **inert** in OSS
(the tools never resolve), so copy them verbatim per COPY-not-rewrite rather
than editing the allowlist.

**KEEP** — generic core fixes: provider/ACP logic, session/cron/memory, Slack
gateway + dashboard, security controls (deny patterns, redaction, trust
matching), token auth, model handling. These are the daily bread of a sync.

**PARTIAL** — a commit that mixes both. Port only the generic hunks; drop the
internal ones. Examples:
- The upstream `send_channel_challenge` change that also flipped tunnel
  delivery from opt-in to unconditional — **port the signature/token-claims
  logic, but KEEP the fork's `get_tunnel_url() if cfg.slack.use_tunnel_url
  else ""` gate** (tunnel is deliberately opt-in here).
- A new `_install_<x>_agent()` that pulls `builder-mcp` into a dedicated agent
  JSON — **de-Amazon it to `kiroclaw-core`-only**, matching how the fork already
  rewrote `_install_research_agent` / `_install_knowledge_agent` (see
  `MIGRATION_PLAN.md`). Port the generic *mechanism* (dedicated agent, dynamic
  `tools`-from-resolved-`mcpServers`, prompt), drop `builder-mcp` from the pull
  tuple, and soften any internal-tool prose in the system prompt. Then **adapt
  the tests** that assert the builder-mcp behavior to the kiroclaw-core reality.
- A hunk anchored on a fork stub with no upstream pre-image (e.g. the
  `sync_aim_packages` iterdir loop the fork replaced with `return None`) has
  **no anchor — drop it.**

If unsure whether a fix is already in the fork, check by **content**, not SHA:
read the upstream diff, then read the corresponding `kiro_claw` file. Verdicts:
ALREADY_PRESENT / MISSING / PARTIAL / N/A_INTERNAL.

## Step 3 — Port a KEEP commit

Path map — **backend:** `src/mesh_claw/X` → `src/kiro_claw/X`. **frontend:**
`MeshClawWebsite` `src/X` → fork `website/src/X` (tests: upstream `src/test/` or
`integration/` → fork `website/src/test/` or `website/integration/` — check which
exists). Symbol/string map (apply everywhere, including comments and test bodies):

```
mesh_claw → kiro_claw      MeshClaw → KiroClaw      meshclaw → kiroclaw
MESHCLAW_ → KIROCLAW_      .meshclaw → .kiroclaw    meshclaw-lite → kiroclaw-lite
_meshclaw_managed → _kiroclaw_managed     CLI `meshclaw` → `kiroclaw`
# frontend-specific:
meshclaw-ui → kiroclaw-ui  MeshClawNavBridge → KiroClawNavBridge
source: 'meshclaw' → 'kiroclaw'   /api/config/meshclaw → /api/config/kiroclaw
# KEEP verbatim (load-bearing literals, NOT brand tokens):
'mc-*' localStorage/postMessage keys (mc-nav, mc-unread-slots, mc-auth-expired),
the 'mc_token_' cookie prefix, and inert tool-name allowlist strings.
```

**Frontend divergence:** the fork's `website/` diverged from a ~2026-06-02
snapshot, so a hot file (e.g. `ChatPage.tsx`) is often hundreds of lines off
upstream. Apply the *intent* by content; for big multi-file frontend features,
port files in chronological commit order so later hunks land on earlier context.

**Source hunks:** read the fork file around each hunk first — the fork's
pre-image often differs from upstream's (de-Amazon edits, prior renames), so
apply the *intent*, not a literal patch. When the context doesn't match, find
the semantically-equivalent location and edit there.

**Test files:** if the fork's test file is byte-identical to upstream's
pre-image (modulo the rename), it's safe to regenerate from the post-image:

```bash
git -C /Volumes/workplace/MeshClaw/src/MeshClaw show <sha>:test/test_x.py \
  | sed 's/mesh_claw/kiro_claw/g; s/MeshClaw/KiroClaw/g; s/meshclaw/kiroclaw/g; s/MESHCLAW/KIROCLAW/g' \
  > test/test_x.py
```

Otherwise (the fork diverged — e.g. removed an internal-only test, changed an
`ada credentials`→`aws sso` string) **apply only the added hunks**, don't
clobber the fork's divergence.

**New data files** (e.g. `model_registry.json`): add them to **all three**
packaging manifests or they won't ship:
- `setup.cfg` `[options.package_data]`
- `packaging/kiroclaw-backend.spec` (the explicit data-file list — separate
  from setup.cfg; the PyInstaller DMG misses files not listed here)
- the frontend copy under `website/src/` if the frontend reads it (+ a parity
  test guarding drift)

## Step 4 — Verify (do NOT trust grep)

Brand renames and "is this already fixed" judgments have burned us before by
relying on grep alone. **Run the tests.**

```bash
# BACKEND per-fix: run the touched test files (override the hardcoded --cov in setup.cfg)
python -m pytest test/test_x.py --override-ini="addopts=" -p no:cacheprovider -q
flake8 src/kiro_claw/<files> test/<files>      # the real gate (NOT black --check)

# FRONTEND per-fix: typecheck + the touched vitest files (from website/)
cd website
npx tsc -b                                     # project refs — the real typecheck (NOT --noEmit)
npx vitest run src/test/<File>.test.tsx        # or integration/<File>.integration.test.tsx
```

Gotchas:
- `setup.cfg` hardcodes `--cov` in `addopts` — always override for fast runs.
- This machine runs **free-threaded CPython 3.13t**; prefix `PYTHON_GIL=0` to
  silence the GIL-re-enable warning. Async tests need `@pytest.mark.asyncio`.
- `tsc -b` (not `--noEmit`) is the real frontend typecheck. Do NOT run
  `prettier`/`eslint --fix` to "clean up" — like black, they churn untouched
  code and are not the gate. A frontend port that adds an import MUST ensure the
  target exists in the fork (port the prerequisite helper in the same wave, e.g.
  `utils/monacoLocal.ts` for the Monaco-local commits).
- **The installed `black` (25.1.0) is NEWER than the repo's formatter** — it
  wants to reformat ~300 untouched files AND upstream's own post-image fails it
  too. So `black --check` is NOT the gate. **Do not run black to "fix"
  anything.** The real gate is **flake8**, which **ignores E501** (line length)
  — so the long verbatim-copied lines you port are fine. Verify your edits are
  clean by: (a) `flake8 <files>`, (b) a `>100`-char scan of *only your added
  lines*, (c) comparing black-`--diff` `+`-line counts mainline-vs-yours per
  file (equal ⇒ your edits add no new churn). `apps/builtins/*` also ignores E128.
- **isort failures may be pre-existing** — if `isort --check` flags a file you
  only added a field/kwarg to (no import change), confirm it fails on `mainline`
  too (`git show mainline:<f> | isort --check -`) and leave it; don't churn.
- **Regenerate-from-pre-image trick** for a test/spec file the commit heavily
  rewrites: if the fork file is byte-identical to the upstream PRE-image (modulo
  the rename), it is safe to regenerate wholesale from the POST-image —
  `diff <(git show <sha>^:path | sed '<rename map>') fork/path` == empty proves
  it, then `git show <sha>:path | sed '<rename map>' > fork/path`. The rename
  map: `s/mesh_claw/kiro_claw/g; s/MeshClaw/KiroClaw/g; s/MESHCLAW/KIROCLAW/g; s/meshclaw/kiroclaw/g`.
  Watch for load-bearing literals the broad map also rewrites correctly
  (e.g. `meshclaw browse *` → `kiroclaw browse *`, `mcp__meshclaw-core__` →
  `mcp__kiroclaw-core__`) — grep the result for residual `mesh` tokens.
- **Insert big verbatim blocks with a Python splice**, not Edit, when the block
  is large and clean (e.g. a new function or test class) — extract via
  `git show <sha>:path | awk/sed`, map symbols, then `str.replace(anchor, block
  + "\n\n\n" + anchor, 1)` against a unique anchor. Re-check blank-line spacing
  (flake8 E301/E303) after splicing next to a class member.

## Step 5 — Commit (one fix per commit)

Commit each ported fix separately, citing the upstream SHA in the body so the
provenance is traceable across the history-less boundary:

```
fix(<scope>): <summary>

Ported from MeshClaw upstream <sha>.
<what + why>. <Any internal hunks deliberately skipped and why.>
```

Do **not** `git commit`/`push` unless the user asks; push needs separate
explicit approval.

## Step 6 — Final de-Amazon audit before pushing

Scan the cumulative ported diff for couplings that slipped in (LIVE code, not
comments):

```bash
git diff origin/mainline...HEAD -- 'src/**/*.py' 'src/**/*.json' \
  | grep -iE "^\+" | grep -ivE "^\+\+\+" \
  | grep -iE "midway|mwinit|mcs|kerberos|federate|aea|cognito|codeartifact|builder-mcp|arcc|quip|taskei|brazil|toolbox"
```

Expected: only **comments** and the pre-existing inert `allowed_prefixes`
tuple in `acp/client.py` (`b"arcc"`, `b"builder"`, `b"aim"` — harmless, those
binaries don't exist in OSS). Any **live** new usage is a bug — drop it.

Note: `global.anthropic.claude-*` model ids and `Bedrock` mentions are **not**
couplings — that's the public `claude-agent-acp` adapter's model-id form, used
pre-fork. Keep them.

## What stays KEPT in the fork (never strip these during a sync)

Generic security controls are NOT Amazon-specific — keep them: AKIA/ASIA
credential redaction, destructive-command deny patterns, `~/.aws`/`~/.ssh`
sensitive-path blocking, the SEL HMAC audit log, command-trust matching.

And keep the OSS-flipped defaults: provider **`acp` (kiro-cli, the only
provider)**, Ollama public embeddings, Piper TTS default, Slack enterprise
default-open, lazy boto3/transcribe imports (STT-only; the `[aws]`/Bedrock
extra was removed with the providers).

## Step 7 — Build, verify, and ship (used by the recurring auto-sync)

After porting + the Step 4 verify + the Step 6 audit, a full sync run finishes
with a build and a CR:

1. **Rebuild both macOS DMGs** (the ported backend must ship). Dual-arch from
   one Apple-Silicon Mac via Rosetta — full recipe in `docs/DESKTOP_APP.md`:
   ```bash
   cd website && npm install && npm run build && cp -R dist ../src/kiro_claw/static/dist && cd ..
   SKIP_FRONTEND=1 PYTHON=$PWD/.venv/bin/python bash packaging/build-desktop.sh   # arm64
   # x86_64: arch -x86_64 .venv-x86 (system py3 universal2) + electron-builder --x64,
   #   then RESTORE the arm64 backend into website/electron/backend-dist.
   ```
   Mount-verify each DMG carries the matching backend arch
   (`file …/Resources/backend-dist/kiroclaw-backend/kiroclaw-backend`) — a
   mismatch crashes on launch. Keep electron `package.json` version at `0.1.0`;
   `rm` stale DMGs (`dist/` is not auto-cleaned). DMGs are gitignored artifacts.
   - `.venv`/`.venv-x86` only carry runtime deps from the editable install —
     `pip install pyinstaller` into each before building.

2. **Commit** each fix separately (Step 5 format) and **update
   `skills/meshclaw-sync/last-synced.txt`** to the new branch tips in the final
   commit.

3. **Submit a CR** to mainline:
   ```bash
   cr --destination-branch mainline --open
   # if auto-merge later complains the destination is null:
   #   cr -r CR-XXXXX --destination-branch mainline
   ```
   The CR **title** names the batch (e.g. `[KiroClaw] MeshClaw beta sync
   <date>: N commits ported`). When the batch spans both repos, say so (e.g.
   `... dual-repo sync: N backend + M frontend ported`). The **description MUST
   list, per commit, both what was synced AND what was left out** — every
   KEEP/PARTIAL with its upstream SHA (note backend vs frontend) + one-line
   summary, and every SKIP/NA_INTERNAL/deferred with the reason (writing_review
   absent, builder-mcp internal, mcp_gateway/secretary/auto-research absent,
   SKIP_NONKIROACP, etc.). Provenance across the history-less boundary lives
   entirely in this description.

   Origin = `ssh://git.amazon.com:2222/pkg/KiroClawExternal`. Per the global git
   rule, `commit`/`push`/CR need explicit user authorization — the recurring
   auto-sync cron job **is** that standing authorization; a manual invocation is
   not (ask first).

## Recurring auto-sync (cron)

A durable cron job runs this whole skill every 6 hours (scan → triage+verify →
port → build → commit → CR). It is the standing authorization for commit/push/CR.
It **scans BOTH repos** (`mesh_claw` backend + `MeshClawWebsite` frontend) each
run. If a run finds **zero** new candidates across both, it does nothing and
exits (no empty commit, no CR). If it hits an ambiguous large/PARTIAL commit it
can't confidently de-Amazon, it ports the clean KEEPs, leaves the ambiguous one
un-ported, and **notes it in the CR description** as deferred-for-human-review
rather than guessing.
