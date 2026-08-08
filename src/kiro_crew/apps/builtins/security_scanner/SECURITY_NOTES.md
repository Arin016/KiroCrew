# Security Boundaries & Safety Defaults

This app **generates and executes adversarial exploit code**. Because it is a
security tool that attacks a target, the safety constraints below are load-bearing
— every later stage MUST uphold them. They are implemented as code guards (live-
target refusal, working-dir jail, wall-clock timeout, output cap, secret-scrub)
that exploit execution is expected to route through. NOTE (accuracy): today those
guards live in the library layer and fire when the security-scan skill calls them;
making them the ONLY reachable path — exposing the executor/service as an MCP tool
the skill MUST invoke — is tracked as a follow-up. Until then SKILL.md is binding:
PoCs run through the executor against an isolated pod, never ad-hoc bash.

## Governance status

No external security-governance policy service was available in this environment
to consult before building. Following the conservative default, the constraints
below were adopted as the secure baseline rather than inventing policy. If such a
service becomes available, these constraints should be revisited against it.

## Hard safety constraints (enforced in code)

1. **Exploit execution is sandbox-only.** Proof-of-concept scripts run ONLY against
   an isolated `kirocrew pod` instance (own port, own `KIROCREW_HOME`, no tunnel,
   `--no-crons`, cgroup memory/CPU caps). They MUST NEVER target the live gateway
   (`:5476`), production hosts, or any real cloud resource. The pod adapter refuses
   any target URL that resolves to `KIROCREW_POD_LIVE_PORT` / `:5476`.

2. **No outbound network from the app.** `permissions.network` is `false`. Generated
   exploit scripts run inside the pod's network boundary; the app itself makes no
   third-party requests. External report ingestion is file/paste only.

3. **Bounded exploit execution.** Every PoC runs under a wall-clock timeout, a
   captured-output size cap, and a working-directory jail. Runaway or
   resource-exhausting PoCs are killed, recorded as `TIMEOUT`/`ERROR`, and never
   retried automatically.

4. **No destructive operations.** PoCs are read/observe-oriented (prove access,
   prove a bypass). No `rm -rf`, no `DROP TABLE`, no credential-file reads
   (`~/.aws/*`, `~/.ssh/*`), no process kills by name. A confirmed-exploitable
   destructive class is reported as a finding, not demonstrated destructively.

5. **Secrets never surface.** Captured PoC evidence is scrubbed before persistence
   and display: credentials, tokens, and private keys are redacted; findings
   reference secrets by role/key name, never by value.

6. **Findings are never auto-filed.** No GitHub issue, no external post, no PR is
   created without explicit human confirmation. The scanner reports; the human acts.

7. **Knowledge store is append-with-audit.** Learned patterns and suppressions are
   added, never silently deleted; deletions require an explicit human action and are
   recorded in the store's activity log.

8. **Scans are read-only against the target's source.** Topic agents read code with
   grep/glob/read; they do not modify the codebase under scan.

## Target scoping

V1 scans **Kiro Crew only**, using the pod infrastructure as the exploit sandbox.
The target-adapter interface (`lib/targets.py`) is deliberately small so a future
adapter can point the scanner at another codebase — but that generalization is
out of scope until there is a real second target.
