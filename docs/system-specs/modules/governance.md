# Governance Model (two-level Policy ∩ Profile)

The `kiro_claw.platform.governance` + `kiro_claw.platform.governance_profiles`
modules implement KiroClaw's **two-level security governance model**. Governance
is resolved by a single rule — *the tightest boundary wins*:

- **Level 1 — POLICY** (`GovernanceCeiling`): the enterprise security ceiling,
  loaded once at boot from a trust-root path the agent process does not own.
  Once present, the running app **and its agent cannot weaken it**.
- **Level 2 — PROFILE** (`Profile`): a per-surface / per-app / per-task scope
  that may only *narrow* what policy permits.

The effective permission for any item is `policy ∩ profile`. This spec is the
implementation companion to the design doc (Pippin `kiroclaw/MVTDhLpm2SSW`).

> Scope: this governs **KiroClaw's own** security boundaries — what the host
> performs on behalf of the agent across every surface (CLI, dashboard, Slack,
> cron, heartbeat, sub-agents, apps). The underlying kiro-cli agent config
> (`~/.kiro/agents/*.json`) is **out of scope**: KiroClaw enforces its own
> ceiling at its own gate even when the kiro side grants more.

## The four archetypes (one composition algebra each)

Every governed control is exactly one of four shapes. The evaluator dispatches
on archetype, never on a scope *name* — this is what keeps the model decoupled
and extensible (adding a scope is data, not engine code).

| Archetype | Shape | Composition (policy ∘ profile) |
|---|---|---|
| `ScopedRuleset` | `{mode, allow[], deny[]}` | Rule 1 within a level (allow beats deny); Rule 2 across (allow = ∩, deny = ∪) |
| `OrdinalControl` | a single enum value | strictest-of, on an **enforcer-owned** scale |
| `CapabilityGate` | `{enabled, scopes{…ruleset}}` | `enabled` = AND; each scope is a ScopedRuleset |
| `ScopedMap` | `{members: ruleset, posture{…}}` | members = ScopedRuleset; `posture` is policy-only |

**Enforcer-owned registries** (never sourced from a governed file, so no profile
can reorder strictness or redefine matching):

- `_ORDINAL_SCALES`: `approval = yolo < auto < interactive`;
  `sandbox = off < standard < cc < strict` (verified against `sandbox.py`).
- `_MATCHERS`: `identifier` (case-insensitive), `command` (case-sensitive
  `fnmatchcase`), `path`, `host`, `mcp` (a `@server` grant covers `@server/tool`).

`SCOPE_CATALOG` is the single place a scope name binds to its archetype +
matcher. `register_scope` / `register_matcher` are append-only extension seams;
the test suite proves a synthetic scope resolves end-to-end with **zero**
evaluator edits.

## Loading + precedence

`load_security_policy()` precedence (first present wins):

1. `KIROCLAW_SECURITY_POLICY` env path — fleet hot-override, highest.
2. companion-bundled resource (the `amazon` edition packages it; the public core
   passes `None`).
3. `~/.kiroclaw/security_policy.json` — standalone operator-authored.
4. none → `None` → editable secure-defaults (ungoverned ceiling).

A **present-but-unreadable / invalid** policy raises `PlatformCompositionError`
(fail-closed to strictest), mirroring `admission.load_admission_policy`. Parsing
is **pure-Python and structural** (it does not depend on `jsonschema`, which is
an optional, possibly-absent dependency) so a malformed policy never silently
degrades to ungoverned.

## Boot composition

`build_default_context` (the single chokepoint backing both a real boot and the
lazy `current_context` default) calls `load_security_policy()` and stores the
result in the frozen `PlatformContext.governance` field. This bumps
`CONTRACT_VERSION` to **2** (a v1 companion must rebuild). Every enforcement
chokepoint reads `current_context().governance`.

## Self-protection (the keystone)

Under *"secure by default, not by mandate"* there is **no compiled-in floor** —
the entire posture is operator-editable. The only invariant is the
**agent-vs-operator split**: the agent cannot edit the policy/profile files.
This is enforced solely by adding them to `security._SENSITIVE_HOME_DIRS`
(`~/.kiroclaw/security_policy.json`, `~/.kiroclaw/profiles`,
`~/.kiroclaw/admission_policy.json`) — `is_sensitive_path` is the shared
read+write gate across every surface. `assert_governance_paths_protected()` is a
boot integrity check that fails closed if a refactor ever drops them.

## Profile resolution + binding

A profile binds to a `surface` (cron/slack/dashboard/subagent/…), an `app` slug,
or a `task` id. `resolve_active_scope(session_key, agent, app)` resolves the
active profile, classifying the session key via `sel._infer_source` (the single
canonical taxonomy parser — never re-implemented). Resolution is:

- **app bind → task/agent bind → surface bind** (most specific first).
- No bound profile on an **attended/proven** surface → `None` (policy alone).
- No bound profile on an **unattended + unproven** surface → `deny_all_profile`
  (fail-closed, never a permissive fall-through), mirroring the dashboard
  `api_session_tool_policy` precedent.

Profiles hot-reload via an mtime fingerprint (`ProfileStore`); a schema-invalid
profile falls back to deny-all (Validation rule 5), **not** the ceiling.
`extends` is monotonic narrowing (`compose_profiles`).

## Enforcement planes

- **Plane A — the host gate** (`HookManager.on_tool_call`, the primary
  chokepoint). After the always-on sensitive-path + deny-floor checks, it
  evaluates `gate_decision(ceiling, profile, title)` — **including MCP titles**
  (`mcp__server__tool`, converted to `@server/tool`) — *before* the auto-approve
  loop, so a governance deny wins over a user auto-approve and denies a tool the
  kiro agent config granted. The call sites thread `session_key`/`agent` (they
  default to `""`, so non-governed callers are unaffected).
- **Plane B — kiro agent JSON**: out of scope (v1). The gate is authoritative;
  KiroClaw does not regenerate `~/.kiro/agents/*.json`.
- **Plane C — out-of-band executors**: the cron `command` (runs via `sh -c`
  outside the ACP flow) is gated in `mcp_cron._vet_command_governance`; the
  sandbox ordinal floor is clamped in `sandbox.wrap_argv`; spawn in
  `subagent._vet_spawn_governance`; outbound messaging in
  `mcp_core._vet_messaging_governance`. All route through the same
  `governance_permits` / `governance_floor_ordinal` decision source.

### Modeled-but-not-yet-enforced scopes (v1)

Several scopes are fully modeled — they parse, validate, compose, and resolve,
and `kiroclaw policy show`/`explain` display them — but are **not yet wired to a
runtime chokepoint**. They are reserved so the policy/profile vocabulary is
stable; a profile/policy authoring them is honoured by the evaluator but does
not change runtime behavior until the corresponding gate lands:

- **`network.egress`** — no governed HTTP/socket client exists yet (the locked
  v1 decision); a later change adds the single governed client.
- **`filesystem.read` / `filesystem.write` / `folders.*`** — file reads/writes
  are gated today only by the fixed `is_sensitive_path` keystone list, not by a
  per-policy path ruleset. (Writes to the trust-root + credential dirs ARE
  blocked; arbitrary per-policy path allow/deny is not yet enforced.)
- **`channels` (members + posture)** — outbound messaging is gated by the
  `capabilities.messaging` on/off gate; the per-transport `channels` map is not
  yet consulted at a transport chokepoint.
- **`approval_mode`** — the ordinal is parsed and floor-checked, but no approval
  chokepoint clamps the live approval pipeline through it yet (the live approval
  vocabulary — `reads`/`yolo`/`auto`/`interactive` — is not yet reconciled onto
  the `approval` scale).

The **enforced** scopes in v1 are: `tools`, `mcp`, `commands` (the host gate +
cron command), `capabilities.spawn`, `capabilities.messaging`, and
`sandbox.min_level` (the ordinal floor at `wrap_argv`). Adding a chokepoint for a
reserved scope is the documented follow-up; the evaluator already supports them.

## Audit

`sel.log_governance_decision` records a `governance_decision` event
(`outcome ∈ {allowed, denied}` — the existing permit vocabulary). On-disk SEL is
not redacted by the writer and the HMAC chain signs the bytes as written, so the
operation / item / reason are redacted via `redact_via_context` **before** `log`.

## CLI

`kiroclaw policy {show | validate | explain <scope> <item> | profile <name>}` —
read-only operator diagnostics. `explain` traces the rule/layer/reason and the
live gate verdict. Deliberately **not** exposed as an MCP tool: it surfaces
governance internals that the agent (the governed subject) should not enumerate.

## Companion (separate package, separate CR)

The `amazon` companion contributes the restrictive posture as its
**bundled `security_policy.json`** (precedence step 2) rather than as code;
capability providers (Midway/SigV4/tunnels) and the SharePoint redaction
carve-out stay as code. It expects `CONTRACT_VERSION == 2`.

## Files

- `platform/governance.py` — archetypes, catalog, loader, evaluator
  (`resolve`, `resolve_ordinal`, `gate_decision`, `assert_governance_floor`,
  `compose_profiles`).
- `platform/governance_profiles.py` — `ProfileStore` (hot-reload),
  `resolve_active_scope`, `governance_permits`, `governance_floor_ordinal`.
- `security.py` — `_SENSITIVE_HOME_DIRS` keystone entries.
- `hooks.py` — Plane A gate threading.
- `sel.py` — `log_governance_decision`.
- chokepoints: `sandbox.py`, `mcp_cron.py`, `subagent.py`, `mcp_core.py`.
- `cli.py` / `cli_commands.py` — the `policy` command.

## Tests

`test_governance_policy.py` (archetypes + loader + evaluator + E1–E13 vectors +
extensibility), `test_governance_boot.py` (compose at boot), 
`test_governance_self_protection.py` (keystone), `test_governance_profiles.py`
(resolution + binding + hot-reload), `test_governance_gate.py` (Plane A
enforcement + audit), `test_governance_chokepoints.py` (sandbox/cron/spawn/
helpers + egress-reserved).
