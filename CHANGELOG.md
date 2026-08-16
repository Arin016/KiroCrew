# Changelog

All notable changes to KiroCrew are documented in this file.

## [Unreleased]

- **`test_redaction_timing_scales_linearly` no longer fails CI
  intermittently** (observed "Redaction scaled super-linearly: 3.2x, limit
  3.0x" on an otherwise-healthy matcher). The test took ONE
  `perf_counter` sample per input size, so it billed itself for whatever
  the OS gave the core to the sibling pytest-xdist workers — and one
  unlucky reading of the SMALL input, the ratio's denominator, was enough
  to push it over the bound. It now measures with `time.thread_time()`
  (redaction is single-threaded pure-regex work, so per-thread CPU is its
  complete cost) and takes best-of-3 per size, since scheduler noise only
  ever adds and the minimum is the closest estimate of the true cost —
  the same two techniques `TestIsDeniedReDoSResistance` already uses for
  this class of assertion. The 3.0x bound is unchanged and detection is
  intact: a genuinely quadratic implementation still measures ~4.3x.

- **Opted-in MCP servers no longer silently fall back to unpooled backends.**
  On one live host, 988 degradations accrued in 15 hours with no signal: 79%
  were guaranteed-ENOENT pooled spawns of bare commands the gateway daemon's
  systemd PATH cannot resolve, and 20% were crash-loops of servers whose
  declared `env` the shared backend deliberately withholds
  (`mcp_gateway.forward_declared_env` off). The rewriter now resolves bare
  commands through the same augmented search path the MCP probe uses and
  refuses to emit a stub it can prove will degrade — such servers are left
  for the session to launch directly, with a warning naming the fix (absolute
  path / the forwarding knob). The fallback audit log gains the reader it
  never had: gatewayd's `stats` reply now carries per-server fallback counts
  for the last 24 h, and the log rotates at 1 MiB instead of growing without
  bound. (#3495)

- **`TestIsDeniedReDoSResistance::test_cpu_cost_is_immune_to_other_threads_where_process_time_is_not`
  no longer intermittently fails CI** (observed "process_time did not exceed
  thread_time under a 2-spinner burst"). The test's 5-sample loop required
  every single process-time/thread-time comparison to succeed, so one sample
  where a shared CI runner's scheduler didn't interleave the burst threads
  within the narrow measurement window failed the whole test even though the
  invariant it checks — that `_cpu_cost` doesn't see other threads' CPU —
  held on every other sample. Now tolerates a minority (≤1 of 5) of failed
  samples; a genuine break in `_cpu_cost` still fails every sample. (The
  companion flake in `test_mid_dotstar_chain_spam_stays_linear`, tracked in
  the same upstream issue kirodotdev/KiroCrew#3080, was independently fixed
  by #3692 while this PR was open — this change covers the one flaky
  assertion #3692 didn't touch.)

- **The instance token-mint timeout is now user-configurable.** The remote
  `kirocrew token` mint ran with a hardcoded 30s budget, so a user behind a
  slow ProxyCommand/jump host timed out in the mint step even when the ssh
  forward itself came up (the connect flow spawns two proxy-bound ssh
  children, and the mint is the second one). A new
  `instances.mint_timeout_secs` (unset by default: SSH 30s, SSM 90s; clamped
  to [10, 120]) now threads through the tunnel manager to both the SSH and
  SSM mint paths; an explicit value applies to both transports, including a
  value equal to either transport's default. (#3566)

- **A Teams answer no longer gets silently truncated by a rate-limited
  chunk.** The Bot Framework Connector API enforces per-bot rate limits and
  can return HTTP 429, but the Teams outbound send raised immediately with
  no retry, unlike the Discord/Telegram/Webex clients (which all absorb a
  single 429 honoring the server's back-off hint). A multi-chunk answer
  stops at its first failed chunk, so a throttled chunk dropped it and
  everything after it, with only a backend log line. Outbound sends now
  retry once on 429, honoring the Connector API's `Retry-After` header. (#3738)

- **Telegram slash commands (`/new`, `/compact`, `/model`, `/yolo`, `/link`,
  `/unlink`, `/stop`, `/help`, `/queue`, `/steer`) no longer silently break
  in a group or forum-topic chat — without executing a command addressed to
  a different bot in the same group.** Telegram's own clients append
  `@BotUsername` to a slash command in any chat with more than one
  participant/bot — standard client behavior triggered by registering a
  command menu, not something the bot's UI controls — but the command
  parser matched the raw token verbatim against alias sets defined without
  that suffix. In the forum-topic supergroups this integration explicitly
  supports, every command fell through to being sent to the LLM as ordinary
  chat text with no error. A trailing `@BotUsername` is now stripped before
  alias matching — but only when it names THIS bot: Telegram fans a command
  addressed to a different bot in the same group out to every bot present
  (Bot API convention is to ignore what isn't addressed to you), so
  stripping any mention unconditionally would let e.g. `/yolo@OtherBot on`
  match this bot's own alias and enable auto-approval here. The gateway now
  resolves its own username via `getMe` at startup and only strips a mention
  that matches it (case-insensitively); any other mention, or none resolved
  yet, is left attached and falls through as unrecognized. (#3734)

- **`agent.dangerously_skip_permissions` no longer treats a string value as an
  affirmative grant.** The config loader coerced this field with a bare
  `bool(...)`, so a plausible config shape like `"dangerously_skip_permissions":
  "false"` (any non-empty string is truthy in Python) silently activated the
  standing, unattended tool-auto-approve grant this key controls — every tool
  call gets auto-approved with no confirmation prompt — instead of the
  explicit disable the value said. Now requires a real boolean, matching
  every other boolean field in the loader; a non-bool value falls through to
  the next accepted spelling instead of being read as a grant. (#3730)

- **A session no longer risks two interleaved turns after a mid-turn reset.**
  `record_failure`'s circuit breaker calls `reset(key)` while the failing
  caller still nominally holds that session's turn semaphore; `reset` pops the
  session and tears it down without touching the semaphore. If a concurrent
  `get_or_create` for the same key registered a replacement session in that
  window, the original caller's later `release(key)` — a fresh lookup by key,
  not the specific session object it acquired — released the REPLACEMENT's
  semaphore instead, an over-release that could hand out a surplus permit and
  let a third message start a turn while a second was still in flight on the
  same live provider session. The per-session semaphore is now a
  `BoundedSemaphore`, so a stray release beyond its one permit raises instead
  of silently succeeding; `release()` catches that specific error and logs a
  warning rather than propagating into a caller's `finally`. (#3749)

- **Notes: a failed GitHub token Save/Clear in Settings no longer gets stuck
  disabled with no explanation.** Neither the Save/Clear button handlers nor
  the `savePat` action they call had any error handling, so a rejected
  request (an invalid token, a transient network error) left `busy` stuck
  `true` — the button permanently disabled — with neither the success
  confirmation nor any error shown, an unhandled promise rejection, and the
  only recovery being to close and reopen Settings. Failures are now caught
  and reported inline next to the button, styled like the sibling per-vault
  knowledge-toggle error state, and the button always recovers. A review
  pass caught a sibling with the same root cause: the vault Remove confirm
  button's `onForget` call still swallowed its failure into the shared
  `error` banner, which only renders in the main-editor branch and is
  invisible while Settings is open — the confirm bar also dismissed itself
  immediately, so nothing indicated the removal was even attempted. Remove
  now catches inline too, keeps the confirm bar up on failure so the user
  can retry without reopening it, and clears on success. (#3743)

- **A folder knowledge source added from the dashboard can now be started.**
  The row's `sync_status` was stored twice — as a table column and inside the
  properties JSON — and the create path wrote `pending_confirmation` only into
  the JSON, leaving the column at its `pending` default. The dashboard list
  reads the column, so a freshly-added `local_folder` / `obsidian_vault` source
  showed a Pause button instead of the Confirm button that starts the scan and
  sat at "pending · 0 items · never synced" forever (the workaround was Pause
  then Resume). Both insert paths (`add_source` and the auto-source path used
  by drop-folder and project-docs sources) now derive the column from the
  passed properties, and the store migration repairs already-divergent rows on
  open, so existing stuck sources become startable without the workaround. (#3701)

- **The Speech-to-Text settings page no longer offers to install Whisper when
  the provider is AWS Transcribe.** With `stt.provider = "transcribe"` the page
  showed an "Install Whisper" button (installing an engine Transcribe never
  uses, so Status stayed "Not installed" forever), listed a Python/Whisper
  prerequisite toolchain that is irrelevant to Transcribe, and rendered a
  Runtime row that could only ever read "Native" because the backend never
  serves `docker_mode`. The install button is now hidden for Transcribe, the
  prerequisite block surfaces the real requirement — installing the `voice`
  extra into the gateway's own interpreter, plus a restart hint — the backend
  refuses `POST /api/stt/install` for Transcribe instead of silently installing
  the wrong package, and the dead Runtime row is gone. Where no install channel
  can make the extra importable (frozen build, pip-less interpreter, PEP 668
  externally-managed python) the page shows an honest unsupported notice
  instead of a command that cannot succeed, and a missing ffmpeg — which
  Transcribe's availability check treats as optional even though browser
  recordings need it — is now flagged with its install command even while the
  status reads ready. (#3559)

- **`kirocrew` commands start up to ~0.8 s faster, and each MCP stdio server
  drops ~58 MB of resident memory.** `cli.py` imported its full 132-subcommand
  dispatch table at module scope — including the Slack gateway, the dashboard
  state module and (through it) numpy — so every CLI invocation and every
  long-lived MCP backend process (`mcp-core`, `mcp-cron`, `mcp-computer`) paid
  ~1.3 s and ~112 MB for subcommands that never run. The four heavy import
  statements now execute inside the one dispatch branch that uses each name,
  cutting a fresh `import kiro_crew.cli` to ~0.5 s / ~54 MB. Each command now
  pays only for the modules its own branch uses: the MCP stdio servers and
  most verbs save the full ~0.8 s / ~58 MB, while commands that dispatch into
  the deferred modules (e.g. `gateway`, `cron`) save the portion they don't
  touch. A ratchet test keeps the deferred modules out of module scope and
  verifies every deferred import still resolves. Behavior is unchanged: the
  entry point, the fail-closed security prelude, and all subcommand dispatch
  are untouched. (#3504)

- **A managed deployment can now withhold the external services the core offers
  unconditionally.**
  Three surfaces had no composition point. Two are installable-content registries
  — skill discovery (skills.sh) and MCP server discovery (the official MCP
  registry) — which fetch from the public internet and then offer to install what
  they return, but hardcoded their public provider at registration time. The third
  is cloud deployment: `kiro_crew/deploy/` provisions S3, CloudFront, IAM roles and
  a reaper Lambda in the operator's own account and carried no capability gate at
  all, so `capabilities.publish` (which bounds publish-provider destinations) did
  not reach it. Together that made "source installable code only from our own
  registry, and never provision cloud infrastructure" impossible to express without
  patching the core — a hard blocker for any deployment where third-party code must
  be reviewed first, or where provisioning is centrally controlled. A new
  `external_access` platform slot adds `admits_registry(kind, name, api_base)` and
  `admits_cloud_deployment(target)`. A refused registry is never registered, so it
  is absent rather than failing per request; a refused cloud deployment makes the
  deploy surface report itself disabled — so the UI hides the console instead of
  rendering one whose every button 403s — and refuses every mutating route, wrapped
  at registration so a new endpoint is gated by being listed rather than by
  remembering an in-handler check. Both decisions take the concrete target as well
  as a label, because a name is self-chosen while the URL or target determines
  where bytes go, so an allowlist stops admitting a provider that later repoints at
  a different host instead of letting it inherit trust from its name. Both outcomes
  are SEL-audited: a log carrying only denials cannot show whether the permitted
  path was ever taken. The public default admits everything, so an ordinary install
  is unchanged.

- **An MCP server that declares `env.PATH` no longer loses its inherited PATH.**
  A spec's `env` is applied per key, so naming one directory to add — a Node
  version manager's shim dir, say — replaced the child's PATH instead of
  extending it, leaving the server with only that one directory. A launcher that
  execs a sibling binary then died with "not found" for a binary that was
  plainly installed, while the dashboard probe — which merged rather than
  replaced — reported the same server healthy, so nothing in the UI
  distinguished it from a working server. The full effective PATH (the spec's
  own entries first, deduped) now backs the probe, command resolution, and the
  value written into the agent config, so "probes healthy" and "works in a
  session" can no longer disagree.

- **Every emitted MCP config surface now goes through one env normalization
  point (`env.emit_env`).** The agent config, the kiro-global entries the sync
  creates, and the Claude Code `~/.mcp.json` sidecar all expand a declared
  `env.PATH` the same way, so a server can no longer work under one consumer
  and die under another. The cosmetic `kiro-cli mcp add` subprocess inside the
  sync — an unsynchronized second writer whose output the rebuild overwrote —
  is removed, and the discover→write sequence is a single mutex-serialized
  entry point (`sync_discovered_servers`) shared by the sync endpoint, the
  restart pre-sync, and the config watcher, closing their read-modify-write
  race.

- **The Online badge now means "tools usable", dated.** A probe whose
  `initialize` succeeds but whose `tools/list` fails reports an error instead
  of `ok` with an empty list; every probe result carries `probedAt` so the
  dashboard can show when a status was established instead of presenting a
  cached one as current; and a managed server served from its in-process
  declaration is marked `declared` — the tool list is correct, but nothing
  verified the server can start — instead of rendering identically to a
  handshake-proven server.

- **Apply & Restart now really mounts a newly installed server, and says so
  honestly when it cannot.** The restart path runs the one serialized
  discover→write entry point and reconciles the consumed agent config
  unconditionally, so an edit that produces an empty discovery delta (a
  `disabled: true` flip, a changed `env`) is still written out instead of
  being skipped as "nothing new". A reconcile that FAILS is reported through
  `mcp_sync_ok` on the restart response rather than being dressed up as a
  successful apply.

- **Publishing an artifact to the public internet now requires an explicit
  acknowledgment, and an operator can remove the path entirely.** The warning
  next to each confirm button could be scrolled past and read as decoration, and
  the public-web destination was the one publish destination exempt from the
  operator's publish policy — `deploy-web-aws` was appended to
  `/api/publish-providers` unconditionally and `POST /api/deploy/deploy` consulted
  no ceiling, so a team that had closed every other destination still had a
  one-click path to a world-readable URL. Every surface that creates the public
  resource (the Publish panel, its scan-override branch, and **Confirm deploy** on
  a pending entry) now ends at a blocking dialog that names the artifact, states
  that anyone with the link can view it, states how long the link stays public,
  and requires pressing **I understand, publish publicly** — a button that is
  neither pre-focused nor the default action, so no keystroke that dismisses an
  ordinary dialog can publish by accident. The destination itself now goes through
  the same `capabilities.publish` chokepoint as artifact publish: closing it in the
  trust-root policy (or narrowing `publish.allowed_destinations` in `config.json`)
  removes the button from the provider registry **and** answers 403 from
  `/api/deploy/deploy` and `/api/deploy/pending/{id}/confirm`, including for the
  agent-mediated `deploy_artifact` preview. Operators who had already narrowed
  `publish.allowed_destinations` must add `deploy-web-aws` to keep deploying. (#3599)

- **The Linux desktop app no longer shows two title bars on GNOME-family
  Wayland desktops.** The window manager's native decoration used to stack on
  top of the dashboard's own 42px header, wasting vertical space and
  duplicating controls. On Wayland sessions of desktops that prefer
  client-side decorations (GNOME, Ubuntu, Unity, Pantheon, Budgie) the window
  now drops the native frame: the header doubles as the title bar via an
  injected drag region, and a minimize/maximize/close cluster is injected at
  the header's top-right (frameless Linux gets no OS-painted controls, unlike
  the macOS traffic lights and the Windows caption overlay). X11 sessions,
  desktops that expect server-side decorations (KDE, XFCE, tiling window
  managers — including hybrids like Regolith that also report a GNOME token),
  and unknown environments keep the native frame: frameless X11 windows lose
  mouse edge-resize, which would be worse than the doubled bar. The
  `linuxFrameless` key in the desktop app's own config (Connection → Open
  Config File, also in the tray menu; read once at launch) forces either
  shape. On frameless windows the menu bar auto-hides (press Alt to reveal
  it) — kept visible it would re-create the stacked-bars problem, removed it
  would take the menu away entirely. Connection windows follow the same
  decision. (#3606)

- **A lesson from a previous embedding-model generation could no longer get
  silently deleted or offered as a false contradiction.** `write_lesson`'s
  semantic dedup and `find_contradiction_candidates` compared raw embeddings
  with a cosine helper that silently truncated a dimension mismatch to the
  shorter vector instead of rejecting it, so a row embedded at a different
  dimensionality (e.g. left over from an old embedding model) could score a
  plausible-looking ~0.5 similarity against an unrelated new rule — landing
  either past the 0.85 dedup line (deleting the old lesson as a "duplicate")
  or inside the [0.4, 0.85) contradiction band (offered as a false
  contradiction candidate). Both paths now converge onto the same
  dimension-checked, float64-precision scorer the ranking paths already use,
  which also removes a per-row query re-derivation from both loops. (#3466)

- **Computer use no longer costs a 109 MB backend process per chat when it is
  off — or on platforms where it cannot run at all.** `kirocrew-computer` was
  registered into the agent spec unconditionally, and the keystone enable was
  only checked *inside* the process the spec had already caused kiro-cli to
  spawn: it suppressed the tool list, never the process. Every chat process paid
  ~109 MB for a disabled capability, including every `spawn_run` subagent, and
  on Linux/Windows it paid that for a feature with no driver (macOS is the only
  supported platform) — measured at 16 processes / 1.75 GB on one Linux host.
  The server is now withheld from the emitted spec, unless this is macOS *and*
  the keystone is on; enabling it from Settings rebuilds the spec before
  restarting sessions, so the tools still appear in the session you are sitting
  in. Your `tools` entries are left untouched — a ref whose server the spec does
  not define resolves to nothing, so a mount you had narrowed to a single tool
  comes back exactly as you left it. Only the entry's own `autoApprove` and
  custom `env` keys are reset by an off/on cycle and need re-applying,
  deliberately: restoring an approval from a file the agent can write would
  bypass the PreToolUse gate. The two in-process checks are kept as defence in
  depth for a mid-session disable. (#3482)

- **Folder-write audit lines now name the internal component that made the
  write, instead of inferring the caller's identity from the internal secret's
  presence.** Every MCP stdio server now declares its component name on
  loopback gateway requests (`X-Internal-Caller`, attached centrally by the
  shared request helpers), and the folder endpoints validate it against a
  known-caller set before trusting it into the security event log's `caller`
  field — `source` stays in SEL's interface vocabulary (`mcp`), so operator
  queries over `source == "mcp"` keep matching folder writes. The old
  inference was correct only while exactly one internal caller existed — a
  second internal caller would have silently inherited the same label. An
  authenticated internal write with a missing or unrecognized caller name is
  audited as `caller="unknown-internal"` with a warning, so a new caller shows
  up loudly until it is added to the known set alongside its own test. Browser
  writes still audit as `dashboard`; the caller header alone grants nothing.
  (#3503)

- **`kirocrew policy show` no longer hides the 139 built-in denied-command
  rules from the agent.** The rules are visible and configurable to the
  user (Settings → Security), but the agent's only way to discover them was
  to attempt a command and be refused — so it could plan multi-step work
  that turned out to be impossible from the first step, walking the user
  through setup effort (e.g. exporting AWS credentials) for a task a
  hard-denied command would block later anyway. `policy show` now prints
  the rule count grouped by category on every install, enterprise policy or
  not; `--ids` lists each category's rule ids for citing a specific rule
  when relaying a refusal. (#3454)

- **Side-panel oversize-question refusal now reports an accurate character
  target for every script, not just emoji.** The refusal derived its
  character count from a fixed worst-case floor (4 bytes/char, the emoji
  case), so an ASCII user over the byte budget was told to cut to ~8,192
  characters when trimming a single character would do (4x over-deletion),
  and a zh-CN user (3 bytes/char) was told 8,192 when ~10,922 actually fit.
  The target is now derived from the submitted question's own byte density,
  so it's accurate per script — the all-emoji case is unaffected (it already
  sat at the 4-byte floor). (#3432)

- **The skill browser no longer serves a different skill than the one you asked
  for.** Three `package/` lookups compared a bare leaf name and returned the
  first hit, so a request for `package/<name>` could answer with a file under
  `<root>/<Pkg>/<name>`, or with whichever of two identically named files the
  filesystem happened to yield. Exact keys now decide first, leaf matching
  survives only where it is unambiguous, and a real collision resolves to
  nothing — a 404, with the competing candidates logged — because the
  `package/<path>` key cannot express which of the two files was meant. Every
  lookup that previously resolved correctly still resolves to the same file.
  **Edition maintainers:** roots the core already keys itself (`~/.kiro/skills`,
  the data home, configured extra paths) are no longer *also* enumerated under
  `package/`, which previously presented an editable skill as a read-only
  package one. A stored reference to one of those duplicate `package/` keys
  stops resolving; the file itself is untouched and still reachable under its
  canonical key, but the stored reference has to be re-pointed. (#3369)

- **MCP gateway daemons no longer leak when their launcher dies.** A `gatewayd`
  whose launcher exited without signalling it (a torn-down `pytest` run, for
  example) used to stay resident forever — invisible to every sweep, ~27 MB
  each, accumulating without bound. The daemon now watches its own listening
  socket path and gracefully self-exits once the path is gone (three
  consecutive checks, POSIX only), and the untracked-orphan sweep reaps any
  gatewayd whose `--socket` path no longer exists on disk, TERM-first so
  pooled backends drain cleanly. (#3315)

- **Aggregate memory ceiling across all concurrent agent spawns.** The cgroup
  memory limit was per-spawn only (65% of RAM each), so many concurrent
  subagents could collectively request several times host RAM without any
  single limit breaching. The gateway now also caps their shared parent slice
  (`kirocrew-agents.slice`) at 80% of RAM plus an aggregate task ceiling —
  override via `resource_limits.max_total_memory_mb` /
  `max_total_processes` — and logs which scopes were OOM-killed when the
  aggregate ceiling engages. (#3316)

- **Slack manifest: private channels now work out of the box.** The shipped app
  manifest adds the `groups:history` and `users:read` bot scopes and subscribes
  to the `message.groups` event, so a tracked private channel actually delivers
  messages and profile lookups resolve real names. **Existing installs are not
  fixed by upgrading alone**: Slack only grants new scopes on reinstall — update
  the app's manifest (or re-import it), then reinstall the app to the workspace
  and copy the new bot token. (#3206)


## [0.3.0-insider.9] — 2026-08-16

### Highlights

- **Browser MCP tool**: native command-bus gateway + browser panel integration
- **Session list redesign**: new row layout with colour bar, tighter spacing
- **Phone-width responsive**: 20+ layout fixes for narrow viewports
- **Memory posture**: defer crons and refuse spawns under critical memory
- **Agent resource limits**: cap agent memory via dedicated user slice
- **Test suite robustness**: full local suite passes without external dependencies

### Features

- **browser**: native command-bus gateway + browser MCP tool
- **sessions**: redesign the session list rows
- **jira**: show linked issues and render Jira issue details in side panel
- **dev-fleet**: allow force-removing kept worktrees in Prune modal
- **mcp**: dashboard-control server as an assignable per-agent set
- **cli**: surface the built-in denied-command catalog in policy show
- **cron**: show a script cron's source in the job detail view
- **chat**: flag a conflicted pull request on its session chip
- **chat**: show an indicator while older messages are loading
- Cap agent memory via dedicated user slice
- Defer crons and refuse spawns under critical memory posture
- Preview notification sound on per-category dropdown change
- Render the app storefront from the published catalog
- **prepare-pr**: extract local reviewer briefs from CI workflows

### Bug Fixes (88 total)

- **security**: lock the revoked-nonce store down through restrict_to_owner
- **memory**: apply the session-recognition gate to the mutating memory routes
- **kas**: mirror context_usage into SSE stream via the single writer
- **acp**: give backend-subagent permission requests main-agent mode parity
- **sandbox**: apply resource limits to synchronous spawns after exec
- **mcp-gateway**: stop opted-in servers from silently degrading to unpooled backends
- **mobile**: pin the command palette to the visual viewport, not the layout one
- **mobile**: let the software keyboard resize the layout viewport
- **dashboard**: strip agent bookkeeping keys on config PUT
- **tests**: make full local test suite pass without external dependencies
- **tests**: eliminate orphan temp dirs and add AUTOSDE rule
- 20+ phone-width responsive layout fixes across all panels
- … and 65 more bug fixes (see commit log)

### Refactoring

- **acp**: consolidate KAS wire parsing + declare provider H14 caps
- **acp**: convert harness-identity negatives to positive form (KAS Group C)
- **terminal**: unify panel shell, remove dead transfer plumbing
- Flatten nested guards and drop dead code in toplevel
- Lay the top bar out in three tracks

