---
title: AgentCore Identity and Gateway — Crew agent identity and token vending
status: draft
author: kyle
created: 2026-08-27
last-audited: 2026-08-27
audited-at: 152c00e99
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: AgentCore Identity and Gateway — Crew agent identity and token vending

## Summary

Give each Kiro Crew agent a first-class **Amazon Bedrock AgentCore
Identity** workload identity, and use **AgentCore Gateway** as the
outbound token-vending plane for MCP tools. Crew does not implement
OAuth, RFC 8693, or a token vault. The companion edition registers the
workload, vends tokens, and contributes the Gateway MCP endpoint. The
public core grows generic, default-off seams so a standalone install
stays byte-identical and never imports AWS.

This is the **identity and credential** plane. It is not the sandbox /
execution plane in the sibling AgentCore sandboxes design.

**Implementation plan:**
[`../superpowers/plans/2026-08-27-agentcore-identity-gateway.md`](../superpowers/plans/2026-08-27-agentcore-identity-gateway.md)

## Motivation

### Current state (verified at `152c00e99`)

Crew has an operator-identity seam and no agent-identity or token-vending
seam.

| Surface | What exists | What it is not |
|---|---|---|
| `IdentityProvider` (`platform/interfaces.py:306`) | SSO status, preflight, MCP credential-watch paths | Not consumed as a principal. `whoami` / `issuer` are **RESERVED** (`platform/context.py:150`) |
| `DefaultIdentityProvider` | Delegates to `sso_status.py` stubs (`available: false`) | No SSO, no JWT, no workload |
| `McpToolingProvider.extra_mcp_servers()` | ADD-only MCP specs merged into `kirocrew.json` | Static at rebuild time. No per-session `Authorization` |
| `kiro_oauth_wire_entry` (`mcp_utils.py:160`) | Translates remote MCP OAuth hints for kiro-cli | Operator-managed client credentials, not AgentCore |
| MCP `headers` on URL servers (`mcp_discovery.py`) | Static headers from config | A baked bearer is a live credential in an agent-readable file |
| Dashboard tokens (`dashboard/token_auth.py`) | HMAC-SHA256, IP-pinned, single-use | Not an OIDC JWT. Cannot satisfy Gateway `CUSTOM_JWT` |
| Channel session keys (`session.md`) | `slack:…`, `dashboard:…`, cron keys | Surface routing, not a cryptographic user identity |
| Governance `SCOPE_CATALOG` | `mcp`, `network.egress`, `capabilities.*` | No AgentCore / token-vend row |
| Credential redaction | AKIA/ASIA floor + bearer-header heuristics | No AgentCore workload-token shape |

Grep of `src/`, `website/src/`, and `docs/` at `152c00e99` finds **no**
`AgentCore`, `bedrock-agentcore`, `GetWorkloadAccessToken`, or
`GetResourceOauth2Token` symbol.

### Problems

1. **The Crew agent has no identity AgentCore can name.** Gateway policy,
   CloudTrail, and the Identity token vault key on a workload identity.
   Today the caller is "whatever IAM role the host has," which cannot
   distinguish `kirocrew` from a sibling agent, a cron job, or a
   compromised local process.
2. **Outbound tool credentials are operator-local secrets.** Slack bot
   tokens, GitHub PATs, and static MCP `Authorization` headers live on
   disk or in env. There is no on-behalf-of exchange, no per-user vault
   binding, and no short-lived scoped token.
3. **A static Gateway URL is not an integration.** An operator can paste
   a Gateway MCP URL into `~/.kiro/crew/mcp.json` today. That carries no
   agent identity, no refresh, no per-session principal, and no 3LO
   consent surface. It also puts a bearer in an agent-readable spec.
4. **Unattended work has no honest principal.** Cron, TaskRunner, and
   subagents are not the operator at a keyboard. ForUserId without a
   trusted derivation is impersonation.

### What AgentCore actually is (product shape)

Two AWS services, not one:

**AgentCore Identity** is the workload identity directory and token
vault.

- A **workload identity** names an agent (`kirocrew`, later
  `kirocrew-<agent_id>`).
- `GetWorkloadAccessToken` / `ForJWT` / `ForUserId` mint an opaque
  **workload access token** bound to `(workload, user)`.
- That token is **first-party only**. It authorizes AgentCore Identity
  APIs (`GetResourceOauth2Token`, vault reads). It is not a Gateway
  inbound credential and must never be sent to a downstream MCP server.
- `GetResourceOauth2Token` vends an OAuth token for a named credential
  provider (`M2M`, authorization-code / 3LO, or `TOKEN_EXCHANGE` /
  on-behalf-of).
- Runtime-managed and Gateway-managed workload identities **cannot**
  call `GetWorkloadAccessToken` themselves. A Crew-owned identity must
  be a **standalone** workload, not the Gateway's.

**AgentCore Gateway** is a hosted MCP endpoint.

- Inbound: `CUSTOM_JWT` (OIDC discovery + JWKS) or `AWS_IAM`. JWT is
  required for OBO; IAM is machine-to-machine and has no user `sub`.
- The Gateway obtains its **own** workload access token and asks
  Identity to vend the outbound credential for the target.
- Outbound grant types: client credentials, authorization code, RFC 8693
  token exchange (`TOKEN_EXCHANGE`).
- Optional Cedar policy engine and request interceptors.

The official [Kiro IDE + AgentCore Gateway](https://builder.aws.com/content/3CS1jTWHngGW3IxFXCjcP2T9l8B/govern-mcp-tools-at-scale-with-kiro-and-agentcore-gateway)
pattern is "IDE presents a developer OIDC JWT; Gateway vends outbound
tokens." Crew is a **local orchestrator**, not Kiro IDE and not AgentCore
Runtime. Runtime auto-injects `WorkloadAccessToken` into hosted agent
code; Crew never runs there, so it must mint its own workload token when
it calls Identity APIs.

## Goals

- Register one standalone AgentCore workload identity for the Crew
  agent, visible in Identity status, CloudTrail, and vault bindings.
- Use AgentCore Gateway as the token-vending plane for approved MCP
  targets. Crew does not implement RFC 8693.
- Bind vault entries to a **trusted** `(workload, user)` pair. Prefer
  `GetWorkloadAccessTokenForJWT`. Use `ForUserId` only with a
  core-derived, partitioned subject.
- Inject Gateway inbound credentials **per session**, never into
  `~/.kiro/agents/kirocrew.json`.
- Public edition remains complete standalone: no AWS SDK, no AgentCore
  import, no new default-on egress, no Cognito/SSO reintroduction.
- Fail closed. A missing companion, expired JWT, or Identity error
  denies the Gateway call. It does not fall back to a shared token.

## Non-goals

- Hosting Crew on AgentCore Runtime, or treating Gateway as an
  `agent.provider`. `agent.provider` stays `acp`.
- A new OS-sandbox backend or Instances `connection_method`. That is
  the sibling sandboxes RFC.
- Re-adding enterprise SSO, Cognito, Midway, or device-posture tunnels
  to the public core. `sso_status.py` stays a stub. Companion SSO lands
  through the existing `IdentityProvider` slot.
- Crew-side `GetResourceOauth2Token` for arbitrary local tools in v1.
  Gateway vends. Direct Identity vending is a later, narrowly-scoped
  follow-on.
- Making `IdentityProvider.whoami` / `issuer` live. Those stay
  RESERVED. Surface principal data through wired `status()` payloads.
- Bumping `CONTRACT_VERSION`. Pre-launch field and method adds stay at
  `1`, matching `knowledge` / `dashboard` / `jail`.
- A public `config.json` AgentCore block. Agent-writable config cannot
  be the trust root for workload name, gateway URL, or region.

## Design

### Target architecture

```
Operator / channel user
        │  dashboard token, Slack user id, companion SSO JWT
        ▼
Kiro Crew gateway (local, ACP → kiro-cli)
        │
        ├─ AgentIdentityProvider.workload_identity()
        │     standalone AgentCore workload "kirocrew"
        │
        ├─ AgentIdentityProvider.vend_workload_access_token(principal)
        │     Identity: GetWorkloadAccessTokenForJWT | ForUserId
        │     first-party token; never leaves the gateway process
        │
        └─ AgentIdentityProvider.vend_gateway_inbound_token(principal)
              user OIDC JWT (or companion-minted audience-bound JWT)
                    │
                    ▼
         AgentCore Gateway  (MCP, CUSTOM_JWT)
                    │  Gateway's own workload token
                    ▼
         AgentCore Identity token vault
                    │  GetResourceOauth2Token (M2M / 3LO / OBO)
                    ▼
         Gateway target (Slack, GitHub, internal API, MCP server)
```

Two tokens, two audiences, never interchangeable:

| Token | Audience | Who mints it | Who holds it |
|---|---|---|---|
| Workload access token | AgentCore Identity APIs only | Identity (`GetWorkloadAccessToken*`) | Crew gateway process, in memory |
| Gateway inbound JWT | Gateway `customJWTAuthorizer` | Companion IdP / SSO | Injected as `Authorization` on the Gateway MCP transport for that session |
| Outbound resource token | Downstream API | Identity, **called by Gateway** | Gateway; Crew never sees it in v1 |

### Edition split

The public core defines the protocol, the session-principal derivation,
the MCP header-injection site, governance, redaction, and SEL. It ships
`DefaultAgentIdentityProvider` (all methods empty / `enabled() ==
False`).

The enterprise companion (separate package, `kirocrew.plugins` entry
point) implements the protocol with `bedrock-agentcore` / boto3, holds
region and workload ARN, talks to the operator IdP, and contributes the
Gateway URL through `McpToolingProvider.extra_mcp_servers()`.

Dependency stays one-way: companion depends on core. Core never imports
`bedrock_agentcore`, never names Cognito, never hardcodes a discovery
URL.

### New CPP slot: `agent_identity`

A new `AgentIdentityProvider` Protocol on `PlatformContext`, not more
methods on `IdentityProvider`.

`IdentityProvider` is operator SSO (status line, preflight, credential
watch). Agent workload identity and token vending are a different
edition concern. The same reason `AgentCatalogProvider` is not folded
into `McpToolingProvider` applies here.

Pre-launch, a new `PlatformContext` field does **not** bump
`CONTRACT_VERSION` (pinned at `1`; `knowledge` / `dashboard` / `jail`
landed the same way). `DefaultAgentIdentityProvider` keeps a standalone
process byte-identical.

```python
@dataclass(frozen=True)
class WorkloadIdentity:
    name: str
    arn: str

@dataclass(frozen=True)
class SessionPrincipal:
    """Trusted caller. Core-derived; never taken from tool input."""
    surface: str          # dashboard | slack | discord | telegram | …
    subject: str          # already partitioned: "{surface}+{id}"
    session_key: str
    user_jwt: str | None  # set only by the companion after IdP verify

@dataclass(frozen=True)
class InboundToken:
    scheme: str           # "bearer"
    token: str
    expires_at: float     # unix epoch seconds
    audience: str

class AgentIdentityProvider(Protocol):
    def enabled(self) -> bool: ...
    def workload_identity(self) -> WorkloadIdentity | None: ...
    def status(self) -> dict[str, object]: ...
    def gateway_mcp_spec(self) -> dict[str, object] | None: ...
    async def annotate_principal(
        self, principal: SessionPrincipal
    ) -> SessionPrincipal: ...
    async def vend_workload_access_token(
        self, principal: SessionPrincipal
    ) -> str | None: ...
    async def vend_gateway_inbound_token(
        self, principal: SessionPrincipal
    ) -> InboundToken | None: ...
```

`status()` is display-only: `{enabled, workloadName, gatewayConfigured,
principalBound}` and never token material. The dashboard merges it next
to the existing `IdentityProvider.status()` payload (or a sibling
`GET /api/agent-identity` if merging would confuse the SSO TTL probe).

`whoami` / `issuer` on `IdentityProvider` stay RESERVED. Do not consume
them to satisfy this RFC.

### Session principal (core, trusted)

The core builds `SessionPrincipal` from ground truth it already has.
The adapter may **annotate** (attach a verified JWT). It may not
replace `subject` with a client-supplied user id.

| Surface | `subject` | JWT available? |
|---|---|---|
| Dashboard (companion SSO) | `dashboard+{idp_sub}` | Yes — use `ForJWT` |
| Dashboard (OSS token auth) | `dashboard+{local_owner}` | No — `ForUserId` only if companion is enabled and the local owner is the host principal |
| Slack / Discord / … | `{channel}+{provider_user_id}` | Only if companion SSO has bound that channel user |
| CLI | `cli+{os_user}` | Same as local owner |
| Cron / TaskRunner | `cron+{job_owner}` (the operator who created the job, persisted at create time) | No interactive JWT. M2M Gateway targets only, or fail closed |
| Subagent | inherit parent principal | Same token audience as parent |
| Injected cron / subagent-completion envelopes | **not a user** | Do not mint a user-bound token for an injected message |

`ForUserId` subjects are partitioned `provider_id+user_id` per the
Identity docs, so `slack+U0123` and `dashboard+U0123` cannot collide in
the vault.

IAM on the companion role denies `GetWorkloadAccessTokenForUserId` when
a JWT path exists for that surface. The core still prefers `user_jwt`
when `annotate_principal` set one.

### Gateway attach and per-session header injection

`gateway_mcp_spec()` / `extra_mcp_servers()` contribute a **URL-only**
remote MCP entry: endpoint, protocol, no `Authorization`.

The missing core seam is per-session header injection at MCP spawn,
analogous to `mcp_gateway` declared-env forwarding
(`security.md` § pooled-backend declared-env):

1. Session start resolves `SessionPrincipal` and calls
   `vend_gateway_inbound_token`.
2. On miss or expiry, the Gateway server is **absent** for that session
   (not present with an empty header). Fail closed.
3. The bearer is written to a `0600` session sidecar that kiro-cli /
   the MCP stub reads as transport headers. It is never merged into
   `~/.kiro/agents/kirocrew.json`.
4. The Gateway server is **unpooled** in v1 (`pool_identity` would have
   to include the bearer, which re-partitions on every refresh and
   leaks a credential into a hash input). Per-session spawn is the
   honest cost.
5. `IdentityProvider.credential_watch_paths()` (already wired) plus
   inbound-token expiry drain the session's Gateway transport on
   rotation.

A local token-proxy MCP (Crew attaches the header, then forwards) is
the fallback if kiro-cli cannot take per-session headers without a
rebuild. Phase 0 of the implementation plan probes this and writes the
verdict before Phase 3 ships.

### Token vending path (Gateway, not Crew)

v1 outbound vending is entirely Gateway + Identity:

- Companion registers Gateway targets and OAuth credential providers
  (M2M, 3LO, `TOKEN_EXCHANGE`) in the AgentCore control plane.
- Crew presents the inbound JWT. Gateway exchanges and calls the
  target.
- Crew never calls `GetResourceOauth2Token` in v1.
- Crew never logs, transcripts, or redacts the outbound token because
  it never holds it.

3LO / consent: when Identity returns `authorizationUrl` + `sessionUri`
instead of a token, Gateway fails the MCP call. The companion must
surface that URL on a **human** channel (dashboard modal or the
originating chat thread), never as model-visible "click this" text.
Reuse the existing operator-OAuth allowlist
(`security.py` `_load_operator_oauth_endpoints` /
`oauth_endpoints.json` keystone) so a consent URL to an unknown host
is refused.

### Unattended jobs

Cron and TaskRunner have no interactive JWT.

v1 policy:

- Gateway targets whose credential provider is **M2M** (client
  credentials, no user) may run unattended, under the job-owner
  principal.
- OBO / 3LO targets are **denied** on unattended sessions unless a
  still-valid vaulted user token already exists for that
  `(workload, job_owner)` pair. There is no silent refresh via a
  guessed user id.
- Injected `[Cron notification]` / `[Subagent completion event]`
  messages do not mint a new principal. They run as the job/parent
  already bound.

### Governance, keystone, redaction

- New `SCOPE_CATALOG` row `capabilities.agentcore` with
  `capability_default=False` (opt-in, like `capabilities.publish` /
  `capabilities.messaging`). Data row only; evaluator untouched;
  `CONTRACT_VERSION` untouched.
- The capability gates: contributing the Gateway MCP server, calling
  either vend method, and surfacing 3LO consent. `network.egress` still
  bounds the Gateway host. `mcp` still bounds the server/tool identity.
- No `agentcore.json` in public `config.json`. Companion configuration
  lives in the companion. If a later public opt-in needs a file, it is
  a keystone path in `security._SENSITIVE_HOME_DIRS` (read **and**
  write, including extract verbs), next to `oauth_endpoints.json`.
- Workload access tokens and Gateway inbound JWTs are bearer material.
  Existing HTTP-bearer redaction covers the wire shape; the companion
  `CredentialPolicy` overlay may add AgentCore-specific prefixes. Tokens
  never enter SEL payloads, transcripts, or `status()`.
- SEL events (grant and deny): `agentcore.workload_token`,
  `agentcore.gateway_inbound`, `agentcore.consent_url`,
  `agentcore.unattended_denied`. No token bytes, no raw JWT.

### Dashboard and CLI

- Status only in v1: workload name, enabled, whether this session has a
  bound principal, Gateway configured. No token display, no "copy
  bearer."
- 3LO consent is a modal / channel prompt with the allowlisted URL.
- User-facing strings go through the i18n catalog
  (`website/docs/i18n-catalog.md`). Backend non-2xx bodies carry a
  machine-readable `code`.
- No emojis. `lucide-react` + `lucide-inline`.

## Migration plan

Each phase is independently shippable and abandonable. Exit criteria
are assertions, not dates.

### Phase 0 — Probe (this repo, no product surface)

Answer two questions and write the verdict into this RFC:

1. Can kiro-cli take per-session `Authorization` headers for a URL MCP
   server without writing them into the rendered agent JSON?
2. Does a standalone (non-Gateway-managed) workload identity in the
   target account accept `GetWorkloadAccessTokenForJWT` from the
   companion's IAM principal?

Exit: both answers recorded here. Phase 3 is blocked on (1). Phase 2
is blocked on (2). If (1) is no, implement the local header-proxy MCP
instead of kiro-cli header injection.

### Phase 1 — Core seams, public no-ops

Add `AgentIdentityProvider`, `DefaultAgentIdentityProvider`,
`PlatformContext.agent_identity`, bootstrap wiring, CPP coverage tests,
`capabilities.agentcore` (default off), and spec updates
(`platform-context.md`, `governance.md`). No AWS dependency. Standalone
behavior byte-identical.

Exit: `test_platform_cpp_seam_coverage.py` lists the new slot;
`enabled()` is False; no `bedrock` / `agentcore` import under
`src/kiro_crew/`.

### Phase 2 — Session principal + Identity vend (companion)

Companion registers the standalone workload, implements
`annotate_principal` / `vend_workload_access_token`, and fills
`status()`. Core derives `SessionPrincipal` at session start and
never accepts a tool-supplied user id.

Exit: companion tests (out of this repo) mint a workload token with
ForJWT; ForUserId subjects are partitioned; injected messages do not
vend.

### Phase 3 — Gateway MCP attach + inbound token injection

Companion contributes the Gateway URL. Core injects the inbound JWT
per session (or the header-proxy fallback). Unpooled. Expiry drains
the transport. Fail closed on miss.

Exit: a session with a valid JWT lists Gateway tools; a session
without does not see the server; `kirocrew.json` contains no
`Authorization` header.

### Phase 4 — Human 3LO consent + unattended policy

Consent URL allowlist + dashboard/channel prompt. Unattended jobs
restricted to M2M or vaulted-owner tokens. SEL events land.

Exit: an unknown consent host is refused; a cron job cannot OBO as an
arbitrary user.

### Phase 5 (follow-on, not v1) — Crew-direct `GetResourceOauth2Token`

Only if a local tool cannot sit behind Gateway. Same workload token,
same principal rules, same redaction. Do not start this phase to
"complete" v1.

## Backward compatibility

- Standalone / public wheel: no new imports, no new MCP server, no new
  default-on capability, no config migration.
- Companion: additive. A companion that does not override
  `agent_identity` inherits the Default (disabled).
- Existing static remote MCP servers and `kiro_oauth_wire_entry` are
  unchanged.
- `IdentityProvider` signatures unchanged. RESERVED methods stay
  reserved.

## Security considerations

- **Do not send a workload access token to Gateway.** Identity docs:
  first-party only. Gateway inbound is a user JWT or IAM.
- **Do not register a Gateway-managed workload as the Crew identity.**
  Those identities refuse `GetWorkloadAccessToken` from the caller
  ("WorkloadIdentity is linked to a service…").
- **Do not put tokens in agent JSON, transcripts, SEL, or `status()`.**
  Sidecar `0600`, process memory, then drop.
- **Do not take `userId` from the model, a tool argument, or a query
  string.** Core-derived principal only.
- **Do not fail open** to a shared service-account token when JWT
  vending fails.
- **Do not re-introduce Cognito / RUM ids / enterprise SSO** into
  `src/kiro_crew/`. Discovery URLs live in the companion.
- Computer use stays ungoverned and in-band. This RFC does not add
  `computer_use.*` scopes.
- Sensitive-path matchers must cover any later keystone file on both
  read and write/extract verbs.

## Alternatives considered

### A. Companion-only, no core seam (rejected as the long-term shape)

The companion could inject a Gateway MCP server with a static header
via `extra_mcp_servers()` today. That cannot do per-session OBO,
refresh, or keep the bearer out of `kirocrew.json`. Acceptable as a
manual operator escape hatch; not the integration.

### B. boto3 AgentCore client in the public core (rejected)

Violates the de-Amazoned fork rule, adds an AWS dependency to the
public wheel, and invites hardcoded Cognito/discovery values. The CPP
seam exists so the core never imports this.

### C. Deploy Crew onto AgentCore Runtime (rejected for this RFC)

Runtime auto-vends `WorkloadAccessToken` in the invocation payload.
Crew is a local multi-surface gateway (dashboard, Slack, cron,
subagents) that drives kiro-cli over ACP. Runtime has no inbound
dashboard TCP and is a different product. Revisit only if a hosted
Crew edition is separately designed.

### D. Extend `IdentityProvider` instead of a new slot (viable, not recommended)

v1 method adds on `IdentityProvider` would work and avoid a
`PlatformContext` field. The slot is already SSO-shaped
(`status_line`, `preflight_checks`). Token vending would overload it.
A dedicated protocol matches "one edition concern, one interface."

### E. Crew calls `GetResourceOauth2Token` and skips Gateway (rejected for v1)

Puts OAuth, 3LO, and per-target credential-provider config in Crew.
Gateway already does this, plus Cedar policy and interceptors. Crew
should present identity, not become a token broker.

### F. IAM inbound to Gateway, no JWT (rejected as the primary path)

Works for M2M. Drops user `sub`, so OBO and per-user vault binding
disappear. Allowed as a companion-only fallback for unattended M2M
targets, not for interactive sessions.

## Open questions

1. **kiro-cli per-session headers (Phase 0).** If the rendered agent
   JSON is the only header channel, v1 ships the local header-proxy
   MCP. Verdict goes here before Phase 3.
2. **One workload vs per-agent-config workloads.** v1 is one
   `kirocrew` workload. A later `kirocrew-<agent_id>` split is
   additive (new identities, same protocol).
3. **Dashboard route.** Merge AgentCore status into `GET /api/sso-ttl`
   vs a sibling `GET /api/agent-identity`. Prefer sibling so the SSO
   TTL probe stays an SSO probe.
4. **Channel-user SSO binding.** How the companion proves a Slack user
   *is* the IdP `sub` is a companion concern. This RFC only requires
   that the proof happen before `user_jwt` is set.
5. **Sibling sandboxes RFC.** If that design lands a Crew-driven
   Code Interpreter session, it must use this RFC's workload identity
   rather than minting a second one.

## Related

- [`platform-context.md`](../system-specs/modules/platform-context.md) —
  CPP seam, RESERVED methods, `CONTRACT_VERSION` pin
- [`security.md`](../system-specs/modules/security.md) — keystone,
  redaction, MCP env forwarding
- [`governance.md`](../system-specs/modules/governance.md) —
  `SCOPE_CATALOG` append-only
- [`mcp.md`](../architecture/mcp.md) — MCP merge, stateless tools
- [`injected-messages.md`](../system-specs/common/injected-messages.md)
  — cron / subagent envelopes are not the user
- [Get workload access token](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html)
- [GetResourceOauth2Token](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_GetResourceOauth2Token.html)
- [On-behalf-of token exchange](https://aws.amazon.com/blogs/machine-learning/implement-on-behalf-of-token-exchange-for-multi-tenant-agents-with-amazon-bedrock-agentcore-gateway/)
