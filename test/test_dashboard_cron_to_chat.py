"""Tests for cron dashboard chat threading (inject_cron_result_to_dashboard)."""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_claw.dashboard.cron_inject import inject_cron_result_to_dashboard


def _make_state(history_messages=None):
    """Create a mock DashboardState with conversation_log."""
    state = MagicMock()
    slots = {}

    def get_or_create_slot(name=None, agent=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True):
                slot.messages.append({"role": role, "content": content, "cls": cls})

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _make_job(job_id="abc123", name="test-cron", last_result="Hello world"):
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.last_result = last_result
    job.agent_id = ""
    return job


class TestInjectCronResultToDashboard:
    def test_sets_linked_session_key(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"

    def test_sets_title_from_job_name(self):
        state = _make_state()
        job = _make_job(name="daily-standup")
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert "daily-standup" in slot.title

    def test_hydrates_history_on_first_link(self):
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # History (2) + result (1) = 3 messages
        assert len(slot.messages) == 3
        assert slot.messages[0]["content"] == "msg1"
        assert slot.messages[1]["content"] == "msg2"

    def test_hydrates_max_50_messages(self):
        history = [{"role": "assistant", "content": f"msg{i}"} for i in range(100)]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # 50 from history + 1 result = 51
        assert len(slot.messages) == 51

    def test_does_not_rehydrate_on_second_call(self):
        history = [{"role": "assistant", "content": "old"}]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result1")
        inject_cron_result_to_dashboard(state, job, "result2")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # history(1) + result1(1) + result2(1) = 3 (no re-hydration)
        assert len(slot.messages) == 3

    def test_dedup_prevents_duplicate_result(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "same result")
        inject_cron_result_to_dashboard(state, job, "same result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # Only 1 message — dedup prevents second identical inject
        assert len(slot.messages) == 1

    def test_empty_result_creates_slot_without_message(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"
        assert len(slot.messages) == 0

    def test_pushes_slots_update(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        state.push_slots_update.assert_called_once()


class TestHydrateSlotFromHistory:
    """Tests for hydrate_slot_from_history (accepts pre-loaded messages)."""

    def test_hydrates_messages_into_slot(self):
        from kiro_claw.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "hello"
        assert slot.messages[1]["content"] == "world"

    def test_empty_history_produces_no_messages(self):
        from kiro_claw.dashboard.cron_inject import hydrate_slot_from_history

        state = _make_state(history_messages=[])
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, [])
        assert len(slot.messages) == 0

    def test_skips_messages_with_empty_content(self):
        from kiro_claw.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "real message"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "real message"

    def test_assigns_user_role_class(self):
        from kiro_claw.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "assistant msg"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert slot.messages[0]["cls"] == "msg msg-u"
        assert slot.messages[1]["cls"] == "msg msg-a"


class TestHasSlot:
    """Tests for DashboardState.has_slot method."""

    def test_returns_true_when_slot_exists(self):
        from kiro_claw.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {"cron-abc": MagicMock()}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("cron-abc") is True

    def test_returns_false_when_slot_missing(self):
        from kiro_claw.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("nonexistent") is False
