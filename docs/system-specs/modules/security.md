# Security Module

Last Updated: 2026-06-29 (challenge-and-redirect for Slack REMOVED — messages processed inline; SEC-009 loud no-isolation fallback + `agent.sandbox_allow_no_isolation`; time-limited safety override replacing permanent YOLO, per-segment deny pattern evaluation, 3-tier interactive trust escalation, SSH tunnel -N flag fix)

## Overview

KiroClaw implements defense-in-depth security across multiple layers: OS-level process isolation, credential path protection, input/output validation, authentication, authorization, and audit logging. This document consolidates all security controls and the vulnerabilities they address.

## Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| XPIA credential theft | LLM reads `~/.aws`, `~/.ssh` via `fs_read` or `cat` | Hook-layer path blocking + OS sandbox |
| XPIA data exfiltration | LLM embeds secrets in URLs posted to Slack/dashboard | Output scanning + URL redaction |
| Cross-origin WebSocket hijack | Malicious page connects to `ws://127.0.0.1:5476/api/ws` | Origin header validation |
| Cross-origin mutation (CSRF) | Malicious page POSTs to dashboard API | Origin/Referer validation on non-safe methods |
| Unauthenticated remote access | Dashboard bound to `0.0.0.0` | Loopback-only by default (`127.0.0.1`); when user opts in via `dashboard.url`, token auth middleware requires HMAC-SHA256 signed, IP-pinned, single-use tokens on every request |
| Unauthenticated remote access (AEA tunnel) | `tunnel.enabled` exposes dashboard via public HTTPS URL | Double auth: Tunnels validates Midway OIDC at edge + KiroClaw token auth middleware. Security gate refuses tunnel start without token auth active. Owner-only access (Tunnels restricts by username). SEL audit on connect/disconnect/denial |
| Unauthorized dashboard access | No auth on localhost | Token auth middleware on all requests (loopback bypass removed); file-based IPC secret for internal paths |
| Non-owner Slack interaction | Any workspace member clicks YOLO/approve buttons | 5-layer owner verification |
| Fail-open owner lock | `KIROCLAW_OWNER_ID` unset → no check | Deny-by-default: refuse connect + reject messages |
| MCP input injection | Malformed/oversized tool inputs from LLM | Centralized schema validation (`validation.py`) |
| MCP response DoS | Unbounded tool output fills memory | Response truncation at 100K |
| Destructive CLI commands | LLM runs `rm -rf /`, `git push --force` | 113 denied command patterns + 55 suspicious bash patterns with per-segment fnmatch glob matching (`security.py`) |
| Frontend XSS | `dangerouslySetInnerHTML` with unsanitized content | DOMPurify + safe DOM APIs + Mermaid `securityLevel: 'strict'` (iframe sandbox) |
| YOLO mode abuse | Unbounded auto-approve window | Time-limited safety override: Slack 30min, dashboard 6h, config 24h (no permanent mode). Re-auth required after expiry. SEL audit on every lifecycle event |
| Trust reads bypass | Read-only command classification tricked into approving writes | Deny-by-default: rejects redirections, command substitutions, newline separator bypasses. Prefix matching only |
| Port-forward auth bypass | socat/ssh -R makes remote traffic appear as 127.0.0.1 | Loopback bypass removed; all requests require token auth. File-based IPC secret for internal paths |
| Observe-mode context poisoning | Non-owner messages in shared channels influence LLM context | `channel_history.push` gated on `_user_authorized` |
| Outbound data exfiltration | LLM exfils data via `curl -d @file`, `nc < file` | Egress patterns in `SUSPICIOUS_BASH_PATTERNS` + `redact_exfiltration_urls()` on output |
| Credential file permissions | `.env` readable by group/other | `chmod 600` enforced at credential load time + setup wizard |
| SEL event forwarding leaks | Forwarded audit events contain raw credentials | `redact()` applied to all string fields before callback |

## Modules

### OS-Level Sandbox (`sandbox.py`)

Hides credential paths from kiro-cli subprocess tree using platform-native isolation:

- **Linux**: user + mount namespace — `unshare(CLONE_NEWUSER)` → identity UID/GID map → `unshare(CLONE_NEWNS)` → bind-mount empty dirs
- **macOS**: `sandbox-exec` with Seatbelt profile denying file reads

#### Sandbox Modes

| Mode | Config value | Hides | Accessible | Env scrub |
|------|-------------|-------|------------|-----------|
| **Standard** | `"auto"` (default) | `.gnupg`, `.gpg`, `.config/gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` | `AWS_SECRET*`, `AWS_SESSION*`, `SSH_AUTH_SOCK`, `GNUPGHOME`, `GIT_ASKPASS` |
| **Strict** | `"strict"` | All of the above + `.aws`, `.ssh`, `.kube` | Only `~/.ssh/known_hosts` | Same as standard |
| **Off** | `"off"` | Nothing | Everything | Nothing |

**Standard mode** (new default) enables git-over-SSH, AWS CLI via `credential_process`, and kubectl while maintaining OS-level isolation on non-workflow credential stores. Env vars are scrubbed in ALL modes — `credential_process` reads from `~/.aws/config`, not env vars.

**No-isolation fallback is loud (SEC-009)**: when no sandbox backend is available (e.g. macOS >= 26, or Linux without user namespaces), `wrap_argv()` still runs the agent (graceful — the host is not bricked) but no longer degrades silently. It emits a one-shot loud `SECURITY` warning unless the operator acknowledges the risk via `agent.sandbox_allow_no_isolation=true` (config-modal editable), which demotes the message to info level.

**Why standard is safe**: The hook layer (`is_sensitive_path()`) still blocks direct file reads of `~/.aws/*` and `~/.ssh/*`. Denied commands block `cat`/`head`/`tail`/`python open()` on those paths. `redact_credentials()` catches any credential patterns that leak through tool output. Three independent layers must all be bypassed simultaneously.

Config: `agent.sandbox` in `config.json` — `"auto"` (standard), `"strict"`, or `"off"`.

Wired into `AcpClient._spawn()` — all kiro-cli processes are sandboxed. Parent KiroClaw process is unaffected. Zero new dependencies (stdlib + system binaries only).

**Linux namespace sandbox**: Fork child → child calls `unshare(CLONE_NEWUSER)` → parent writes identity UID/GID map (`uid uid 1` / `gid gid 1`) to `/proc/<child>/{setgroups,uid_map,gid_map}` → child calls `unshare(CLONE_NEWNS)`, sets mount propagation private (`MS_REC|MS_PRIVATE`), bind-mounts empty dirs over credential paths (per mode), scrubs sensitive env vars (`AWS_SECRET*`, `SSH_AUTH_SOCK`, etc.), and execs the agent. Two-pipe synchronization ensures correct ordering. The child retains the real UID/GID so all toolchains (JVM ByteBuddy, brazil-build, Gradle, npm, etc.) work without workarounds. Implemented as a Python launcher script (`_build_launcher_script()`) spawned by `namespace_argv()`.

**AL2 toolbox fix**: On AL2, kiro-cli is a bash shim (`aim sandbox --client kiro-cli`). `_resolve_real_kiro_bin()` bypasses the shim by resolving the real ELF binary (magic byte check rejects shell scripts).

### XPIA Hardening (`security.py` + `hooks.py`)

**Sensitive path protection** — blocks at the hook layer before tool execution:
- `is_sensitive_path(path)` — checks `fs_read`/`ReadFile` targets against sensitive dirs
- `is_sensitive_bash_command(cmd)` — regex matches `cat`, `head`, `tail`, `less`, `cp`, `scp`, `python open()`, pipe redirects targeting sensitive paths
- `hooks.on_tool_call` runs **both** `is_sensitive_path` and `is_sensitive_bash_command` on the **normalized** tool title regardless of the kiro-cli `Reading: `/`Running: ` display prefix. The claude-agent-acp adapter sets a file-read tool's title to the bare path and a Bash tool's title to the bare command (no prefix), so gating either check on the prefix would let credential reads through on the Claude Code provider. `is_sensitive_path` resolves the title as a path (a bare `~/.aws/credentials` matches; a `cat ~/.aws/credentials` command resolves to a non-sensitive path and is caught by `is_sensitive_bash_command` instead).
- Sensitive paths: `~/.aws`, `~/.ssh`, `~/.gnupg`, `~/.gpg`, `~/.config/gcloud`, `~/.azure`, `~/.docker/config.json`, `~/.kube/config`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, `~/.kiroclaw/.env`

**Write-only config protection** (`is_sensitive_write_path` in `security.py` + `hooks.py`) — runtime config files are protected against *modification* by agent tools while staying *readable*:
- `~/.kiroclaw/config.json` and `~/.kiroclaw/config.local.json` are in a write-only tier (`_WRITE_PROTECTED_HOME_PATHS`), deliberately NOT in the read+write `_SENSITIVE_HOME_DIRS` list above — the dashboard file viewer, `cat`, and knowledge indexing legitimately read config.
- `is_sensitive_write_path(path)` is a superset of `is_sensitive_path(path)`, sharing the same `_path_in_home_dirs` resolve/casefold core so the two gates can't drift. `hooks.on_tool_call` denies a file-EDIT tool call (ACP `edit` kind) whose `path`/`file_path` resolves to a config file.
- Empty/unknown ACP tool kinds are intentionally left to the load-time clamp backstop rather than hard-denied, to avoid over-blocking config reads that arrive without a kind (governance's shape inference can apply both read+write scopes because it is a permissive policy intersection; this gate is a hard deny). Bash writes (`tee`, `>`, `sed -i`) likewise fall to the clamp.
- The operator edits config out-of-band via the dashboard config API / CLI, which do not route through this gate.

**Load-time resource-limit clamp** (`config/loader.py`) — defends against a config-loader bound bypass: the dashboard config API rejects out-of-range writes, but a direct edit of `config.json` (any process as the same OS user, or a prompt-injected agent with file-write access) bypassed that gate.
- `KiroClawConfig.load()` calls `_clamp_security_bounds(data)` on the disk-read path (before caching) so cache hits and the `GET /api/config/kiroclaw` serialization both report clamped values.
- Clamped knobs: `agent.subagent_auto_max` ≤ `SUBAGENT_AUTO_MAX_CEILING` (64), `agent.max_subagents` ≤ 64, `agent.subagent_max_turns` ≤ `SUBAGENT_MAX_TURNS_CEILING` (200), `session.pool_size` ≤ `POOL_SIZE_MAX` (10). Mins match existing runtime floors (0/1); `bool` and non-int values are left untouched for dataclass coercion.
- The ceilings live once in `config.loader` and are imported by the API write-gate (`dashboard/handlers/core.py`) and the runtime pool cap (`session._MAX_POOL`), so the write-gate, runtime cap, and load-time clamp cannot drift.
- A clamp is logged at WARNING and recorded as a `config_bounds_clamped` SEL tamper event (best-effort, never fatal — config loading must not raise). This neutralizes any inflated on-disk value regardless of how it was written.

**URL exfiltration detection** — scans LLM output before posting to Slack/dashboard:
- `scan_exfiltration_urls(text)` — domain-agnostic, flags the payload not the destination
- Detects: long query strings (≥200 chars), base64 blobs (40+ chars), heavy URL-encoding, AWS access key IDs (`AKIA`/`ASIA`), SSH keys, private key headers, Slack tokens
- `redact_exfiltration_urls(text)` — replaces suspicious URLs with `[REDACTED: suspicious URL to {domain}]`

**Credential output redaction** — catches raw credential patterns in LLM/tool output:
- `redact_credentials(text)` — scans for plaintext AND base64-encoded credentials
- Plaintext patterns: `AKIA`/`ASIA` access key IDs, `SecretAccessKey=`, `aws_secret_access_key=`, `SessionToken=`, `aws_session_token=`, private key headers (`BEGIN RSA/DSA/EC/OPENSSH PRIVATE KEY`), Slack tokens (`xoxb-`/`xoxp-`)
- Base64 detection: finds 40+ char base64 chunks, decodes them, checks if decoded content matches any credential pattern
- Applied on ALL 5 output paths: dashboard streaming (mid-flush + trailing), dashboard non-chunk messages, dashboard history save (JSONL), Slack final response
- Defense against write-then-execute attacks: even if the LLM tricks kiro-cli into running a credential-extracting script, the output is scrubbed before the LLM can use it in follow-up messages

### Denied Commands (`security.py` + `agent.py`)

113 regex patterns in `agents/defaults.json` blocking destructive and credential-exfiltrating operations. ada credential patterns are NOT in KiroClaw's denied commands — kiro-cli has its own built-in deny list for `ada credentials` that cannot be overridden via agent config.

**Credential exfiltration blocks**:
- `.*echo.*\$AWS_SECRET.*`, `.*echo.*\$AWS_ACCESS.*`, `.*echo.*\$AWS_SESSION.*` — env var echo
- `.*printenv.*AWS.*`, `.*env.*grep.*AWS.*` — env dump/grep
- `.*python.*boto3.*get_credentials.*`, `.*python.*botocore.*credentials.*` — script-based extraction
- `.*curl.*169\.254\.169\.254.*`, `.*wget.*169\.254\.169\.254.*` — IMDS metadata endpoint
- `.*curl.*\$AWS_SECRET.*`, `.*curl.*\$AWS_ACCESS.*` — credential exfil via curl
- `aws s3 cp .* s3://.*`, `aws s3 mv .* s3://.*`, `aws s3 sync .* s3://.*` — file upload exfiltration
- `.*cat.*/\.aws/.*`, `.*cat.*/\.ssh/.*`, etc. — direct credential file reads

**Allowed operations** (system prompt explicitly permits):
- `ada credentials update` — blocked by kiro-cli's built-in deny list (not KiroClaw). Users must run ada in their own terminal; `credential_process` in `~/.aws/config` handles automatic refresh for AWS CLI commands
- `ada profile add/list/print/delete` — also blocked by kiro-cli
- `aws sts assume-role` — cross-account access
- AWS CLI commands (`describe-*`, `list-*`, `get-*`, `filter-*`, `s3 cp`, `s3 ls`, etc.) — work via `credential_process`

**Destructive operation blocks**: `rm -rf`, `git push --force`, `aws * delete-*`, `aws ec2 terminate-instances`, `cdk destroy`, `terraform destroy`, etc.

- `is_denied(command, auto_deny_tools)` checks against built-in patterns, agent-configured patterns, and a dedicated verb-anchored git-publish detector:
  - **Git publish (verb-anchored regex):** `git push` is detected by `_is_git_publish()` (`_GIT_PUBLISH_RE` + `_GIT_PUBLISH_GLUE_RE`), **not** a substring glob. `push` must be the git *subcommand* (first non-flag token after `git`, allowing intervening `-x` / `-C path` / `-c k=v` options), so a commit message, branch name, grep pattern, or ssh remote payload that merely contains the word "push" is **not** blocked (e.g. `git commit -m '...push...'`, `git log --grep push`, `git switch -c fix/git-push`). Checked on the whole string first to catch command-substitution glue-evasion (`git$(echo ' ')push`, `git\`echo\`push`, `git_push`) and on segment-spanning chains (`git stash push && git push origin main`). Replaces the former broad `*git*push*` glob + ` stash push` exception, which over-blocked benign commands and surfaced as a silent `Tool use aborted` on the Claude Code provider.
  - **Pass 1 (whole-string glob):** every deny glob is matched against the full input. If a pattern matches and no exception pattern also matches the full input, the command is denied immediately. This closes evasion vectors where the deny string spans a shell separator boundary.
  - **Pass 2 (per-segment glob):** only runs if pass 1 found a glob match AND the full input also matched at least one exception. The input is split on shell separators (`;`, `&&`, `||`, `|`, `&`, `$()`, backticks, newlines) into independent segments, and each segment is re-evaluated. `_DENY_EXCEPTIONS` is currently empty (the former git-stash carve-out is obsolete under the verb-anchored detector); the machinery is retained for any future scoped exception.
  - SEL audit events emitted on every denial (`deny_event`, recorded under the `git push` label for git-publish) and every exception grant (`deny_exception`).
> **Removed with the Claude Code provider.** A former check
> (`cc_agent.find_overbroad_cc_deny_rules`, the `seed_isolated_cc_config`
> isolation seed, and the `kiroclaw doctor` surfacing of over-broad CC
> `permissions.deny` rules) guarded against a user's `~/.claude/settings.json`
> `Bash(*)` rule aborting commands upstream of KiroClaw's gate. It was specific
> to the `claude-agent-acp` backend and was **deleted** when KiroClaw became
> KiroACP / `kiro-cli`-only (`agent.provider` fixed to `acp`). kiro-cli's
> permission model routes every tool decision back through KiroClaw's
> `HookManager.on_tool_call` gate, so there is no equivalent upstream-deny gap.

- `_enforce_denied_commands()` replaces denied commands in ALL agent configs from bundled defaults (not union — stale patterns are removed on update)
- Runs at install, gateway startup, and periodically (~60s) with mtime-based skip
- `kiroclaw update` automatically calls `kiroclaw setup --agent-only` as subprocess to refresh agent config
- Targets both `execute_bash` and `shell` tool settings

### Suspicious Bash Patterns (`security.py`)

55 patterns in `SUSPICIOUS_BASH_PATTERNS` checked by `audit_bash_command()` at tool invocation time. Patterns with `*` use `fnmatch` glob matching; others use substring matching.

**Deletion patterns**: `find * -delete`, `find * -exec rm`, `find * -exec shred`, `xargs rm`, `git clean -f`, `shred `, `truncate `, `rm -rf /`, `rm -rf ~`

**Exfiltration patterns**: `curl * -d @`, `curl -d @`, `curl * --data @`, `curl --data @`, `curl * -F file=@`, `curl -F file=@`, `wget --post-file`, `nc * < `

**Pipe execution**: `| bash`, `| sh`, `| python`, `| perl`

### SEL Forward Callback (`sel.py`)

`set_forward_callback()` enables centralized log integration (basin/ktap). Events are redacted via `redact()` before forwarding to strip credentials and exfiltration URLs from string fields. Callback failures are logged at debug level (never silently swallowed).

### Credential File Permissions

`load_credentials()` in `loader.py` enforces `chmod 600` on `~/.kiroclaw/.env` at load time. If permissions are too open (group/other readable), they are tightened automatically. If `chmod` fails (e.g., file owned by another user), a warning is logged.

### Observe Mode Context Isolation

`channel_history.push` in observe-mode channels is gated on `_user_authorized`. Only messages from the owner or allowlisted users are recorded in the history buffer. This prevents non-owner messages from influencing LLM context via prompt injection through shared channel traffic.

### Mermaid Diagram Sandboxing

Mermaid `securityLevel` is set to `'strict'` in `MarkdownRenderer.tsx`, rendering diagrams inside an iframe sandbox. This prevents JavaScript execution from prompt-injected Mermaid diagram payloads.

### MCP Input/Output Validation (`validation.py`)

Centralized validation for all 12 MCP tool handlers (SDO-183):

- **Type-safe schemas**: `FieldSpec` + `ToolSchema` declarative validation
- **Unicode normalization**: NFC normalization + hidden character stripping (control chars, format chars, private use, surrogates — preserves `\n`, `\r`, `\t`)
- **Allow-lists**: enum enforcement for lesson categories, cron schedule kinds
- **Regex patterns**: agent name, job ID format validation
- **Range checks**: positive numbers for timeouts/intervals, valid timestamps
- **Length limits**: tool names (64), short strings (500), medium (5K), long (50K)
- **Unknown field rejection**: rejects unexpected fields in tool inputs
- **Response truncation**: 100K char limit prevents DoS from unbounded tool output
- **JSON-RPC 2.0 envelope validation**: request + response structure

### Dashboard Authentication & Authorization

**Dashboard URL config** — single `dashboard.url` field in `config.json` (e.g. `http://my-host.corp.amazon.com:8080`). Hostname, port, local-only mode, and allowed origins are all derived from this URL. When not set, defaults to `localhost:5476`. `KIROCLAW_PORT` env var overrides the port (dev mode).

**SSH tunnel instructions** — All SSH tunnel commands printed by `kiroclaw gateway` and `kiroclaw doctor` now use the `-N` flag (`ssh -NL ...`) to suppress remote shell allocation. The tunnel purely forwards the port without opening an interactive session on the remote host.

**Local-only resolution** (`origin.py:is_local_only()`):
- No Slack → always local-only (no auth layer available)
- Loopback host in URL (localhost, 127.0.0.1, kiroclaw.localhost) → local-only (`127.0.0.1`)
- Non-loopback host or auto-detect on remote machine → all interfaces (`0.0.0.0`)

**Token authentication** (`token_auth.py`):
- HMAC-SHA256 signed tokens with dual expiry: 5-minute link click window (`exp`) + session TTL up to 20 hours (`session_exp`)
- `!dashboard` and `/kiroclaw dashboard` available to owner and allowed users; link always sent via DM (never in channel)
- First use: validates `exp` (5-min window), binds IP, marks consumed, sets `mc_token_{port}` cookie with `max_age` from `session_exp`
- Subsequent requests: validates `session_exp` via cookie
- `parse_duration()` caps at 20 hours max (MAX_SESSION_TTL_SECS = 72000)
- Loopback access trusted only in local-only mode (SSH tunnel); on all-interfaces mode, all requests require a token
- `token_auth_middleware(local_only)` — single boolean controls all auth behavior

**Response security headers** (`server.py:_apply_security_headers`):
- All dashboard responses receive `Cache-Control: no-store`, `Content-Security-Policy` (default-src 'self' plus curated exceptions for tailwind/jsdelivr/WebSocket loopback), and `Permissions-Policy: clipboard-write=(self), clipboard-read=(self)`
- The Permissions-Policy grant is required by Chrome 143+, which changed the default policy to DENY `clipboard-write` even on secure contexts (crbug.com/414348233). Without it, `navigator.clipboard.writeText` throws a permissions-policy violation and the Copy-link button on published artifacts fails
- When the instances feature is enabled, `frame-src` is extended with `http://127.0.0.1:*`, `http://localhost:*`, and `http://*.localhost:*` so dynamically-connected tunnel ports can be framed
- Applied via `no_cache_middleware` using `setdefault` so per-handler overrides are preserved

**CSRF protection** (`server.py` + `origin.py`):
- Validates `Origin` (with `Referer` fallback) on POST/PUT/DELETE
- Allowed origins computed once via `build_allowed_origins()` at startup: `127.0.0.1:{port}`, `localhost:{port}`, `kiroclaw.localhost:{port}`, plus configured host and machine hostname when not local-only, plus `localhost:3000` in dev mode
- Shared `check_origin()` function used by both CSRF middleware and WebSocket origin check — single source of truth

**WebSocket origin validation** (`ws.py` + `origin.py`):
- `_check_ws_origin()` calls shared `check_origin(require=True)` before `ws.prepare()`
- Reads `app["allowed_origins"]` (same set as CSRF middleware)
- Rejects missing Origin (non-browser clients) and cross-origin requests
- **Same-origin loopback fallback (Mesh-1864)**: when an `Origin` is not in the
  allowed set, it is still accepted if its host is loopback **and** it exactly
  equals the request `Host` header — a genuine same-origin request. This covers
  the multi-instance embedded iframe, which is served at `<host>:<tunnelPort>`
  and opens its WebSocket to that same `location.host` (so `Origin == Host`),
  without reopening SEC-016: an arbitrary-port local page's `Origin` differs
  from the gateway `Host`, and browsers forbid scripts from forging either
  header. Non-loopback `Origin == Host` is **not** auto-trusted (still allowlist-only).

### Slack Owner Authorization

**Deny-by-default owner lock**:
- `_init_socket_mode()` refuses to connect if `KIROCLAW_OWNER_ID` is unset/empty
- `_on_event()` rejects all messages when owner ID is missing (secondary guard)

**Interactive button verification** (5 defense-in-depth layers):
1. Owner check in `_handle_interactive()` — deny-by-default (rejects unless positively confirmed)
2. Owner check in `handle_interaction()` — handler defense-in-depth
3. `conversations.info` DM gate for Trust/YOLO actions
4. Trust/YOLO buttons suppressed in group channels
5. `disable_yolo()` + `yolo off` keyword to reverse YOLO

Non-owners receive ephemeral message: "⛔ Only the KiroClaw owner can use these buttons."

**Safety override (YOLO) — time-limited with re-authorization** (`safety_override.py`):

Permanent YOLO mode has been eliminated. All activations go through the `SafetyOverride` singleton which enforces a hard ceiling of 24 hours. The tiered TTL defaults are:

| Source | Default TTL | Max TTL |
|--------|------------|---------|
| Slack (`!yolo on`) | 30 minutes | 24 hours |
| Dashboard YOLO button | 6 hours | 24 hours |
| Config `approval_mode: "auto"` | 24 hours | 24 hours |

After expiry, re-authorization is required. A 5-minute grace window allows `!yolo renew` (Slack) or the dashboard re-auth button to extend the session without creating a new one. Outside the grace window, a fresh activation is needed.

SEL audit events are emitted on every lifecycle transition:
- `safety_override:activate` — override enabled
- `safety_override:renew` — session extended within grace window
- `safety_override:expired` — TTL reached, auto-deactivated
- `safety_override:deactivate` — manually disabled

Fleet governance endpoints:
- `/api/status` now reports `yolo_active` (bool) and `yolo_expires_at` (ISO 8601) fields
- `/api/admin/compliance/yolo-status` provides full override status (source, remaining time, activation count, renewal history)

Expiry notifications are delivered via Dashboard WebSocket and Slack DM to inform the user before and at override expiration.

**Challenge-and-redirect for Slack direct requests** — **REMOVED**
(`slack/events.py`, `slack/allowlist.py`):

> The redirect flow intercepted every inbound Slack message and turned it into
> a presigned dashboard-session link (deny-by-default), an Amazon-internal-only
> posture. It has been removed for external/open-source usage: Slack messages
> are processed **inline** and reach the agent directly, gated by the user
> allowlist and the Enterprise Grid origin check. `send_channel_challenge()`
> and the `_CHALLENGE_REDIRECT_ENABLED` gate no longer exist; do not restore
> them on an upstream sync (see `skills/meshclaw-sync/SKILL.md`).

**3-tier interactive trust escalation** (`dashboard/chat_runner.py`, `dashboard/chat_handlers.py`):

When the dashboard presents a tool approval prompt, users can now choose from three trust levels:

| Action | Scope | What it trusts |
|--------|-------|---------------|
| `trust_command` | Session-scoped | Exact command/tool (e.g., `ls /tmp`) |
| `trust_base` | Session-scoped | Base command glob (e.g., `ls *` — trusts `ls` with any arguments) |
| `yolo` | Global | All tools across all slots (existing behavior, now time-limited) |

Trust patterns are stored per-slot as session-scoped fnmatch globs (`slot._trusted_patterns`). Pattern matching uses the ACTUAL command from `tool_input` (not the LLM-controlled display text) for security. For non-shell MCP tools without `tool_input`, `event.title` is used as it IS the provider-controlled tool name. Multi-command titles (e.g., `cat,wc`) generate patterns for each binary.

### SEL Audit Logging (`sel.py`)

See `docs/system-specs/modules/sel.md` for full spec. Integrated across 8 surfaces: Slack handler, dashboard chat, task runner, subagent, background tasks, MCP core, MCP cron, API middleware.

**What counts as an auditable permission decision.** A SEL event is emitted when a decision has a *subject* — a tool/capability that was granted or denied. The audit records grants and denies, not the absence of any decision:

- **Skill triggering** (`skills.py:get_triggered_skills`, runs per message) emits **one** event per call when at least one skill was injected (`outcome="triggered"`, grant) or actively excluded by a negative trigger that would otherwise have matched (`outcome="denied"`, with the excluded skills in `metadata.negated`). When no skill matched and none was negated — the overwhelmingly common case — **no event is emitted**: nothing was granted or injected into LLM context, so there is no permission decision with a subject to record (analogous to not auditing an authz check that had nothing to authorize). This is a deliberate, threat-model-reviewed choice: the prior per-skill "not_triggered" logging was a per-message synchronous-write hot-path cost, and a per-message "matched nothing" event would dwarf the real grant/deny signals and *reduce* the audit trail's usefulness rather than improve it. The message text is already captured in conversation history; skill names are not secret.

### Frontend Security

- **No `dangerouslySetInnerHTML` with unsanitized content** — all HTML content sanitized via DOMPurify
- **Safe DOM APIs** — `createElement` + `textContent` for error fallbacks (not `innerHTML`)
- **Ref callbacks** for highlight.js output (DOMPurify-sanitized)
- **React text children** instead of `esc()` + `sanitize()` HTML strings
- **No regex URL linkification in HTML strings** — use React elements via `.split()`
- **Shell injection prevention** — `/etc/hosts` update uses `sudo tee -a` (not `sh -c echo`)

## Security Rules for Development

When writing new code, these rules MUST be followed:

### Backend
1. **Never read sensitive paths** — all file reads must go through `hooks.py` which enforces `is_sensitive_path()` and `is_sensitive_bash_command()`
2. **Never trust LLM output** — scan with `redact_exfiltration_urls()` before posting to any external surface (Slack, dashboard, API responses)
3. **Validate all MCP tool inputs** — use `validation.py` schemas; never pass raw LLM input to filesystem, subprocess, or database operations
4. **Deny-by-default for authorization** — reject unless positively confirmed. Never use `if x and y and z` guards where any falsy value skips the check
5. **Sandbox all agent subprocesses** — new subprocess spawning must go through `AcpClient._spawn()` which applies OS-level sandbox
6. **Enforce denied commands** — new CLI-facing tools must be covered by `deniedCommands` patterns
7. **Log security events** — all tool invocations and permission *decisions* (a capability granted or denied) must emit SEL events. The absence of a decision — e.g. skill-trigger matching that injected and excluded nothing — is not itself an auditable event (see "What counts as an auditable permission decision" above)

### Frontend
1. **Never use `dangerouslySetInnerHTML`** without DOMPurify sanitization
2. **Never use `innerHTML`** — use `textContent`, `createElement`, or React elements
3. **Never construct HTML strings with user/LLM content** — use React components
4. **Sanitize all external content** — use `md()`, `sanitize()`, or `esc()` from `helpers.ts`
5. **No inline event handlers in HTML strings** — use React event props

### Binary File Handling (`security.py`, `handlers/files.py`, `mcp_core.py`)

The `file_send` MCP tool and outbox handlers support binary media files with a deny-by-default MIME allowlist.

#### BINARY_MIME_ALLOWLIST

Module-level constant in `security.py`. Only these MIME types are accepted for binary (non-UTF-8) files:

| Category | Types |
|----------|-------|
| Audio | `audio/mpeg`, `audio/wav`, `audio/x-wav`, `audio/ogg`, `audio/flac`, `audio/aac`, `audio/mp4`, `audio/webm`, `audio/opus` |
| Video | `video/mp4`, `video/webm`, `video/ogg` |
| Image | `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `image/bmp` |
| Document | `application/pdf` |

**Excluded:** `image/svg+xml` (XSS vector — SVG can contain `<script>` tags).

#### Security Model

| File type | Content scan | MIME check | Disposition |
|-----------|-------------|------------|-------------|
| Text (UTF-8 decodable) | `redact()` for credentials/exfiltration | N/A | `attachment` |
| Binary (in allowlist) | Skipped (can't redact binary) | Must be in `BINARY_MIME_ALLOWLIST` | `inline` (browser renders natively) |
| Binary (not in allowlist) | N/A | Rejected with 400/403 | N/A |
| SVG (UTF-8 decodable) | `redact()` for credentials/exfiltration | Not in allowlist (text path) | `attachment` (never inline — defense-in-depth against XSS) |

#### Response Headers

All outbox downloads include:
- `Content-Type`: from `mimetypes.guess_type()` or `application/octet-stream`
- `Content-Disposition`: `inline` for media, `attachment` for others
- `X-Content-Type-Options: nosniff`: prevents MIME sniffing attacks

#### Invariants

- Path traversal protection unchanged (resolved path must be under `outbox_dir()`)
- Filename sensitivity check unchanged (`redact(filename) == filename`)
- Text content redaction unchanged for UTF-8 files
- Binary files: filename validated, content scan skipped (binary data cannot be meaningfully redacted)
