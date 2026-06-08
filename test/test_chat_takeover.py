"""Tests for POST /api/chat/takeover endpoint (Mesh-449)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_claw.dashboard.chat import api_chat_takeover
from kiro_claw.dashboard.state import DashboardState, _ChatSlot

VALID_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/takeover", api_chat_takeover)
    return app


def _mock_state() -> MagicMock:
    state = MagicMock(spec=DashboardState)
    state._slot_counter = 0
    state._slots = {}
    state.sessions = MagicMock()
    state.sessions._session_map = MagicMock()
    state.sessions._session_map.find_key_by_sid = MagicMock(return_value=None)
    state.sessions.get_provider = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()

    def _get_or_create(name):
        slot = _ChatSlot(name)
        state._slots[name] = slot
        return slot

    state.get_or_create_slot = MagicMock(side_effect=_get_or_create)
    return state


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    mock_sel.log_api_access = MagicMock()
    with patch("kiro_claw.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


# ---------------------------------------------------------------------------
# Validation (early returns)
# ---------------------------------------------------------------------------


class TestChatTakeoverValidation:
    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/takeover",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "invalid JSON" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_session_id_returns_400(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/takeover", json={"session_id": ""})
            assert resp.status == 400
            data = await resp.json()
            assert "required" in data["error"]

    @pytest.mark.asyncio
    async def test_no_session_id_key_returns_400(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/takeover", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_uuid_format_returns_400(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/takeover", json={"session_id": "not-a-valid-uuid"}
            )
            assert resp.status == 400
            data = await resp.json()
            assert "format" in data["error"]

    @pytest.mark.asyncio
    async def test_uuid_with_uppercase_rejected(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/takeover",
                json={"session_id": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_whitespace_only_session_id_returns_400(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/takeover", json={"session_id": "   "})
            assert resp.status == 400


# ---------------------------------------------------------------------------
# Not Found
# ---------------------------------------------------------------------------


class TestChatTakeoverNotFound:
    @pytest.mark.asyncio
    async def test_session_not_found_returns_404(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "some-project"
        projects_dir.mkdir(parents=True)

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 404
                data = await resp.json()
                assert "not found" in data["error"]
                # Must NOT leak filesystem path
                assert str(tmp_path) not in data["error"]

    @pytest.mark.asyncio
    async def test_no_projects_dir_returns_404(self, tmp_path, _patch_sel):
        state = _mock_state()
        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_non_directory_entries_skipped(self, tmp_path, _patch_sel):
        """Files (not dirs) in projects/ are skipped during search."""
        state = _mock_state()
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "not-a-directory").write_text("file")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_404_emits_sel_audit_not_found(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "some-project"
        projects_dir.mkdir(parents=True)

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 404

        _patch_sel.log_api_access.assert_called_once()
        call_kwargs = _patch_sel.log_api_access.call_args.kwargs
        assert call_kwargs["operation"] == "session_takeover"
        assert call_kwargs["outcome"] == "not_found"


# ---------------------------------------------------------------------------
# Conflict
# ---------------------------------------------------------------------------


class TestChatTakeoverConflict:
    @pytest.mark.asyncio
    async def test_session_already_active_returns_409(self, tmp_path, _patch_sel):
        state = _mock_state()
        state.sessions._session_map.find_key_by_sid.return_value = "existing-key"
        state.sessions.get_provider.return_value = MagicMock()

        projects_dir = tmp_path / "projects" / "some-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 409
                data = await resp.json()
                assert "already active" in data["error"]
                assert data["existing_key"] == "existing-key"

    @pytest.mark.asyncio
    async def test_409_emits_sel_audit_denied(self, tmp_path, _patch_sel):
        state = _mock_state()
        state.sessions._session_map.find_key_by_sid.return_value = "existing-key"
        state.sessions.get_provider.return_value = MagicMock()

        projects_dir = tmp_path / "projects" / "some-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                assert resp.status == 409

        _patch_sel.log_api_access.assert_called_once()
        call_kwargs = _patch_sel.log_api_access.call_args.kwargs
        assert call_kwargs["operation"] == "session_takeover"
        assert call_kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_stale_session_in_map_proceeds(self, tmp_path, _patch_sel):
        """Session in map but provider is None -> allowed."""
        state = _mock_state()
        state.sessions._session_map.find_key_by_sid.return_value = "stale-key"
        state.sessions.get_provider.return_value = None

        projects_dir = tmp_path / "projects" / "some-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/tmp"},
                )
                assert resp.status == 200


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestChatTakeoverHappyPath:
    @pytest.mark.asyncio
    async def test_valid_session_with_cwd_returns_ok(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-myproject"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/home/user/myproject"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert "takeover-" in data["slot"]

    @pytest.mark.asyncio
    async def test_session_map_set_called_with_raw_cwd(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/home/user/project"},
                )

        call_args = state.sessions._session_map.set.call_args
        assert call_args.args[1] == VALID_SID
        assert call_args.kwargs["provider"] == "claude_code"
        assert call_args.kwargs["cwd"] == "/home/user/project"

    @pytest.mark.asyncio
    async def test_linked_session_key_set_on_slot(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/tmp"},
                )
                data = await resp.json()
                slot_name = data["slot"]

        slot = state._slots[slot_name]
        assert slot.linked_session_key == f"dashboard:{slot_name}"

    @pytest.mark.asyncio
    async def test_slot_project_gets_redacted_cwd(self, tmp_path, _patch_sel):
        """slot.project passes through redaction; session_map gets raw cwd."""
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        cwd = "/home/user/project"
        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ), patch(
            "kiro_claw.dashboard.chat_handlers.redact_exfiltration_urls",
            side_effect=lambda s: ("[REDACTED]", True),
        ) as mock_redact:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": cwd},
                )
                data = await resp.json()
                slot_name = data["slot"]

        # session_map.set receives the ORIGINAL cwd (for filesystem resume)
        call_args = state.sessions._session_map.set.call_args
        assert call_args.kwargs["cwd"] == cwd
        # slot.project receives the redacted output
        slot = state._slots[slot_name]
        assert slot.project == "[REDACTED]"
        # redact was called with the cwd
        mock_redact.assert_any_call(cwd)

    @pytest.mark.asyncio
    async def test_push_slots_update_called(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/tmp"},
                )

        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_slot_counter_increments(self, tmp_path, _patch_sel):
        state = _mock_state()
        state._slot_counter = 5
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/tmp"},
                )
                data = await resp.json()

        assert state._slot_counter == 6
        assert "takeover-6-" in data["slot"]


# ---------------------------------------------------------------------------
# Title handling
# ---------------------------------------------------------------------------


class TestChatTakeoverTitle:
    @pytest.mark.asyncio
    async def test_explicit_title_used_over_cwd_derived(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={
                        "session_id": VALID_SID,
                        "cwd": "/home/user/project",
                        "title": "My Custom Title",
                    },
                )
                data = await resp.json()
                slot_name = data["slot"]

        slot = state._slots[slot_name]
        assert slot.title == "My Custom Title"
        assert slot._titled is True

    @pytest.mark.asyncio
    async def test_cwd_derived_title_when_no_explicit_title(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-myproject"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/home/user/myproject"},
                )
                data = await resp.json()
                slot_name = data["slot"]

        slot = state._slots[slot_name]
        assert "myproject" in slot.title
        assert slot._titled is True

    @pytest.mark.asyncio
    async def test_title_truncated_to_200_chars(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover",
                    json={
                        "session_id": VALID_SID,
                        "cwd": "/tmp",
                        "title": "A" * 300,
                    },
                )
                data = await resp.json()
                slot_name = data["slot"]

        slot = state._slots[slot_name]
        assert len(slot.title) <= 200

    @pytest.mark.asyncio
    async def test_no_title_no_cwd_derives_title_from_dir_name(
        self, tmp_path, _patch_sel
    ):
        """Without explicit title or cwd, title is derived from project dir name."""
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-derived"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )
                data = await resp.json()
                slot_name = data["slot"]

        slot = state._slots[slot_name]
        assert slot._titled is True
        assert "derived" in slot.title


# ---------------------------------------------------------------------------
# CWD derivation from project directory name
# ---------------------------------------------------------------------------


class TestChatTakeoverCwdDerivation:
    @pytest.mark.asyncio
    async def test_cwd_derived_from_project_dir_name(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "home-user-myproject"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat/takeover", json={"session_id": VALID_SID}
                )

        call_args = state.sessions._session_map.set.call_args
        derived_cwd = call_args.kwargs["cwd"]
        assert derived_cwd == "/home/user/myproject"

    @pytest.mark.asyncio
    async def test_explicit_cwd_overrides_derivation(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "home-user-myproject"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/actual/real/path"},
                )

        call_args = state.sessions._session_map.set.call_args
        assert call_args.kwargs["cwd"] == "/actual/real/path"

    @pytest.mark.asyncio
    async def test_sel_audit_log_called(self, tmp_path, _patch_sel):
        state = _mock_state()
        projects_dir = tmp_path / "projects" / "local-home-user-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / f"{VALID_SID}.jsonl").write_text("{}\n")

        with patch(
            "kiro_claw.dashboard.chat_handlers.cc_config_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post(
                    "/api/chat/takeover",
                    json={"session_id": VALID_SID, "cwd": "/tmp"},
                )

        _patch_sel.log_api_access.assert_called_once()
        call_kwargs = _patch_sel.log_api_access.call_args.kwargs
        assert call_kwargs["operation"] == "session_takeover"
        assert call_kwargs["outcome"] == "ok"
