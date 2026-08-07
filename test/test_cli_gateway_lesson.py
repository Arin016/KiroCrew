"""Regression tests for the CLI's gateway lesson-delegation path.

Both guards here cover findings on ``_gateway_add_lesson`` in
``src/kiro_crew/cli_commands.py``:

1. It dialed the loopback HOSTNAME (``http://localhost:{port}``) while carrying an
   ``X-Internal-Secret``. On a dual-stack host the name can resolve to ``::1``
   first, so a foreign process listening on ``[::1]:port`` would receive the
   internal API credential -- and in the benign case the probe simply misses a
   gateway bound to ``127.0.0.1``. Amazon SSRF guidance names this class directly
   (``localhost`` / ``ip6-localhost`` / ``ip6-loopback`` are distinct OS-level
   names, and IPv4 services stay reachable via IPv4-mapped IPv6).

2. It imported ``kiro_crew.mcp_core`` at MODULE scope. ``mcp_core`` evaluates
   ``_API = _resolve_api_base()`` at import time, which calls
   ``parse_dashboard_url``; that degrades gracefully on a malformed *string* but
   only catches ``ValueError``, while ``_ensure_scheme``'s ``"://" in url`` raises
   ``TypeError`` on a non-string. A numeric ``dashboard.url`` therefore crashed
   EVERY kirocrew command at import -- including the ``config`` and ``doctor``
   commands needed to repair it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CLI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "cli_commands.py"


class TestGatewayLessonLoopbackHost:
    """Every credential-bearing request must target a literal loopback ADDRESS.

    Covers ``_gateway_add_lesson`` and ``_artifact`` -- both build a loopback base
    and then send ``X-Internal-Secret``. ``_artifact`` predates the lesson work and
    carried the identical hazard; it is asserted here because the invariant is
    per-file, not per-function, and a future call site would otherwise reintroduce
    it silently.
    """

    def test_uses_literal_ip_not_localhost_name(self):
        src = _CLI_SOURCE.read_text(encoding="utf-8")
        assert 'f"http://127.0.0.1:{port}"' in src, (
            "the loopback base must be the literal 127.0.0.1"
        )
        assert 'f"http://localhost:{port}"' not in src, (
            'no call site may dial the NAME "localhost" while sending '
            "X-Internal-Secret: on a dual-stack host it can resolve to ::1 and hand "
            "the internal credential to a foreign listener"
        )


class TestCliDoesNotImportMcpCoreAtModuleScope:
    """A broken ``dashboard.url`` must not brick every CLI command at import.

    Asserted on the AST rather than by importing with a poisoned config: the
    failure is an import-time side effect, and once ``mcp_core`` is in
    ``sys.modules`` from any earlier test the crash cannot be reproduced in-process.
    The static check is what actually holds the invariant.
    """

    def test_no_top_level_mcp_core_import(self):
        tree = ast.parse(_CLI_SOURCE.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:  # module scope ONLY -- nested imports are the fix
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "kiro_crew.mcp_core"
            ):
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("kiro_crew.mcp_core"):
                        offenders.append(f"line {node.lineno}: import {alias.name}")
        assert not offenders, (
            "cli_commands must not import mcp_core at module scope -- mcp_core runs "
            "KiroCrewConfig.load() + parse_dashboard_url() at import time, so a "
            "malformed dashboard.url would crash every CLI command before dispatch. "
            f"Found: {offenders}"
        )

    def test_resolver_is_still_used_inside_the_function(self):
        """The fix must CONTAIN the import, not drop the resolver.

        Reading ``KIROCREW_SESSION_KEY`` alone would not do: warm-pool kiro-cli
        processes have no such env var, so delegation would silently stop working
        in the pooled topology. The PID-file ancestor walk in
        ``_resolve_session_key`` is what makes it resolve there.
        """
        src = _CLI_SOURCE.read_text(encoding="utf-8")
        assert "from kiro_crew.mcp_core import _resolve_session_key" in src, (
            "the hardened resolver must still be used (lazily), not replaced by a "
            "bare env-var read"
        )
        assert "_resolve_session_key()" in src
