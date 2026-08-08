"""Security Scanner builtin app package.

A self-improving adversarial security scanner: topic-based parallel scanning
with tagged knowledge, sandboxed proof-of-concept exploit validation against an
isolated pod, and a learning loop over confirmed exploits and false positives.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.security_scanner")`` then
# checks ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule). Without this re-export the gateway skips route
# registration and every ``/api/apps/security-scanner/*`` request 404s.
# issue_radar / code_review_sage do the same.
from .backend.routes import register_routes  # noqa: F401
