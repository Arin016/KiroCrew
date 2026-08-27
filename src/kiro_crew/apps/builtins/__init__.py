# Built-in apps package.

BUILTIN_NAMES: list[str] = [
    "auto_improvement",
    "auto_research",
    "aws_control",
    "code_review_sage",
    "crew_companion",
    "issue_radar",
    "meetings",
    "ops_mission_control",
    "papyrus",
    "mochi",
    "personal_shopper",
    "pptx_maker",
    "spec_builder",
]

# Builtins that no longer exist as separate apps, so the startup migration can
# identify and clear a stale install left behind by an upgrade.
#
# Include BOTH forms -- hyphenated (the installed dir / manifest name) and
# underscored (the Python module name) -- because either can be what is on disk.
#
#   * deploy-web moved into the core deploy module.
#   * auto-triage-pipeline became a dashboard inside Issue Radar. Removing it from
#     BUILTIN_NAMES stops it being REGISTERED, but an install that already has it
#     keeps the directory and its installed.json entry, which would leave an App
#     Store card for an app with no manifest behind it -- present enough to show,
#     not present enough to open.
_MIGRATED_BUILTINS: list[str] = [
    "deploy-web",
    "deploy_web",
    "auto-triage-pipeline",
    "auto_triage_pipeline",
]
