# MeshClaw → KiroClaw migration

Imports a previous **MeshClaw** install (`~/.meshclaw`) into the current
**KiroClaw** home (`~/.kiroclaw`, or `$KIROCLAW_HOME`).

The MeshClaw and KiroClaw on-disk formats are compatible (identical `memory.db`
schema, forward-compatible `config.json`), so this is a **copy + selective
config merge**, not a transform.

## Usage

```bash
# Preview what would happen — writes nothing
python transfer/meshclaw_to_kiroclaw/migrate.py --dry-run

# Run the migration (prompts for confirmation)
python transfer/meshclaw_to_kiroclaw/migrate.py

# Run without the prompt
python transfer/meshclaw_to_kiroclaw/migrate.py --yes
```

Requires KiroClaw to be installed — the engine imports `kiro_claw` so the target
path resolution matches the app exactly (including `KIROCLAW_HOME`).

## What it does

- **Backs up** the current `~/.kiroclaw` to `~/.kiroclaw.backup-<UTC-timestamp>/`
  before writing anything (only if the target is non-empty).
- Treats `~/.meshclaw` as **read-only** — the source is never modified.
- **Idempotent**: re-running skips files that already exist; safe to run twice.

### Copied

| Category | Notes |
|---|---|
| `sessions/` | union by filename |
| `memory.db` | copied if target empty, else row-merged (`INSERT OR IGNORE`) |
| `workspace/`, `uploads/`, `skills/`, `artifacts/`, `tasks/`, `plan_memory/`, `agent-metadata/`, `apps/`, `cron-history/` | copied |
| `crons.json`, `session_map.json`, `tags.json`, `autonudge.json`, `hooks.json`, `folders.json`, `notifications.jsonl`, `playwright-config.json` | copied with `.meshclaw`→`.kiroclaw` path rewriting |
| `config.json` | user-data keys merged in; install-specific KiroClaw keys win; legacy keys (`tunnel`, `mcp_gateway`) dropped |

### Skipped (never copied)

- Per-install identity / crypto: `.env`, `.local_secret`, `sel_hmac.key`,
  `token_signing.key`, `telemetry_salt`
- Runtime / process state: `*.lock`, `*_pids.txt`, `session_pid_*.txt`, `*.log`
- `memory_index.db` — the FTS index is rebuilt from `memory.db` on next launch.

## Rollback

If you want to undo a migration: stop KiroClaw, then restore the backup:

```bash
rm -rf ~/.kiroclaw && mv ~/.kiroclaw.backup-<timestamp> ~/.kiroclaw
```

## Tests

```bash
python -m pytest transfer/meshclaw_to_kiroclaw/test_engine.py
```
