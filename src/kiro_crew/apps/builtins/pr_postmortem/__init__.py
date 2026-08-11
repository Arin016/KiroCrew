"""PR Postmortem — links each merged fix PR to the PR that introduced the bug.

Attribution is mechanical: the lines a fix deleted or rewrote are blamed at the
fix's parent commit, and the resulting commits roll up to their pull requests. A
model then explains why review and tests missed the defect, and the findings
aggregate into prevention proposals a human accepts one at a time.

This pull request lands the ENGINE only: the attribution, evidence-bundle,
analysis-validation and backlog code, plus its tests. The app is deliberately not
registered in ``BUILTIN_NAMES`` yet and has no HTTP surface, so nothing imports
this package at runtime — the follow-up adds ``backend/routes.py``, the manifest,
and the ``register_routes`` re-export the gateway looks for on this package.
"""
