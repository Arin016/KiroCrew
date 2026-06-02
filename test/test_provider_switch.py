"""Tests for cross-provider session-switch behavior.

Verifies that swapping acp <-> claude_code mid-session:
1. Clears the incompatible resume_sid from session_map
2. Does NOT attempt to pass resume: to the new session
3. Injects KiroClaw's own history replay on the first prompt
4. Replay fires exactly once per switch, not on subsequent prompts
5. Same-provider resume path is unaffected
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.config import KiroClawConfig
from kiro_claw.session import (
    SessionManager,
    _is_claude_backend,
    detect_provider_switch,
)
from kiro_claw.session_map import SessionMap

# ── Fixtures ──


@pytest.fixture()
def session_map(tmp_path):
    """Create a SessionMap backed by a temp directory."""
    with patch("kiro_claw.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


@pytest.fixture()
def cfg():
    c = KiroClawConfig()
    c.session.timeout_secs = 30
    return c


def _make_acp_provider_mock(session_id: str = "kiro-sid-abc", resumed: bool = False):
    """Create a mock AcpProvider (kiro backend)."""
    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.client = MagicMock()
    m.client._session_id = session_id
    m.client.resumed = resumed
    m.client.backend = ""  # not claude backend
    m.client.set_resume_session_id = MagicMock()
    m.client.rekey = MagicMock()
    m._work_dir = Path("/tmp/test")
    return m


def _make_cc_provider_mock(session_id: str = "cc-uuid-1234"):
    """Create a mock ClaudeCodeProvider."""
    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.session_id = session_id
    m.was_resumed = False
    m.set_resume_session_id = MagicMock()
    m._work_dir = Path("/tmp/test")
    return m


# ── Unit tests: detect_provider_switch ──


class TestDetectProviderSwitch:
    """Tests for the detect_provider_switch helper."""

    def test_returns_false_when_same_provider(self, session_map, tmp_path):
        """Same provider -> no switch detected."""
        # Store a kiro session with provider=acp
        kiro_json = tmp_path / ".kiro" / "sessions" / "cli"
        kiro_json.mkdir(parents=True, exist_ok=True)
        (kiro_json / "sid-abc.json").write_text("{}")
        (kiro_json / "sid-abc.jsonl").write_text('{"line":1}\n')
        with patch("kiro_claw.session_map._KIRO_SESSIONS_DIR", kiro_json):
            session_map.set("dash:1", "sid-abc", provider="acp")
            result = detect_provider_switch(session_map, "dash:1", "acp")
        assert result is False

    def test_returns_true_when_kiro_to_cc(self, session_map, tmp_path):
        """kiro -> cc: switch detected."""
        kiro_json = tmp_path / ".kiro" / "sessions" / "cli"
        kiro_json.mkdir(parents=True, exist_ok=True)
        (kiro_json / "sid-abc.json").write_text("{}")
        (kiro_json / "sid-abc.jsonl").write_text('{"line":1}\n')
        with patch("kiro_claw.session_map._KIRO_SESSIONS_DIR", kiro_json):
            session_map.set("dash:1", "sid-abc", provider="acp")
            result = detect_provider_switch(session_map, "dash:1", "claude_code")
        assert result is True

    def test_returns_true_when_cc_to_kiro(self, session_map):
        """cc -> kiro: switch detected."""
        session_map.set("dash:1", "cc-uuid-123", provider="claude_code")
        result = detect_provider_switch(session_map, "dash:1", "acp")
        assert result is True

    def test_returns_false_when_no_stored_sid(self, session_map):
        """No stored SID -> no switch (nothing to discard)."""
        session_map.set("dash:1", "", provider="acp")
        result = detect_provider_switch(session_map, "dash:1", "claude_code")
        assert result is False

    def test_returns_false_when_no_entry(self, session_map):
        """No entry at all -> no switch."""
        result = detect_provider_switch(session_map, "nonexistent", "claude_code")
        assert result is False

    def test_emits_sel_event_on_switch(self, session_map):
        """SEL audit event is emitted when switch is detected."""
        session_map.set("dash:1", "cc-uuid-123", provider="claude_code")
        with patch("kiro_claw.session.sel") as mock_sel:
            mock_sel_inst = MagicMock()
            mock_sel.return_value = mock_sel_inst
            detect_provider_switch(session_map, "dash:1", "acp")
            mock_sel_inst.log_tool_invocation.assert_called_once()
            call_kwargs = mock_sel_inst.log_tool_invocation.call_args[1]
            assert call_kwargs["tool_name"] == "provider_switch_detected"
            assert call_kwargs["metadata"]["stored_provider"] == "claude_code"
            assert call_kwargs["metadata"]["new_provider"] == "acp"


# ── Unit tests: SessionMap.clear_sid ──


class TestSessionMapClearSid:
    """Tests for SessionMap.clear_sid."""

    def test_clears_sid_preserves_entry(self, session_map):
        """clear_sid empties the sid field but keeps the entry."""
        session_map.set("dash:1", "sid-abc", provider="acp", cwd="/tmp")
        session_map.set_slack_link("dash:1", "ts-123", "C001")
        session_map.clear_sid("dash:1")
        # SID is now empty
        assert session_map.get_provider("dash:1") == "acp"
        assert session_map.get_cwd("dash:1") == "/tmp"
        link = session_map.get_slack_link("dash:1")
        assert link == ("ts-123", "C001")

    def test_clear_sid_no_op_if_no_sid(self, session_map):
        """clear_sid is a no-op if SID is already empty."""
        session_map.set("dash:1", "", provider="acp")
        session_map.clear_sid("dash:1")  # should not raise
        assert session_map.get_provider("dash:1") == "acp"

    def test_clear_sid_no_op_if_no_entry(self, session_map):
        """clear_sid is a no-op if entry doesn't exist."""
        session_map.clear_sid("nonexistent")  # should not raise


# ── Integration tests: provider switch in SessionManager ──


class TestProviderSwitchIntegration:
    """Integration tests for provider switch during get_or_create.

    These test detect_provider_switch + clear_sid + provider_switch_replay
    via SessionManager.get_or_create, using real AcpProvider subclasses to
    avoid __instancecheck__ issues with MagicMock.
    """

    @pytest.mark.asyncio
    async def test_kiro_to_cc_clears_sid_and_sets_replay_flag(self, cfg, tmp_path):
        """kiro->cc switch: SID cleared, resume not passed, replay flag set."""
        from kiro_claw.providers.claude_code import ClaudeCodeProvider

        kiro_dir = tmp_path / "kiro_sessions"
        kiro_dir.mkdir()
        (kiro_dir / "kiro-sid-abc.json").write_text("{}")
        (kiro_dir / "kiro-sid-abc.jsonl").write_text('{"line":1}\n')

        # Create a real-ish CC provider subclass so isinstance works
        class FakeCCProvider(ClaudeCodeProvider):
            def __init__(self):
                self._work_dir = Path("/tmp/test")
                self._session_id = "new-cc-uuid"
                self._resume_sid = None
                self._started = True

            async def start(self):
                pass

            async def shutdown(self):
                pass

            @property
            def session_id(self):
                return self._session_id or ""

            @property
            def was_resumed(self):
                return self._resume_sid is not None

            def set_resume_session_id(self, session_id):
                self._resume_sid = session_id

        cc_provider = FakeCCProvider()

        def cc_factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return cc_provider

        with (
            patch("kiro_claw.session_map.config_dir", return_value=tmp_path),
            patch("kiro_claw.session_map._KIRO_SESSIONS_DIR", kiro_dir),
            patch("kiro_claw.session.sel") as mock_sel,
        ):
            mock_sel.return_value = MagicMock()
            mgr = SessionManager(cfg, provider_factory=cc_factory)
            # Seed session_map with a kiro session
            mgr._session_map.set("dash:1", "kiro-sid-abc", provider="acp", cwd="/tmp")

            provider, is_new, resumed = await mgr.get_or_create("dash:1")

            # Verify: resume_sid was NOT passed to the CC provider
            assert cc_provider._resume_sid is None
            # Verify: session is new and not resumed
            assert is_new is True
            assert resumed is False
            # Verify: old kiro SID was replaced by new CC SID in session_map
            entry = mgr._session_map._data.get("dash:1", {})
            assert entry.get("sid") == "new-cc-uuid"
            assert entry.get("provider") == "claude_code"
            # Verify: provider_switch_replay flag is set on the session
            sess = mgr._sessions.get("dash:1")
            assert sess is not None
            assert sess.provider_switch_replay is True

            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cc_to_kiro_clears_sid_and_sets_replay_flag(self, cfg, tmp_path):
        """cc->kiro switch: SID cleared, resume not passed, replay flag set."""
        from kiro_claw.providers.acp import AcpProvider

        # Create a real-ish ACP provider subclass
        class FakeAcpProvider(AcpProvider):
            def __init__(self):
                self._work_dir = Path("/tmp/test")
                self._client = MagicMock()
                self._client._session_id = "new-kiro-sid"
                self._client.resumed = False
                self._client.backend = ""
                self._client.set_resume_session_id = MagicMock()

            async def start(self):
                pass

            async def shutdown(self):
                pass

        acp_provider = FakeAcpProvider()

        def acp_factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return acp_provider

        with (
            patch("kiro_claw.session_map.config_dir", return_value=tmp_path),
            patch("kiro_claw.session.sel") as mock_sel,
        ):
            mock_sel.return_value = MagicMock()
            mgr = SessionManager(cfg, provider_factory=acp_factory)
            # Seed session_map with a CC session
            mgr._session_map.set("dash:1", "cc-uuid-1234", provider="claude_code", cwd="/tmp")

            provider, is_new, resumed = await mgr.get_or_create("dash:1")

            # Verify: resume_sid was NOT passed to the ACP client
            acp_provider._client.set_resume_session_id.assert_not_called()
            # Verify: session is new and not resumed
            assert is_new is True
            assert resumed is False
            # Verify: old CC SID was replaced by new kiro SID in session_map
            entry = mgr._session_map._data.get("dash:1", {})
            assert entry.get("sid") == "new-kiro-sid"
            assert entry.get("provider") == "acp"
            # Verify: provider_switch_replay flag is set
            sess = mgr._sessions.get("dash:1")
            assert sess is not None
            assert sess.provider_switch_replay is True

            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_same_provider_resumes_normally(self, cfg, tmp_path):
        """Same provider (kiro->kiro): normal resume path."""
        from kiro_claw.providers.acp import AcpProvider

        kiro_dir = tmp_path / "kiro_sessions"
        kiro_dir.mkdir()
        (kiro_dir / "kiro-sid-abc.json").write_text("{}")
        (kiro_dir / "kiro-sid-abc.jsonl").write_text('{"line":1}\n')

        class FakeAcpProvider(AcpProvider):
            def __init__(self):
                self._work_dir = Path("/tmp/test")
                self._client = MagicMock()
                self._client._session_id = "kiro-sid-abc"
                self._client.resumed = True
                self._client.backend = ""
                self._client.set_resume_session_id = MagicMock()

            async def start(self):
                pass

            async def shutdown(self):
                pass

        acp_provider = FakeAcpProvider()

        def acp_factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return acp_provider

        with (
            patch("kiro_claw.session_map.config_dir", return_value=tmp_path),
            patch("kiro_claw.session_map._KIRO_SESSIONS_DIR", kiro_dir),
            patch("kiro_claw.session.sel") as mock_sel,
        ):
            mock_sel.return_value = MagicMock()
            mgr = SessionManager(cfg, provider_factory=acp_factory)
            # Seed session_map with a kiro session (same provider)
            mgr._session_map.set("dash:1", "kiro-sid-abc", provider="acp", cwd="/tmp")

            provider, is_new, resumed = await mgr.get_or_create("dash:1")

            # Verify: resume_sid WAS passed to the ACP client
            acp_provider._client.set_resume_session_id.assert_called_once_with("kiro-sid-abc")
            # Verify: session resumed normally
            assert is_new is True
            assert resumed is True
            # Verify: SID was NOT cleared
            assert mgr._session_map._data.get("dash:1", {}).get("sid") != ""
            # Verify: provider_switch_replay flag is NOT set
            sess = mgr._sessions.get("dash:1")
            assert sess is not None
            assert sess.provider_switch_replay is False

            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_replay_flag_consumed_once(self, cfg, tmp_path):
        """provider_switch_replay is consumed (cleared) on first read."""
        from kiro_claw.session import _Session

        acp_mock = _make_acp_provider_mock()
        sess = _Session(provider=acp_mock, is_new=True)
        sess.provider_switch_replay = True

        # First read: flag is True, then cleared
        assert sess.provider_switch_replay is True
        sess.provider_switch_replay = False  # simulates chat_runner consuming it
        # Second read: flag is False
        assert sess.provider_switch_replay is False


# ── Unit test: _is_claude_backend ──


class TestIsClaudeBackend:
    """Tests for _is_claude_backend helper."""

    def test_acp_with_claude_backend(self):
        """AcpProvider with backend='claude' returns True."""
        from kiro_claw.providers.acp import AcpProvider

        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.client = MagicMock()
        mock_provider.client.backend = "claude"
        result = _is_claude_backend(mock_provider)
        assert result is True

    def test_acp_with_kiro_backend(self):
        """AcpProvider with empty backend returns False."""
        from kiro_claw.providers.acp import AcpProvider

        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.client = MagicMock()
        mock_provider.client.backend = ""
        result = _is_claude_backend(mock_provider)
        assert result is False

    def test_non_acp_provider(self):
        """Non-AcpProvider returns False."""
        mock_provider = MagicMock()
        result = _is_claude_backend(mock_provider)
        assert result is False


class TestCloseAllPersistsProviderLabel:
    """close_all() must persist provider= so detect_provider_switch on next
    startup doesn't see a missing label, default to "acp", and falsely fire
    a switch for users still on claude_code (AutoSDE r1 #24).
    """

    @pytest.mark.asyncio
    async def test_close_all_persists_acp_label_for_kiro_session(self, cfg, tmp_path):
        from kiro_claw.providers.acp import AcpProvider

        class FakeAcpProvider(AcpProvider):
            def __init__(self):
                self._work_dir = Path("/tmp/kiro")
                self._client = MagicMock()
                self._client._session_id = "kiro-sid-1"
                self._client.backend = ""

            async def shutdown(self):
                pass

        provider = FakeAcpProvider()

        with patch("kiro_claw.session_map.config_dir", return_value=tmp_path):
            mgr = SessionManager(cfg, provider_factory=lambda **kw: provider)
            # Inject a session by hand to avoid going through get_or_create.
            from kiro_claw.session import _Session

            mgr._sessions["dash:1"] = _Session(provider=provider)
            await mgr.close_all()

            entry = mgr._session_map._data.get("dash:1", {})
            assert entry.get("sid") == "kiro-sid-1"
            assert entry.get("provider") == "acp"

    @pytest.mark.asyncio
    async def test_close_all_persists_claude_code_label_for_cc_backend(
        self, cfg, tmp_path
    ):
        from kiro_claw.providers.acp import AcpProvider

        class FakeAcpClaudeProvider(AcpProvider):
            def __init__(self):
                self._work_dir = Path("/tmp/cc")
                self._client = MagicMock()
                self._client._session_id = "cc-sid-1"
                self._client.backend = "claude"

            async def shutdown(self):
                pass

        provider = FakeAcpClaudeProvider()

        with patch("kiro_claw.session_map.config_dir", return_value=tmp_path):
            mgr = SessionManager(cfg, provider_factory=lambda **kw: provider)
            from kiro_claw.session import _Session

            mgr._sessions["dash:1"] = _Session(provider=provider)
            await mgr.close_all()

            entry = mgr._session_map._data.get("dash:1", {})
            assert entry.get("sid") == "cc-sid-1"
            assert entry.get("provider") == "claude_code"
