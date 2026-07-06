"""Regression tests for the legacy dashboard stored-XSS fix.

Pentest finding: static/js/dashboard.js used an esc() that escaped only & < >,
leaving " ' and ` unescaped. User-controlled slot names (title/data-key
attributes) and lesson rules (onclick handler) could break out of attribute
context and inject event handlers (script-src 'unsafe-inline' let them run).

Fix:
  1. esc() now escapes " ' and ` too -> attribute-context breakout closed.
  2. The three inline-onclick JS-string sinks (lesDel, showSkill, mcpToggle)
     were converted to data-* attributes + event delegation, removing the
     JS-string context entirely.

These tests read the shipped source file and (where node is available) execute
esc() against the exact pentest payloads to prove the breakout is neutralized.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DASHBOARD_JS = (
    Path(__file__).resolve().parent.parent
    / "src" / "kiro_claw" / "static" / "js" / "dashboard.js"
)


@pytest.fixture(scope="module")
def js_source() -> str:
    return _DASHBOARD_JS.read_text(encoding="utf-8")


def _extract_esc(js: str) -> str:
    """Pull the one-line `function esc(s){...}` definition out of the source."""
    m = re.search(r"function esc\(s\)\{return[^\n]*\}", js)
    assert m, "esc() definition not found"
    return m.group(0)


class TestEscEscapesAttributeChars:
    def test_esc_escapes_quotes_and_backtick(self, js_source: str) -> None:
        esc = _extract_esc(js_source)
        # The three characters the pentest exploited must now be escaped.
        assert r"replace(/\"/g,'&quot;')" in esc or '/"/g' in esc
        assert "&quot;" in esc
        assert "&#x27;" in esc  # single quote
        assert "&#x60;" in esc  # backtick

    def test_no_inline_onclick_jsstring_esc_sinks(self, js_source: str) -> None:
        """No inline onclick handler may embed an esc()'d value in a JS string —
        that context is unsafe (HTML entities decode back inside the string).
        All such sinks were converted to data-* + delegation."""
        assert re.search(r'''onclick="[A-Za-z_]+\('\$\{esc''', js_source) is None
        assert "window.lesDel" not in js_source
        assert "window.showSkill" not in js_source
        assert "window.mcpToggle" not in js_source

    def test_slot_name_sinks_still_escaped(self, js_source: str) -> None:
        """The slot-name attribute sinks (Source 1) still route through esc()."""
        assert 'title="${esc(s.key)}"' in js_source
        assert 'data-key="${esc(s.key)}"' in js_source

    def test_lesson_delete_uses_data_attribute(self, js_source: str) -> None:
        assert 'data-action="les-del"' in js_source
        assert 'data-rule="${esc(l.rule)}"' in js_source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestEscBehaviorViaNode:
    """Execute the real esc() against the pentest payloads and prove the
    attribute breakout is neutralized (no raw quote survives)."""

    def _run_esc(self, js_source: str, payload: str) -> str:
        esc = _extract_esc(js_source)
        program = esc + "\nprocess.stdout.write(esc(" + json.dumps(payload) + "));"
        out = subprocess.run(
            ["node", "-e", program], capture_output=True, text=True, timeout=15
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    def test_source1_slot_name_payload_neutralized(self, js_source: str) -> None:
        # The zero-interaction slot-name payload from the report.
        payload = 'xss-poc" onfocus="alert(document.domain)" autofocus tabindex="0" x="'
        escaped = self._run_esc(js_source, payload)
        # No raw double quote may survive — that is what breaks out of title="...".
        assert '"' not in escaped
        assert "&quot;" in escaped
        # Rendered into title="..." the browser sees no attribute boundary break.

    def test_source2_lesson_rule_payload_neutralized(self, js_source: str) -> None:
        payload = '" onfocus="alert(document.domain)" autofocus tabindex="0" x="'
        escaped = self._run_esc(js_source, payload)
        assert '"' not in escaped
        assert "&quot;" in escaped

    def test_single_quote_and_backtick_escaped(self, js_source: str) -> None:
        escaped = self._run_esc(js_source, "a'b`c")
        assert "'" not in escaped
        assert "`" not in escaped
        assert "&#x27;" in escaped and "&#x60;" in escaped

    def test_benign_text_roundtrips_readably(self, js_source: str) -> None:
        # Ampersand/angle handling unchanged for normal content.
        escaped = self._run_esc(js_source, "Tom & Jerry <ok>")
        assert escaped == "Tom &amp; Jerry &lt;ok&gt;"


def _extract_md(js: str) -> str:
    """Pull the one-line `function md(t){...return h}` definition."""
    m = re.search(r"function md\(t\)\{.*?return h\}", js)
    assert m, "md() definition not found"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestMdRendering:
    """md() must still render markdown AND stay XSS-safe after esc() started
    escaping backticks. Regression: md() escaped backticks before its own
    code-block regexes could match (AutoSDE finding on CR-286485989 rev1)."""

    def _run_md(self, js_source: str, payload: str) -> str:
        md = _extract_md(js_source)
        program = md + "\nprocess.stdout.write(md(" + json.dumps(payload) + "));"
        out = subprocess.run(
            ["node", "-e", program], capture_output=True, text=True, timeout=15
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    def test_fenced_code_block_renders(self, js_source: str) -> None:
        out = self._run_md(js_source, "```\nhello world\n```")
        assert "<pre><code>" in out and "hello world" in out and "</code></pre>" in out
        assert "```" not in out  # fence markers consumed, not left raw

    def test_inline_code_renders(self, js_source: str) -> None:
        out = self._run_md(js_source, "use `myVar` here")
        assert "<code>myVar</code>" in out

    def test_bold_and_italic_render(self, js_source: str) -> None:
        out = self._run_md(js_source, "**bold** and *em*")
        assert "<strong>bold</strong>" in out and "<em>em</em>" in out

    def test_html_injection_still_escaped(self, js_source: str) -> None:
        out = self._run_md(js_source, '<img src=x onerror="alert(1)">')
        assert "<img" not in out  # angle bracket escaped
        assert "&lt;img" in out
        assert '"' not in out  # double quote escaped

    def test_stray_backtick_escaped_in_output(self, js_source: str) -> None:
        # A single unmatched backtick must not survive raw in the output.
        out = self._run_md(js_source, "a ` b")
        assert "`" not in out
        assert "&#x60;" in out
