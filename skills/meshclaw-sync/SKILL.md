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

- **Upstream (internal):** `/Volumes/workplace/MeshClaw/src/MeshClaw` (package `mesh_claw`)
- **Fork (this repo):** `/Volumes/workplace/KiroClawExternal/src/KiroClawExternal` (package `kiro_claw`)

## Step 1 — Find the candidate commits

There is no merge-base. Bound the candidate set by the version where the fork
was branched (currently internal **v2.6.0**). List upstream's non-merge commits
since that boundary:

```bash
cd /Volumes/workplace/MeshClaw/src/MeshClaw
git fetch -q
# 72301c08 == the v2.6.0 release merge the fork was cut from; update as the
# fork rebaselines. Find it with: git log --oneline --all | grep "v2.6.0 release"
git log --no-merges --oneline 72301c08..origin/mainline
```

For each commit, get the touched files: `git show --stat <sha>`.

## Step 2 — Triage each commit (KEEP vs SKIP)

**SKIP** — anything that only touches Amazon-internal subsystems the fork
removed or stubbed. These have no public-fork equivalent:

| Internal subsystem | Why skip |
|---|---|
| Brazil / `Config` / `AUTOSDE.yaml` / toolbox bundler / `npm-pretty-much` | public build is setuptools + npm/Vite |
| Midway / `mwinit` / MCS / Kerberos / federate / AEA tunnel | auth stubs (`midway.py`, `browser/auth.py`, `tunnel/manager.py`) |
| `builder-mcp` / `arcc` / Quip / Taskei / SIM / mimir | removed integrations |
| CodeArtifact / vendored `claude-agent-acp` | fork uses **public** `npm i -g @agentclientprotocol/claude-agent-acp` |
| Cognito / RUM ids / AEA | removed identity/telemetry |

**KEEP** — generic core fixes: provider/ACP logic, session/cron/memory, Slack
gateway + dashboard, security controls (deny patterns, redaction, trust
matching), token auth, model handling. These are the daily bread of a sync.

**PARTIAL** — a commit that mixes both. Port only the generic hunks; drop the
internal ones. The classic example: the upstream `send_channel_challenge`
change that also flipped tunnel delivery from opt-in to unconditional —
**port the signature/token-claims logic, but KEEP the fork's
`get_tunnel_url() if cfg.slack.use_tunnel_url else ""` gate** (tunnel is
deliberately opt-in here).

If unsure whether a fix is already in the fork, check by **content**, not SHA:
read the upstream diff, then read the corresponding `kiro_claw` file. Verdicts:
ALREADY_PRESENT / MISSING / PARTIAL / N/A_INTERNAL.

## Step 3 — Port a KEEP commit

Path map: `src/mesh_claw/X` → `src/kiro_claw/X`. Symbol/string map (apply
everywhere, including comments and test bodies):

```
mesh_claw → kiro_claw      MeshClaw → KiroClaw      meshclaw → kiroclaw
MESHCLAW_ → KIROCLAW_      .meshclaw → .kiroclaw    meshclaw-lite → kiroclaw-lite
_meshclaw_managed → _kiroclaw_managed     CLI `meshclaw` → `kiroclaw`
```

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
# Per-fix: run the touched test files (override the hardcoded --cov in setup.cfg)
python -m pytest test/test_x.py --override-ini="addopts=" -p no:cacheprovider -q
black src/kiro_claw/<files> test/<files>
isort src/kiro_claw/<files>
flake8 src/kiro_claw/<files> test/<files>
```

Gotchas:
- `setup.cfg` hardcodes `--cov` in `addopts` — always override for fast runs.
- This machine runs **free-threaded CPython 3.13t**; prefix `PYTHON_GIL=0` to
  silence the GIL-re-enable warning. Async tests need `@pytest.mark.asyncio`.
- `tsc -b` (not `--noEmit`) is the real frontend typecheck.
- Do NOT run `black src/kiro_claw test` over the whole tree — the installed
  black churns ~300 unrelated files. Pass explicit file paths only.

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

And keep the OSS-flipped defaults: provider `claude_code`, optional `kiro-cli`
via PATH, Ollama public embeddings, Piper TTS default, Slack enterprise
default-open, lazy boto3/transcribe imports.
