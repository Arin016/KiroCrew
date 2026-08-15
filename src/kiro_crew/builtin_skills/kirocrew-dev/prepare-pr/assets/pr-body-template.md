<!-- This template MUST mirror .github/PULL_REQUEST_TEMPLATE.md in the Kiro Crew
     repo — that file is the single source of truth for required PR description
     sections. The maintainer's auto-approval bot greps for these exact headings
     (prefix-match); mismatched headings block workflow approval indefinitely.

     When filling this in: replace the HTML comments with real content. Delete
     any section that is genuinely N/A and say why in one line. -->

## Problem / Motivation

<!-- Bug fix: the concrete symptom — what is broken or missing, ideally what the
     user observes.
     New feature / enhancement: the gap, use case, or opportunity this addresses
     — what a user cannot do (or does awkwardly) today. -->

## Why it matters

<!-- Impact if this is left undone: for a fix, who is hit by the bug and how
     badly; for a feature, the user/business value it unlocks. -->

## What changed (motivation → approach → change)

<!-- A short chain of thought so the reader sees *why this is the right change*,
     not just what changed:
     - Bug fix: observed symptom → underlying root cause → the specific change
       that addresses that cause.
     - New feature / enhancement: goal → the approach/design you chose (and why,
       over the alternatives you considered) → what you actually built. -->

## Tests

<!-- Automated tests added/updated and the behavior each one locks in. -->

## Manual verification

<!-- Manual steps performed or still required where unit tests fall short
     (integration paths, UI, external services). State "N/A — unit coverage
     sufficient" only when genuinely true, with a one-line why. -->

## Screenshots / video

<!-- MANDATORY for any user-visible UI change; delete this section otherwise.
     Commit media under temp-screenshots/<feature>/ and embed with
     commit-SHA-pinned URLs. See the prepare-pr skill's "Screenshots" contract
     for the full recipe. -->

## Related Issues

<!-- Link with a closing keyword — "Fixes #N", "Closes #N", or "Resolves #N".
     A bare "#N" or "Related: #N" links but closes NOTHING on merge.
     If no issue: delete this section and state "no linked issue: <why>" on its
     own line instead. -->

## Checklist

- [ ] Single commit with a Conventional Commits title (`feat|fix|docs|refactor|perf|test|chore|ci|build|revert: ...`)
- [ ] Existing tests pass and new tests added for new functionality
- [ ] Self-review completed; code follows project style guidelines
- [ ] Documentation updated (if applicable)
- [ ] No secrets, credentials, or internal references in the diff
