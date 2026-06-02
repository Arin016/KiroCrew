# CLAUDE.md — KiroClaw (backend)

Guidance for Claude Code working in this repository. This file is the
high-signal quick reference; **`AGENTS.md` holds the exhaustive conventions**
(MCP-first rule, injected-message protocols, skill loading, widget protocol,
full module map). Read `AGENTS.md` before non-trivial changes. The frontend has
its own `website/CLAUDE.md`.

## What this is

KiroClaw is an open-source personal AI agent that runs on your own machine —
chat from Slack, a web dashboard, or the CLI; run multi-step tasks unattended;
schedule cron jobs; persist memory across sessions. It drives an LLM through a
pluggable provider layer (default: `claude-agent-acp`) over the ACP JSON-RPC
protocol, plus MCP tools.

- **Backend:** Python package `kiro_claw` in `src/kiro_claw/` (~216 modules).
- **Frontend:** React + TS + Vite SPA in `website/`; built `dist/` is staged
  into `src/kiro_claw/static/dist/` and served by the backend.
- **Distribution:** public GitHub → `pip install` (backend) + `npm`/Vite
  (frontend). Plain setuptools — **no Brazil, no internal build tooling.**

## This is a public OSS fork — do not re-introduce Amazon-internal couplings

This repo is the de-Amazoned public fork of an internal package. When adding or
changing code, **never reintroduce** any of the following (see
`DEAMAZON_REPORT.md` + `MIGRATION_PLAN.md` for the full record):

- Build/infra: Brazil (`Config`, `AUTOSDE.yaml`, `CODE_APPROVERS.yaml`),
  `npm-pretty-much`, toolbox bundler, AIM hooks, CodeArtifact registries.
  Use setuptools + public PyPI / public npm only.
- Services/auth: Midway, `mwinit`, MCS, Kerberos, federate, AEA tunnels,
  Cognito/RUM ids, builder-mcp, `arcc`, Quip, Taskei/SIM/mimir.
- These subsystems are **stubbed** (`midway.py`, `browser/auth.py`,
  `dashboard/handlers/mwinit.py`, `tunnel/manager.py`, `aim_agents.py`): their
  public symbols are preserved as no-ops so the import graph stays intact — keep
  them stubbed, don't wire them back to internal services.
- Defaults that were flipped for OSS (keep these): provider default is
  **`claude_code`** (public `claude-agent-acp`); `kiro-cli` is an *optional*
  backend resolved via `PATH` (graceful `None` when absent); embeddings pull
  from the **public** Ollama registry (`ollama pull qwen3-embedding:0.6b`);
  voice TTS defaults to **Piper** (local), not Polly; Slack enterprise gate is
  default-open (opt-in allowlist via `slack.allowed_enterprise_ids`); `boto3` /
  `amazon-transcribe` / Bedrock are **optional** lazy imports
  (`pip install kiroclaw[voice]` / `[aws]`).

**Keep** the generic security controls (these are not Amazon-specific): AKIA/ASIA
credential redaction, destructive-command deny patterns, `~/.aws` / `~/.ssh`
sensitive-path blocking, SEL audit log.

> Stale references: `website/Config` (Brazil) and `website/AUTOSDE.yaml` are
> leftover internal files not used by the public build — don't treat them as
> the build system, and don't add new ones.

## Build / install

```bash
# Frontend first (so the dashboard is bundled), then backend:
cd website && npm install && npm run build      # → website/dist
cp -R website/dist ../src/kiro_claw/static/dist  # stage into the package
cd .. && pip install -e ".[voice]"               # editable; [voice] = STT extras

# Or use the Makefile (does frontend build + dist staging + venv install):
make build
```

`kiroclaw` and `kiroclaw-browse` are installed onto `PATH`. Self-update is
`git pull` + rebuild + `pip install -e .` + execv restart (no toolbox/brazil).

## Test / lint / type-check

Run the full quality cycle before committing:

```bash
black src/kiro_claw test && isort src/kiro_claw test
flake8 src/kiro_claw test
mypy src/kiro_claw
python -m pytest                 # full suite: -n auto worksteal, --cov (from setup.cfg)
```

**Gotcha — `setup.cfg` hardcodes `--cov` in `addopts`.** Coverage adds heavy
overhead and conflicts with selective runs. For fast iteration, override it:

```bash
# Only tests affected by your changes:
python -m pytest --testmon --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q
# Single file / keyword:
python -m pytest test/test_dashboard_chat.py --override-ini="addopts=" -p no:cacheprovider -q
python -m pytest -k "flush_segment" --override-ini="addopts=" -p no:cacheprovider -q
```

- Async tests **must** carry `@pytest.mark.asyncio` (`asyncio_mode=strict`).
- Mock external processes (`kiro-cli`, `claude-agent-acp`) — never spawn real
  ones in tests.
- `TestCleanupLoopResilience` in `test_session.py` is timing-flaky under
  parallel load but passes in isolation.

## Code style (essentials — see `AGENTS.md` for the full table)

- Line length 100 (black). Python ≥ 3.9; `from __future__ import annotations`.
- `import logging` + `logger = logging.getLogger(__name__)`.
- `asyncio` for all I/O; `@dataclass` for data containers.
- **No hardcoded strings/values in business logic** — constants live in
  designated modules (`AGENTS.md` lists each one).
- flake8 enforces no unused imports (F401), pep8-naming (N806), W504.
- **Never use emojis in the UI** — frontend uses `lucide-react`. See
  `website/CLAUDE.md`.

## MCP-first rule

When adding an LLM-facing CLI command, **also add it as an MCP tool**
(`mcp_cron.py` / `mcp_core.py`). The LLM reliably calls MCP tools but may refuse
bash CLI commands. `kiroclaw-cron` + `kiroclaw-core` are the managed servers
(`agent.py:_MANAGED_MCP_SERVERS`). Full CLI↔MCP mapping is in `AGENTS.md`.

## Platform support

macOS + Linux (x86_64 and ARM/Graviton). **Windows is not supported.** Verify
process management, signal handling, file locking, and system-metrics code on
both macOS and Linux.

## Git conventions

- Do **not** `git commit` or `git push` unless the user explicitly asks.
  Pushing requires separate explicit approval even after a commit.
- Commit format: `<type>: <summary>` (≤72 chars, imperative, lowercase, no
  period), types `feat|fix|refactor|docs|test|chore`, body wrapped at 72.

## Specs

When changing documented behavior, read the relevant spec in
`docs/system-specs/modules/` first and update it **in the same commit**. Do not
create new top-level markdown files unless explicitly asked.
