# Verification Controller v1 — Implementation Plan

**Date:** 2026-09-02 · **Status:** approved (chat)

## Goal

Ship the verification controller as reviewable, independently testable changes:

1. a dashboard feature map that stays fresh;
2. isolated pod scenario seeding;
3. agent-facing pod inspection;
4. recipes that compose those capabilities.

The implementation is complete on the combined `feat/pod-verbs` source branch,
but that branch is not the intended merge unit. It is source material for the
single-capability PRs below.

## Non-negotiable delivery rules

1. **One externally visible capability per PR.** A PR may contain the internal
   support, tests, and capability-specific docs required to make that one
   capability real. It may not absorb another verb because the same Python file
   happens to contain both.
2. **One pod-capability PR open at a time.** Each PR targets `main`, receives the
   full main-targeting CI suite, reaches review-ready, and merges before the next
   capability PR opens. No stacked pod PRs and no merging into an intermediate
   feature branch.
3. **Reality is the acceptance boundary.** A capability that drives a pod is not
   complete until it succeeds against a real isolated pod. A permissive HTTP stub
   or an in-process call to `boot()` is supporting coverage, not acceptance.
4. **No silent degradation.** A requested seed that cannot land, an ownership
   proof that cannot complete, or an authentication scheme the gateway does not
   accept must refuse clearly rather than report success with less behavior.
5. **Review findings stay in their owning slice.** A finding against a later
   capability is recorded for that capability; it does not expand the PR under
   review. If a finding exposes a prerequisite without which the current
   capability cannot work, execution stops and this plan is superseded with
   explicit approval before topology changes.
6. **The plan is immutable once approved.** A topology or scope change requires a
   new task spec that names this one as superseded. PR bodies link this plan and
   restate their one-capability boundary.

## PR sequence

### PR 0 — Dashboard feature map and mechanical freshness gate

**Existing PR:** #7683 · **Public capability:** none (repository governance)

**Owns**

- `docs/feature-map/README.md`.
- The `Feature Map Gate` in `.github/workflows/ci.yml`.
- `scripts/check_feature_map.py`, its self-test, and focused pytest coverage.
- The local `prepare-pr` profile entry and CI documentation.

**Gate contract**

The gate nudges contributors when a structural dashboard feature change adds or
removes a page, dashboard handler, or route while leaving the map untouched. It
handles replacements explicitly: route swaps and file renames cannot net to “no
change.” Pure edits and non-dashboard features do not trigger it.

**Does not own**

- Any pod command, fixture, recipe, or pod lifecycle change.
- Semantic review of whether the contributor changed the correct map row.

**Acceptance**

- Self-test passes.
- Focused pytest suite passes.
- Main-targeting CI and reviewer lanes are green.

### PR 1 — `kirocrew pod up --seed <scenario>`

**Reuse:** #7817 after this plan is approved · **One public capability:** the
`--seed` option on the existing `pod up` command.

**Owns**

- Fixture registry internals and the ten seed fixtures.
- Resolution of a scenario name or explicit seed directory.
- Staged-copy-then-rename population of the isolated pod home.
- Channel/config sanitization and fail-closed rewrite behavior.
- Checkout-pinned pod boot through a per-instance systemd drop-in, because the
  globally installed CLI cannot execute worktree-only seed behavior.
- Post-health verification that a requested seed actually landed.
- Cleanup of the drop-in and pod home on `pod down`.
- Tests and user documentation needed specifically for `pod up --seed`.

**Expected production ownership**

- Seed-related hunks in `src/kiro_crew/cli.py`, `pod/cli.py`, and
  `pod/runtime.py`.
- `src/kiro_crew/seed.py` and `src/kiro_crew/pod/unit.py`.
- `src/kiro_crew/tests_fixtures/**`.
- Seed/drop-in portions of `test_seed.py`, `test_pod.py`,
  `test_pod_seed_scenarios.py`, and `test_pod_boot_override.py`.

**Explicitly excluded**

- The `pod scenarios` command.
- The `pod api` command, token ownership proof, and HTTP response handling.
- The pod-e2e `--no-suppress-first-run` fix.
- Cross-capability verification recipes.

**Live acceptance**

1. Build the worktree and start a real pod with
   `pod up <worktree> --seed crons-active --json`.
2. Confirm systemd executes the worktree’s venv binary, not the global install.
3. Confirm `pod exec <worktree> -- cron list` shows all three fixture jobs.
4. Confirm an unknown scenario fails and does not boot an unseeded pod.
5. Run `pod down`; confirm no pod home or systemd drop-in remains.

### PR 2 — `kirocrew pod scenarios`

**One public capability:** list the scenarios already supported by `pod up
--seed`.

**Owns**

- CLI parser and dispatch for `pod scenarios`.
- Stable scenario-name and description rendering from the fixture registry.
- Focused tests for ordering, descriptions, malformed manifests, and empty
  registries.
- Command-specific CLI and pod documentation.

**Explicitly excluded**

- New fixture payloads; those landed with the seeding capability they exercise.
- Any pod lifecycle, systemd, authentication, HTTP, or pod-e2e behavior.

**Acceptance**

- Lists every installed fixture exactly once in deterministic order.
- Reads no pod home and mutates no state.
- Full main-targeting CI and reviewers are green.

### PR 3 — `kirocrew pod api`

**One public capability:** authenticated HTTP calls into a running pod.

**Owns**

- `pod api <pod> <method> <path> [--data JSON] [--allow-write]`.
- Port-ownership attestation needed to mint the pod-scoped credential.
- The pid-record/MainPID agreement tier for hosts where listener tools cannot
  attribute another process’s socket.
- Gateway-compatible `?token=` authentication, with the token excluded from
  printed URLs and redacted from transport errors.
- Refusal of caller-supplied `token` query parameters before errors or SEL audit
  records can disclose them.
- Bounded response reads and structured transport failures.
- Safe-method default: GET/HEAD are permitted; POST/PUT/PATCH/DELETE require
  `--allow-write` and are marked `write=1` in the audit trail.
- Focused portability, audit, security-posture, CLI, and HTTP tests.

**Design decision**

No route-by-route side-effect catalog ships in v1. Such a catalog would live in
pod tooling, drift from dashboard route registration, and fail open whenever a
new route appears. A destructive safe-method route is a server contract defect
and is fixed at that route. The v1 client guard follows HTTP safe-method
semantics and targets the demonstrated threat: untrusted content steering an
agent into destructive non-safe methods.

**Expected production ownership**

- API and ownership-related hunks in `src/kiro_crew/cli.py`, `pod/cli.py`, and
  `pod/runtime.py`.
- `src/kiro_crew/instances/run_marker.py`.
- Listener-tool changes in `src/kiro_crew/platform_compat.py`.
- Required security-posture and spawn-audit registrations.

**Explicitly excluded**

- Seed fixtures, scenario listing, and seed-home mutation.
- Pod-e2e flag forwarding.
- Cross-capability recipes.

**Live acceptance**

1. Start an ordinary real pod and obtain a non-empty minted token from the
   ownership-checked path.
2. `pod api <pod> GET /api/crons` returns 2xx against the real gateway.
3. A path carrying a caller `token` parameter refuses without echoing its value.
4. A non-safe method refuses without `--allow-write` and succeeds with it.
5. Transport errors and oversized/truncated bodies return structured pod errors,
   never raw tracebacks or credential-bearing URLs.
6. Teardown leaves zero residue.

### PR 4 — pod-e2e `--no-suppress-first-run` forwarding fix

**One bug fix:** the documented flag is accepted and reaches the Playwright
driver.

**Owns**

- The parser/forwarding hunk in `pod-e2e.sh`.
- Focused harness-path tests proving acceptance, forwarding, default suppression,
  unknown-flag refusal, and usage discoverability.
- The matching pod-e2e skill text.

**Explicitly excluded**

- Seed flags, fixture APIs, pod HTTP calls, and verification recipes.

### PR 5 — Verification recipes and discoverability

**One documentation capability:** teach agents how to compose the already-merged
commands and existing session-control endpoints.

**Precondition:** PRs 1–4 are on `main`; every command shown has passed its live
acceptance test.

**Owns**

- `docs/guides/verification-recipes.md` and its guide index row.
- The `AGENTS.md` discoverability row.
- Worktree/pod-e2e skill guidance that composes the shipped commands.

**Explicitly excluded**

- Production code, fixtures, CLI parser changes, and new API surface.

**Acceptance**

- Every command is copied from current `--help` output and run against a real
  pod.
- Every completion condition observes the state it claims (for example, no
  queued turn is treated as complete).
- Documentation lint and brand checks pass.

### PR 6 — Feature-map semantic review parity

**One governance capability:** make reviewer judgment complement PR 0’s
mechanical nudge.

**Owns**

- A reviewer rule that activates when the feature map changes or the PR adds or
  removes a watched page, handler, or route.
- Judgment that every structural feature change has the corresponding row
  addition/update/deletion.
- Verification that nav path, frontend page, backend handler, and representative
  endpoints are supported by the changed code.
- Rejection of unrelated map churn or a token edit made only to appease CI.
- Verification that the map’s net diff matches the PR’s stated scope.

**Explicitly excluded**

- Reimplementing semantic judgment in `check_feature_map.py`.
- Expanding the map beyond its dashboard scope.

**Acceptance**

- Reviewer-contract tests include: correct row, wrong row, unrelated map touch,
  missing deletion, route swap, and rename.
- The existing mechanical gate remains deterministic and unchanged.

## Extraction procedure

1. Merge PR 0 (#7683).
2. Before rewriting the combined source branch, create a **local-only backup
   ref** so the final aggregate tree can be compared for omissions. Do not push
   the backup ref.
3. Rewrite #7817 to PR 1 only; update its title/body to link this plan and name
   every excluded capability.
4. Drive PR 1 through full CI, reviewer convergence, and its live acceptance;
   merge it to `main`.
5. Create PR 2 from fresh `origin/main`; repeat through PR 6. Do not open the
   next pod-capability PR early.
6. Shared files (`cli.py`, `pod/cli.py`, `pod/runtime.py`, and broad test modules)
   are partitioned by hunk and symbol. Their shared location is never a reason to
   combine capabilities.
7. After PR 5 merges, compare the aggregate resulting tree against the local
   combined-source backup. Every intentional difference must be attributable to
   a review fix or an explicitly excluded hunk; no capability may disappear
   silently during extraction.

## Verification floor for every code PR

- Focused red-first regression tests for the capability’s own behavior.
- Full backend floor: pytest, isort, flake8, mypy with Linux parity.
- Full frontend floor when frontend files change: TypeScript build and Vitest.
- Every changed ratchet/gate listed in CI, including subprocess encoding,
  formatting, brand, harness parity, and security/spawn audit guards.
- Main-targeting server CI and reviewer convergence before merge.
- The capability-specific live acceptance above when the code drives a pod.

## Stop conditions

Stop and return for a decision instead of widening a PR when:

- a fix requires another externally visible verb;
- a live smoke contradicts the PR’s unit-test model;
- a third blocking review round identifies the same methodological gap;
- a base merge or history rewrite makes the PR show commits from another slice;
- the implementation can only proceed by weakening ownership, sandbox, audit, or
  credential controls.
