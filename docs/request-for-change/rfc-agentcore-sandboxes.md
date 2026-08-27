---
title: AgentCore sandboxes — a Coder-shaped session host for Crew
status: draft
author: kyle
created: 2026-08-27
last-audited: 2026-08-27
audited-at: 84d546170
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: AgentCore sandboxes — a Coder-shaped session host for Crew

## TL;DR

The Coder work on `feat/coder-aws-dogfood-poc` is not a packaging template.
It is a **session execution host**: the gateway stays the control plane, and
each durable parent session tree owns one isolated Linux workspace where
`kiro-cli` ACP actually runs. MCP, memory, policy, hooks, and credentials
stay on the gateway and ride back over `coder ssh` reverse forwards.

AgentCore sandboxes are the AWS-managed analog of those workspaces. They are
**not** a third `sandbox.py` backend and **not** an Instances
`connection_method`. The similar fork is a second `SessionHost` kind
(`kind: "agentcore"`) that reuses the Coder lifecycle, fail-closed
placement, and capability-relay contracts.

The hard gap is transport. Coder gives inbound SSH + reverse forwards.
AgentCore gives invoke APIs, one-shot root commands, optional `openShell`,
and **no inbound TCP**. Coder-parity therefore requires a custom
`linux/arm64` Runtime/Harness image whose supervisor **dials out** to the
gateway and multiplexes ACP + relays on that socket. Code Interpreter as an
MCP tool is a useful lesser plane; it is not the Coder analog.

Phase 0 extracts the host seam from the Coder fork and writes a transport
verdict. Product code for AgentCore does not start until that verdict is
in this file.

## 1. What the Coder fork actually is

Measured on `feat/coder-aws-dogfood-poc` (`d197657fd`), not on this
branch. Main at `84d546170` has none of this.

### Topology

From that branch's
`docs/superpowers/specs/2026-08-25-coder-per-session-workspace-lifecycle-design.md`
(quoted here so this RFC stands alone):

```text
Coder control plane (small, always on; no Crew state)
  |
  +-- gateway workspace (small, always on)
        |
        +-- session A binding --> crew-session-user-…
        |                           parent ACP + all A subagents + A disk
        +-- session B binding --> stopped, disk retained
```

Invariants that any AgentCore fork must keep:

| Invariant | Coder implementation |
|---|---|
| One control plane | Gateway owns history, memory, MCP, OAuth, cron, policy, bindings |
| One workspace per parent tree | Subagents inherit; forks and distinct cron runs get a new binding |
| Fail closed | Lifecycle failure never falls back to local ACP or a sibling workspace |
| No secrets in the workspace | No Crew data home, AWS keys, channel tokens, Coder bearer, MCP creds |
| Gateway-owned MCP/hooks | Credential-free relay + reverse forward; backends stay local |
| Lazy start / idle stop / retain / delete | Autostop ~30 min; delete stopped workspace after ~30 days |
| Local path preserved | `session.coder.enabled=false` is today's process placement |
| Positive Kiro identity | Remote host is Kiro-only (`ACP_BACKEND_KIRO`); no negative harness test |

### Code seam (the thing to copy)

`src/kiro_crew/acp/session_host.py` already splits hosts:

- `LocalSessionHost` — current main behavior
- `CoderWorkspaceSessionHost` — static workspace (early POC)
- `ManagedCoderWorkspaceSessionHost` — per-parent binding via
  `coder.manager` + `coder.registry` + `coder.client`

`execution_location` is `{kind, workspace, remote_cwd, …}` with
`kind: "coder"`. The factory fails closed without a token. Dashboard
surfaces an execution-location badge and a Settings Coder panel.
Bindings live in `coder_workspaces.json` on the keystone deny list.

Transport is `coder ssh`: ACP stdio plus reverse forwards from workspace
loopback to gateway loopback. A contract marker
`/etc/kirocrew-coder-contract.json` (version, user, `remote_cwd`,
`kiro-cli`, `systemd-user-scopes`) is verified before `session/new`.

### Deploy shape

`deploy/coder-aws/` is a single-user Graviton dogfood: Tailscale-only
ingress, always-on Coder + gateway workspaces, per-session
`c8g.large` (selectable presets). Session instances have **no inbound
SG rules**; Coder's agent connection is outbound. That outbound-agent
fact is the one Coder property AgentCore already shares.

## 2. What AgentCore sandboxes are

Three AWS products get called "sandboxes." Only one can host ACP.

| Product | What it is | Can it host `kiro-cli acp`? |
|---|---|---|
| **Code Interpreter** | `Start/Invoke/StopCodeInterpreterSession`. Python/JS/TS + `executeCommand` / files. TTL 15 min default, 8 h max. Optional S3/EFS mounts. Modes `SANDBOX` / `PUBLIC` / `VPC`. | No. There is no long-lived child and no bidirectional stdio. |
| **Runtime / Harness** | Custom `linux/arm64` microVM. `InvokeAgentRuntime` (agent loop) + `InvokeAgentRuntimeCommand` (one-shot root shell, 1–3600 s, **stateless between commands**). Session storage / EFS / S3 Files under `/mnt`. `openShell` PTY in the TS SDK. Idle and max-lifetime caps. **No inbound listener.** | Yes, if a supervisor in the image keeps `kiro-cli` alive and opens an **outbound** tunnel. Not via the command API alone. |
| **Browser** | Managed browser VM. | No. Out of scope (Crew already has computer-use). |

Other facts that constrain the design:

- Custom images must be `linux/arm64` (Graviton, same as the Coder POC).
- Harness overrides `ENTRYPOINT`/`CMD`; a background process is started
  with `InvokeAgentRuntimeCommand` after the session begins, or the
  image must cooperate with that override.
- `InvokeAgentRuntimeCommand` runs as **root in the VM**, even if the
  container `USER` is non-root. Treat the VM as hostile to secrets.
- Code Interpreter `SANDBOX` mode still allows DNS (AWS documented this
  after disclosure). Never advertise it as airgapped. Prefer `VPC`.
- Session storage persists across stop/resume for the same
  `runtimeSessionId` without a VPC. EFS / S3 Files need VPC.

## 3. Mapping Coder primitives → AgentCore

| Coder | AgentCore stand-in | Gap |
|---|---|---|
| Coder API + token | `bedrock-agentcore` + `bedrock-agentcore-control` IAM | Different auth; already have boto3 for SSM Instances |
| Template `kirocrew-arm` + `/etc/kirocrew-coder-contract.json` | Custom Harness image + the same contract file | Must bake `kiro-cli`, Python, systemd-user (or drop that capability) |
| `coder ssh` (inbound to workspace) | **Outbound** supervisor → gateway WebSocket/SSH | Must invent. This is P0. |
| Reverse forwards for MCP/hooks | Multiplexed streams on the outbound socket | Same relay bundle; different pipe |
| Persistent root disk | Session storage and/or EFS | Resume-after-stop works; 30-day retain needs EFS or we accept recreate |
| Autostop 30 min | Runtime idle timeout + gateway lease (Coder's managed-workload lease already exists as a concept) | Gateway must keep the session leased during long turns |
| `max_running` | Same semaphore in the manager | Direct copy |
| Settings panel + execution badge | `session.agentcore.*` + `kind: "agentcore"` | i18n-gated UI; clone Coder panel structure |
| Fail closed | Same | Never fall back to local ACP |

Code Interpreter does not appear in this table. It cannot replace a row.

## 4. Goals

- G1. A second `SessionHost` kind that an operator can enable the way they
  enable Coder: per-parent-session isolated compute, gateway control plane
  unchanged.
- G2. Fail closed. No local ACP fallback. No sharing a sandbox across
  unrelated parents. Subagents inherit the parent host.
- G3. No secrets in the sandbox. Same deny list as Coder: Crew home,
  `~/.aws`, `~/.ssh`, channel tokens, MCP creds, keystone files.
- G4. Extract the Coder host interface so adding AgentCore is a new
  adapter, not a fork of `session_host.py`.
- G5. No new `agent.provider`, no new ACP backend, no negative
  harness-identity test. Remote host stays Kiro-only.
- G6. Default off. Local placement on main is byte-identical.

## 5. Non-goals

- Replacing `sandbox.wrap_argv` with AgentCore. That confines a *local*
  child. This RFC remotes the child.
- Instances `connection_method = "agentcore"`. Instances iframes a
  loopback dashboard (`instances.md` §1). AgentCore has no inbound port.
- Hosting the Crew gateway inside AgentCore. The Coder fork already
  rejected co-locating the control plane with session compute.
- Computer-use, channel bots, or cron *scheduling* moving into the
  sandbox. Cron *sessions* may be AgentCore-hosted, as they are
  Coder-hosted; the scheduler stays on the gateway.
- Shipping the Coder fork to main as a prerequisite product. P0 may
  extract its host ABC from that branch; landing Coder itself is a
  separate review.
- Re-adding other LLM providers or a hardcoded model id.

## 6. Approaches

### A. Host-kind parity (recommended)

Copy the Coder architecture. Add `kind: "agentcore"`.

```text
Gateway
  - cron, memory, history, MCP, policy, hooks, bindings
  - AgentCoreSessionHost + binding registry
                  |
                  | outbound tunnel (sandbox dials gateway)
                  |   stream 0: ACP stdio
                  |   stream 1..N: MCP / hook relays
                  v
AgentCore Runtime / Harness microVM
  - supervisor (image ENTRYPOINT is overridden; start via
    InvokeAgentRuntimeCommand or a cooperating keep-alive)
  - kiro-cli ACP
  - built-in file / shell / code tools
  - contract marker
  - session storage or EFS at remote_cwd
```

**Why this is the similar fork.** Every Coder invariant in §1 has a
named owner. The only invention is the transport that replaces
`coder ssh`.

**Transport (the P0 question).** Ranked:

1. **Outbound supervisor (preferred).** Image contains a tiny stdlib-only
   supervisor. After the session starts, it dials a gateway-minted,
   single-use, loopback-or-tailnet WebSocket (same capability-bearer
   discipline as Coder's relay). Multiplex ACP + relays. Workspace never
   receives a dashboard URL derived from `Host` headers. This matches
   the Coder AWS POC's "no inbound SG rules, agent connects out."
2. **`openShell` PTY.** Frame ACP over a managed PTY. Fragile (the same
   reason Coder AgentAPI parses a TUI). No clean reverse-forward analog.
   Acceptable as a probe, not as the product transport.
3. **InvokeAgentRuntimeCommand as bash.** Cannot host ACP. Rejected for
   this approach.

If P0 finds no supported way to keep a long-lived outbound supervisor
under the Harness ENTRYPOINT override, Approach A stops and this RFC
is updated. We do not pretend Code Interpreter is a host.

### B. Code Interpreter MCP plane (lesser, independently useful)

Gateway-local `kiro-cli`. A managed MCP server exposes `sandbox_exec` /
`sandbox_files` against `Start/Invoke/StopCodeInterpreterSession`.
One Crew session → one interpreter session. Useful for untrusted
eval / data jobs when the operator does **not** want to remote the
whole ACP runtime.

This is additive and cheaper to ship, but it is **not** the Coder
analog: local `bash` / `fs_read` still see the gateway host. Do it
as a follow-on or a parallel small PR if Approach A's transport is
blocked. Do not advertise it as "AgentCore session hosting."

### C. Third `detect_backend()` in `sandbox.py` (rejected)

`wrap_argv` returns a local argv for `Popen`. AgentCore is a remote VM.
Nested-sandbox markers, Seatbelt, and cgroup scopes are host-kernel
contracts. A `backend == "agentcore"` branch would silently change
Windows fail-closed into "call AWS." Category error.

## 7. Design (Approach A)

### 7.1 Extract a host interface first

P0 lands an ABC (name TBD: `SessionHost`) with the methods
`CoderWorkspaceSessionHost` already has in the fork:

- `execution_location() -> dict`
- `clone()` for descendant runtimes
- `prepare()` / contract check
- `spawn_acp(...)` returning a stdio transport
- `project_agent(...)` / capability mint for MCP + hooks

`LocalSessionHost` implements it on main with no behavior change.
The Coder classes move behind it on the fork. AgentCore implements it
later. This is the only way "a similar fork" does not mean
copy-pasting 1,100 lines of `session_host.py`.

Do **not** add a `PlatformContext` adapter until a companion edition
needs a private interpreter. `CONTRACT_VERSION` stays 1.

### 7.2 Bindings and lifecycle

Copy `WorkspaceBinding` / `WorkspaceBindingRegistry` /
`CoderWorkspaceManager` into a host-agnostic binding store, or add
`agentcore/` as a parallel package with the same shape:

- opaque binding id (no title, prompt, channel, email, or raw session
  key in the AWS session name)
- AgentCore `runtimeSessionId` / harness ARN / interpreter id
- template/image digest recorded at creation (settings edits must not
  redirect a live binding)
- last-activity, stop, delete-after
- `max_running` semaphore
- keystone-covered `agentcore_sessions.json` + lock
- IAM via the default AWS chain; no access keys in `config.json`

Lease holders stay gateway-owned (in-flight turn, subagent runtime,
prepare/teardown), matching Coder's managed-workload lease so idle
stop cannot kill a 40-minute test.

Fail closed on capacity, IAM, prepare, or tunnel loss. Session recovery
restarts one coherent runtime, as Coder does when SSH drops.

### 7.3 Configuration

```json
{
  "session": {
    "agentcore": {
      "enabled": false,
      "region": "",
      "harness_arn": "",
      "image_uri": "",
      "remote_cwd": "/mnt/workspace",
      "network_mode": "vpc",
      "runtime_warm_minutes": 5,
      "stop_after_minutes": 30,
      "delete_after_days": 30,
      "max_running": 3
    }
  }
}
```

`enabled` is **not** on the agent-writable config allowlist (same as
Coder). Off means the factory returns `LocalSessionHost`.

Prefer bring-your-own harness ARN over Crew calling `CreateHarness`.
Crew is not CloudFormation. The image is an operator artifact; this
repo may ship a reference Dockerfile under `deploy/agentcore/` the way
the Coder fork ships `deploy/coder-aws/workspace/`.

### 7.4 Image contract

Same idea as `/etc/kirocrew-coder-contract.json`:

```json
{
  "version": 1,
  "user": "kiro",
  "remote_cwd": "/mnt/workspace",
  "capabilities": ["kiro-cli", "outbound-tunnel"]
}
```

`systemd-user-scopes` is optional; if the Runtime image cannot offer
it, the conservative background-work guarantee is documented as
degraded and the prepare check does not require it.

Prepare still verifies `kiro-cli` on PATH and directory ownership
before ACP starts. Missing or mismatched contract fails the session
closed.

### 7.5 Governance and audit

Add a `SCOPE_CATALOG` row (`agentcore.host` or similar),
`off < exec`. Tightest-wins. Data change only.

Every allocate / start / invoke / stop / deny is a SEL tool-invocation
event. Redact payloads. Do not log source or file blobs.

Local `BUILTIN_DENIED_RULES` still apply to the *gateway* hook path.
Commands the remote `kiro-cli` runs inside the VM are confined by the
microVM + VPC + execution role, plus whatever the projected agent is
allowed. Do not claim the local OS sandbox confines them — it does
not; they are not local children.

### 7.6 UI

Clone the Coder panel structure: enable, region, harness, remote cwd,
retention, connection test (IAM + `GetHarness`, **no** billable
session), separate explicit smoke (create + delete). Execution-location
badge grows a `kind: "agentcore"` variant. All strings go through i18n.

P1 may ship CLI/`doctor` only if the UI is the long pole; the host
must work headlessly first.

## 8. Migration plan

Each phase is independently shippable and abandonable.

### P0 — Seam extract + transport probe (blocks everything)

1. From the Coder fork, extract a `SessionHost` ABC and land
   `LocalSessionHost` on main with tests that pin today's spawn path.
   No AgentCore code.
2. Against a real account (or a recorded fixture if AWS cannot be
   reached from CI), answer:
   - Can a custom Harness image start a supervisor after the platform
     overrides `ENTRYPOINT`?
   - Can that supervisor hold a WebSocket to a gateway loopback/tailnet
     listener for > idle timeout if we renew a lease?
   - Can we multiplex ACP stdio + one MCP relay on that socket?
   - What is p50/p95 cold start?
   - Does session storage survive stop/resume for `runtimeSessionId`?
   - Is `openShell` usable as a fallback probe?

**Exit:** this RFC gains a "P0 verdict" section. If the outbound
supervisor cannot be kept alive, Approach A is no-go and Approach B
is the only remaining product. A no-go is a successful phase.

### P1 — Binding manager + fail-closed factory

`session.agentcore` config, registry, manager, factory branch that
returns `AgentCoreSessionHost` or raises. No ACP spawn yet if the
tunnel is not ready; in that case P1 is config + tests only.

**Exit:** `enabled=false` is a no-op. `enabled=true` without IAM fails
closed with a machine-readable `code`. Bindings never contain secrets.
Registry file is on the sensitive-path deny list.

### P2 — Outbound tunnel + ACP spawn

Supervisor image, contract marker, `spawn_acp`, execution-location
badge, doctor smoke. Parent session only. No MCP relay yet (MCP-free
agent, same as the Coder POC's first milestone).

**Exit:** a unit test with a fake tunnel runs `session/new` remotely.
A manual doctor smoke against a throwaway harness starts and stops one
session. Failure does not start a local `kiro-cli`.

### P3 — Relays, subagent affinity, lifecycle

Project MCP/hooks through the multiplexed tunnel using the Coder
capability model (digest on gateway, bearer in an owner-only remote
file, no argv secrets). Descendants clone the parent host. Autostop /
retain / delete. `max_running`.

**Exit:** `learn_add` from a remote session writes gateway memory. A
dedicated subagent starts in the same sandbox. Idle stop does not
kill an in-flight turn. A sibling session cannot join the VM.

### P4 — Approach B (optional)

Code Interpreter MCP tools, only if someone still wants disposable
eval without remoting ACP. Separate enable flag.

## 9. Security considerations

- **Local ceiling unchanged.** Keystone files stay in
  `_SENSITIVE_HOME_DIRS`. The sandbox must never mount `~/.kiro/crew`,
  `~/.aws`, `~/.ssh`, or `~/.kirocrew`.
- **Outbound-only.** The gateway listener is loopback or a trusted
  tailnet origin, never derived from inbound `Host` /
  `X-Forwarded-*` (same rule as Coder's OAuth callback).
- **Capability bearers** stay out of argv, env, ACP frames, logs, and
  SEL resource strings.
- **Root in the VM.** Do not put secrets on the image or in session
  storage. Execution role is operator-owned IAM with least privilege
  (`InvokeAgentRuntime`, `InvokeAgentRuntimeCommand`, session get/stop;
  no `iam:PassRole` on the Crew principal if we are BYO-harness).
- **SANDBOX network mode is not a security claim.** Prefer VPC.
- **Enable is config, not keystone.** AgentCore is *more* isolation and
  is billed. Keep the key off the agent-writable allowlist so the
  model cannot flip it.
- **Shared responsibility.** AWS isolates microVMs. Crew is responsible
  for what it sends, which principal may call the APIs, and the
  outbound tunnel's authentication.

## 10. Alternatives considered

**A1. Wait and land the Coder fork on main first.** Right for the ABC
extract (P0.1). Wrong as a product gate: AgentCore should not require
operators to run Coder.

**A2. Instances iframe.** Rejected. No inbound dashboard.

**A3. Intercept local `bash` and send it to Code Interpreter.** Looks
like remoting and breaks `wrap_argv`, MCP stdio, and computer-use.
Rejected.

**A4. Generic remote-sandbox ABC (E2B, Daytona, Modal).** Write
`SessionHost` so a fourth kind could exist. Do not abstract three
backends before the second one works.

**A5. Kiro cloud sessions.** Orthogonal. Crew cannot drive those as a
library.

## 11. Backward compatibility

Default `session.agentcore.enabled=false`. No agent-spec change. No
new import on the ACP spawn path when disabled. Confirm at P1 whether
the existing boto3 pin speaks `bedrock-agentcore`; add the extra only
if needed and unused when disabled.

## 12. Open questions

| # | Question | Blocks |
|---|---|---|
| O1 | Can a Harness image run a long-lived outbound supervisor under the platform `ENTRYPOINT` override? | P0 verdict; Approach A go/no-go |
| O2 | Session storage vs EFS for the 30-day retain story? Storage is cheaper and VPC-free; EFS matches Coder disk. | P3 |
| O3 | BYO harness ARN vs Crew `CreateHarness`? This RFC picks BYO. | P1 config shape |
| O4 | UI in P2 or doctor-only until P3? Prefer doctor first (i18n cost). | P2 scope |
| O5 | Should the Coder ABC extract land on main before or with this RFC's P0? Prefer before, as its own PR, so AgentCore is not coupled to Coder review. | P0.1 |
| O6 | Billing UX. CloudWatch only for P1–P2. | P2 |

## 13. Why this is the similar fork

Coder's fork answers: *the agent process lives in an isolated workspace
I already operate; Crew remains the brain.* AgentCore answers the same
question with an AWS-managed microVM, **if** we can replace `coder ssh`
with an outbound tunnel.

A Code Interpreter MCP tool answers a different question: *give the
local agent a disposable calculator.* That is worth doing. It is not
what the Coder fork built.

P0 exists because one sentence above may be false (Harness cannot host
a supervisor). The verdict goes in this file. The product host does
not start until that sentence is true.
