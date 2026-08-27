# AgentCore Identity and Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Crew agent a standalone AgentCore Identity workload
and use AgentCore Gateway as the outbound token-vending plane, behind
default-off CPP seams so the public edition stays byte-identical.

**Architecture:** A new `AgentIdentityProvider` slot on
`PlatformContext` holds workload identity and token vending. The public
`Default` is empty. The companion talks to Identity (`GetWorkloadAccessToken*`)
and contributes the Gateway MCP URL. The core derives a trusted
`SessionPrincipal`, injects a per-session inbound JWT (never into
`kirocrew.json`), and leaves outbound OAuth to Gateway. `CONTRACT_VERSION`
stays `1`.

**Tech Stack:** Python 3.10+, existing CPP (`platform/`), MCP gateway
rewriter, governance `SCOPE_CATALOG`, pytest-asyncio. Companion-only:
`boto3` / `bedrock-agentcore` (not a public-wheel dependency).

**Spec:**
[`../../request-for-change/rfc-agentcore-identity-gateway.md`](../../request-for-change/rfc-agentcore-identity-gateway.md)

## Global Constraints

- Core never imports `bedrock_agentcore`, `boto3` AgentCore clients, or
  names Cognito / Midway / a discovery URL.
- Do not consume `IdentityProvider.whoami` or `issuer` (RESERVED).
- Do not write tokens into `~/.kiro/agents/kirocrew.json`, transcripts,
  SEL payloads, or `status()`.
- Do not take `userId` from the model, a tool argument, or a query
  string.
- Fail closed: missing companion, expired JWT, or vend error means the
  Gateway server is absent for that session.
- `capabilities.agentcore` defaults **off**. No new default-on egress.
- `sso_status.py` stays a stub. No `CHANGELOG.md` edit.
- Computer use stays ungoverned. No `computer_use.*` scopes.
- Update the owning spec in the same commit as the code it covers.
- Run `scripts/docs-lint.sh` after every docs change.
- Frontend user-facing strings go through the i18n catalog. No emojis.

---

## Stack and file map

| PR | Branch suffix | Primary files |
|---|---|---|
| 1 | `plan` (this PR) | RFC, this plan, RFC + plans indexes |
| 2 | `seams` | `platform/interfaces.py`, `defaults.py`, `context.py`, `bootstrap.py`, CPP coverage tests, `SCOPE_CATALOG`, `platform-context.md`, `governance.md` |
| 3 | `principal` | session-principal derivation, injected-message guard, unit tests, `session.md` |
| 4 | `probe` | Phase 0 verdict written back into the RFC (kiro-cli headers + standalone workload) |
| 5 | `inject` | per-session Gateway header sidecar **or** header-proxy MCP; `mcp.md`; unpooled Gateway server |
| 6 | `consent-unattended` | 3LO allowlist + dashboard/channel prompt + cron/M2M policy + SEL; `security.md` |
| 7 | companion (out of tree) | real `AgentIdentityProvider`, workload registration, IdP JWT, Gateway URL |

PRs 2, 3, 5, and 6 are this repository. PR 4 is a research commit that
only edits the RFC. PR 7 is the enterprise companion package.

### Stable interfaces

```python
@dataclass(frozen=True)
class WorkloadIdentity:
    name: str
    arn: str

@dataclass(frozen=True)
class SessionPrincipal:
    surface: str
    subject: str
    session_key: str
    user_jwt: str | None = None

@dataclass(frozen=True)
class InboundToken:
    scheme: str
    token: str
    expires_at: float
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

`DefaultAgentIdentityProvider`: `enabled() -> False`; all other methods
return `None` / `{}` / the input principal unchanged.

---

### Task 1: PR 1 — record the design and this plan

**Files:**

- Create: `docs/request-for-change/rfc-agentcore-identity-gateway.md`
- Create: `docs/superpowers/plans/2026-08-27-agentcore-identity-gateway.md`
- Modify: `docs/request-for-change/README.md`
- Modify: `docs/superpowers/plans/README.md`

**Interfaces:**

- Consumes: CPP slot table, RESERVED identity methods, MCP merge rules,
  AgentCore Identity/Gateway product shape.
- Produces: locked design + PR-sized implementation map.

- [x] **Step 1: Write the RFC and this plan.**

- [x] **Step 2: Index both documents.**

  Add a `draft` row to `docs/request-for-change/README.md` naming the
  commit they were verified against (`152c00e99`). Link this plan from
  `docs/superpowers/plans/README.md`.

- [x] **Step 3: Verify documentation.**

  Run: `bash scripts/docs-lint.sh && git diff --check`
  Expected: both exit zero.

- [x] **Step 4: Commit.**

  ```
  docs: add AgentCore identity and gateway plan
  ```

---

### Task 2: PR 2 — CPP slot and governance row (public no-ops)

**Files:**

- Modify: `src/kiro_crew/platform/interfaces.py` (add dataclasses + Protocol)
- Modify: `src/kiro_crew/platform/defaults.py` (add `DefaultAgentIdentityProvider`)
- Modify: `src/kiro_crew/platform/context.py` (add `agent_identity` field)
- Modify: `src/kiro_crew/platform/bootstrap.py` (wire Default)
- Modify: `src/kiro_crew/platform/__init__.py` (exports)
- Modify: `src/kiro_crew/platform/governance.py` (`capabilities.agentcore`)
- Modify: `test/test_platform_cpp_seam_coverage.py` (and any bootstrap/context tests the coverage file names)
- Modify: `docs/system-specs/modules/platform-context.md`
- Modify: `docs/system-specs/modules/governance.md`

**Interfaces:**

- Consumes: the Protocol above.
- Produces: a composed `ctx.agent_identity` that is disabled in
  standalone.

- [ ] **Step 1: Write the failing coverage test.**

  Assert `PlatformContext` has `agent_identity`, the default adapter's
  `enabled()` is False, `workload_identity()` is None,
  `gateway_mcp_spec()` is None, `status()` is a dict with no token-like
  keys, and `capabilities.agentcore` exists with
  `capability_default=False`.

- [ ] **Step 2: Run the test and verify RED.**

  Run: `python -m pytest test/test_platform_cpp_seam_coverage.py -n0 -q -k agent_identity`
  Expected: FAIL because the field / scope does not exist.

- [ ] **Step 3: Implement Default + catalog row.**

  Follow existing v1 addition comments (`no CONTRACT_VERSION bump`).
  `safe_context_call` fallback for every new method must be the disabled
  answer (False / None / `{}` / unchanged principal), never a raised
  error that degrades to "enabled."

- [ ] **Step 4: Grep the public tree for AWS leakage.**

  Run: `rg -n "bedrock.agentcore|bedrock_agentcore|GetWorkloadAccessToken|cognito-idp" src/kiro_crew website/src`
  Expected: no matches.

- [ ] **Step 5: Update specs and run gates.**

  `platform-context.md` table gets an `agent_identity` row.
  `governance.md` documents the new capability as opt-in.
  Run: `black --target-version py310 <touched py>` then
  `python3 scripts/check_black_formatting.py` and
  `mypy --platform linux src/kiro_crew`.

- [ ] **Step 6: Commit.**

  ```
  feat: add agent_identity CPP slot and agentcore capability
  ```

---

### Task 3: PR 3 — trusted session principal

**Files:**

- Create: `src/kiro_crew/platform/agent_identity.py` (dataclasses if not
  already in interfaces; `derive_session_principal(slot_or_session)`)
- Create: `test/test_agent_identity_principal.py`
- Modify: session start site(s) — the smallest existing hook that already
  knows `session_key` + surface (likely `session` / chat runner / channel
  dispatch). Do **not** invent a second session key.
- Modify: `docs/system-specs/modules/session.md`

**Interfaces:**

- Consumes: existing session key and channel identity.
- Produces: `SessionPrincipal` with partitioned `subject`.

- [ ] **Step 1: Write failing derivation tests.**

  ```python
  def test_dashboard_owner_is_partitioned():
      p = derive_session_principal(surface="dashboard", raw_id="alice", session_key="dashboard:1")
      assert p.subject == "dashboard+alice"
      assert p.user_jwt is None

  def test_tool_input_cannot_supply_subject():
      # whatever helper rejects kwargs from tool_input
      ...

  def test_injected_cron_envelope_does_not_derive_a_user():
      assert derive_session_principal_for_injected("[Cron notification from \"job\"]") is None
  ```

- [ ] **Step 2: Run tests, verify RED, then implement.**

  Run: `python -m pytest test/test_agent_identity_principal.py -n0 -q`

- [ ] **Step 3: Call `annotate_principal` through `safe_context_call`.**

  Fallback = the core-derived principal unchanged. Companion may set
  `user_jwt`. It must not change `subject`. Add a test that a stub
  adapter attempting to rewrite `subject` is ignored or rejected.

- [ ] **Step 4: Commit.**

  ```
  feat: derive partitioned AgentCore session principals
  ```

---

### Task 4: PR 4 — Phase 0 probe verdict

**Files:**

- Modify: `docs/request-for-change/rfc-agentcore-identity-gateway.md`
  (Open question 1 + Phase 0)

No product code. Record:

1. Whether kiro-cli accepts per-session MCP headers without persisting
   them in the rendered agent file (experiment against current kiro-cli;
   cite version).
2. Whether a standalone workload identity accepts
   `GetWorkloadAccessTokenForJWT` from the companion IAM role (companion
   repo or a scratch account; paste only the error string, no tokens).

- [ ] **Step 1: Run the two probes.**

- [ ] **Step 2: Write the verdict into the RFC Open questions section.**

- [ ] **Step 3: Commit.**

  ```
  docs: record AgentCore Phase 0 header and workload verdicts
  ```

Phase 5 implements **exactly one** of: kiro-cli sidecar headers, or the
local header-proxy MCP. Do not implement both.

---

### Task 5: PR 5 — Gateway attach and inbound injection

**Files (sidecar path, if Phase 0 said yes):**

- Modify: `src/kiro_crew/mcp_gateway/` rewriter / session spawn
- Modify: `src/kiro_crew/agent.py` (`_extra_mcp_servers` merge of
  `gateway_mcp_spec()`, URL only)
- Create: `test/test_agentcore_gateway_inject.py`
- Modify: `docs/architecture/mcp.md`

**Files (proxy path, if Phase 0 said no):**

- Create: `src/kiro_crew/mcp_gateway/agentcore_proxy.py` (stdio MCP that
  forwards to the Gateway URL with the session inbound token)
- Same tests and `mcp.md` update

**Either path:**

- [ ] **Step 1: Write failing tests.**

  - `enabled() is False` → no Gateway server in the rebuilt agent config.
  - `enabled() is True` but `vend_gateway_inbound_token` returns None →
    server still absent (fail closed).
  - Successful vend → transport has `Authorization: Bearer …` and
    `~/.kiro/agents/kirocrew.json` does not.
  - Two sessions with different principals do not share a backend
    (unpooled).
  - Token bytes never appear in a captured log / SEL fixture.

- [ ] **Step 2: Implement the Phase 0-chosen path only.**

  Gate contribution on `capabilities.agentcore` (fail closed when the
  capability is off, even if the companion `enabled()` is True).

- [ ] **Step 3: Commit.**

  ```
  feat: inject per-session AgentCore Gateway inbound tokens
  ```

---

### Task 6: PR 6 — consent surface and unattended policy

**Files:**

- Modify: `src/kiro_crew/security.py` (consent-host allowlist reuse of
  `oauth_endpoints.json`; do not add a second file unless the existing
  keystone cannot express AgentCore consent hosts)
- Modify: dashboard handler + a small Settings / modal component
  (`website/src/…`) with i18n keys
- Modify: cron / task start path to refuse OBO targets without a
  vaulted owner token (companion reports "m2m" vs "user" on
  `gateway_mcp_spec()` or status)
- Modify: SEL event names from the RFC
- Modify: `docs/system-specs/modules/security.md`
- Test: `test/test_agentcore_consent.py`, `test/test_agentcore_unattended.py`

- [ ] **Step 1: Write failing tests for unknown consent host, injected
  envelope, and cron-without-JWT.**

- [ ] **Step 2: Implement allowlist + fail-closed unattended policy.**

  User-facing copy is cataloged. Backend errors include `code`.
  No model-visible "click this URL" injection.

- [ ] **Step 3: Verify dashboard strings and the unattended path.**

  `cd website && npm run test` for the new modal/copy.
  Backend: `python -m pytest test/test_agentcore_consent.py test/test_agentcore_unattended.py -n0 -q`

- [ ] **Step 4: Commit.**

  ```
  feat: gate AgentCore 3LO consent and unattended vending
  ```

---

### Task 7: Companion package (out of this repository)

Not landed in KiroCrew. The companion implements
`AgentIdentityProvider` against:

- `bedrock-agentcore:GetWorkloadAccessToken`
- `bedrock-agentcore:GetWorkloadAccessTokenForJWT`
- `bedrock-agentcore:GetWorkloadAccessTokenForUserId` (denied in IAM
  when a JWT exists for that surface)
- A **standalone** workload identity named `kirocrew` (not
  Gateway-managed, not Runtime-managed)
- Gateway `CUSTOM_JWT` authorizer pointed at the operator IdP
- `McpToolingProvider.extra_mcp_servers()` URL-only Gateway spec
- `IdentityProvider` SSO JWT → `annotate_principal.user_jwt`

Companion checklist (for the other repo's plan):

- [ ] Create standalone workload; confirm `GetWorkloadAccessTokenForJWT`
      works; confirm a Gateway-linked workload returns the "linked to a
      service" error (negative test).
- [ ] IAM resource-scoped to
      `workload-identity-directory/default/workload-identity/kirocrew`.
- [ ] Prefer ForJWT; partition ForUserId as `{surface}+{id}`.
- [ ] Register Gateway targets + OAuth credential providers (M2M / 3LO /
      `TOKEN_EXCHANGE`) in the control plane, not in Crew config.
- [ ] `status()` contains no token material.
- [ ] Redaction overlay for any AgentCore-specific prefix the core
      bearer heuristic misses.

---

## Execution order and stop points

1. Land PR 1 (this documentation).
2. Land PR 2 before anything consumes the slot.
3. Land PR 3 before any vend call.
4. Run PR 4 **before** writing PR 5 code. If the header probe is
   inconclusive, stop and update the RFC rather than guessing.
5. PR 6 can overlap PR 5 once the injection tests exist, but do not
   merge consent UI that points at a server the inject path cannot
   attach.
6. Do not start PR 7 (companion) until PR 2's Protocol is on the core
   version the companion will pin.

**Do not implement Phase 5 of the RFC** (Crew-direct
`GetResourceOauth2Token`) in this stack.

## Verification before calling a phase done

- Public tree still has zero AgentCore SDK imports.
- `DefaultAgentIdentityProvider.enabled()` is False and no Gateway
  server appears in a standalone `rebuild_agent_config()`.
- `kirocrew.json` fixtures contain no `Authorization` header.
- Injected cron / subagent envelopes cannot vend a user token.
- `scripts/docs-lint.sh` and
  `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py`
  are clean on the files you added.
