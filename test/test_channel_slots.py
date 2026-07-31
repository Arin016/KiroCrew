"""Tests for surfacing channel-originated sessions as dashboard chat slots.

Covers the eligibility rules (recency window, closed, ephemeral memory modes,
pin/folder exemption), the slot-creation/binding behaviour, and the async
reconcile pass.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.messaging.link import (
    channel_namespace_of,
    is_channel_session_key,
)

NOW = time.time()


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    """DashboardState with mocked services and a real (empty) ConversationLog."""
    return _make_state(tmp_path)


def _session(key: str, *, modified: float | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": "", "modified": modified if modified is not None else NOW, **extra}


class TestChannelKeyPredicates:
    def test_recognizes_every_channel_namespace(self) -> None:
        for key in (
            "slack:1785370133.085469",
            "discord:kirocrew:direct:U1",
            "telegram:kirocrew:direct:U1",
            "whatsapp:kirocrew:direct:U1",
            "webex:kirocrew:direct:U1",
            "wecom:kirocrew:direct:U1",
            "teams:kirocrew:direct:U1",
            "weixin:kirocrew:direct:U1",
            "unified:kirocrew",
        ):
            assert is_channel_session_key(key), key

    def test_recognizes_the_persisted_filename_stem_form(self) -> None:
        """list_sessions() reports the stem, where _safe_key folded ':' -> '_'.

        Missing this is why the reconciler saw zero channel sessions in a real
        instance while every synthetic ``slack:`` fixture passed.
        """
        assert is_channel_session_key("slack_1785370133.085469")
        assert is_channel_session_key("discord_kirocrew_direct_U1")
        assert is_channel_session_key("unified_kirocrew")
        assert channel_namespace_of("slack_1.1") == "slack"
        assert channel_slots.channel_label("slack_1.1") == "Slack"

    def test_rejects_non_channel_namespaces(self) -> None:
        for key in (
            "dashboard:chat-1-123",
            "cron:abc123",
            "hook:default:1",
            "subagent:xyz",
            "channel:general",
            "dashboard_chat-1-123",
            "cron_abc123",
            "",
            "slackish:1.2",
            "slackish_1.2",
        ):
            assert not is_channel_session_key(key), key

    def test_namespace_of(self) -> None:
        assert channel_namespace_of("slack:1.2") == "slack"
        assert channel_namespace_of("teams:a:direct:b") == "teams"
        assert channel_namespace_of("cron:x") == ""

    def test_labels(self) -> None:
        assert channel_slots.channel_label("slack:1.2") == "Slack"
        assert channel_slots.channel_label("wecom:a:direct:b") == "WeCom"
        assert channel_slots.channel_label("dashboard:chat-1") == "Channel"


class TestEligibility:
    def test_dashboard_and_cron_sessions_are_never_eligible(self) -> None:
        sessions = [_session("dashboard:chat-1-1"), _session("cron:abc")]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert out == []

    def test_recent_channel_session_is_eligible(self) -> None:
        sessions = [_session("slack:1785370133.085469")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={}, cutoff=NOW - 1800
        )
        assert [s["key"] for s in out] == ["slack:1785370133.085469"]

    def test_stale_channel_session_is_filtered(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={}, cutoff=NOW - 1800
        )
        assert out == []

    def test_zero_window_disables_recency_filter(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 999999)]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert len(out) == 1

    def test_pinned_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"pinned": True}}, cutoff=NOW - 1800
        )
        assert len(out) == 1

    def test_foldered_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"folder_id": "f1"}}, cutoff=NOW - 1800
        )
        assert len(out) == 1

    def test_closed_on_the_channel_key_is_never_resurfaced(self) -> None:
        """Closing the tab must stick — otherwise the next pass undoes it."""
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"closed": True}}, cutoff=NOW - 1800
        )
        assert out == []

    def test_closed_on_the_slot_key_is_never_resurfaced(self) -> None:
        """Closing the TAB writes `closed` to the slot key, not the channel key.

        Reading only the channel key is why a closed tab would reopen on the
        next 30s pass — the exact defect this asserts against.
        """
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"closed": True}},
            cutoff=NOW - 1800,
        )
        assert out == []

    def test_pinned_on_the_slot_key_survives_the_window(self) -> None:
        sessions = [_session("slack:1.1", modified=NOW - 7200)]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"pinned": True}},
            cutoff=NOW - 1800,
        )
        assert len(out) == 1

    def test_ephemeral_on_the_slot_key_is_skipped(self) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"dashboard:slack_1.1": {"memory_mode": "incognito"}},
            cutoff=None,
        )
        assert out == []

    def test_slot_history_key_derivation(self) -> None:
        assert channel_slots.channel_slot_name("slack:1.1") == "slack_1.1"
        assert channel_slots.slot_history_key("slack:1.1") == "dashboard:slack_1.1"

    def test_closed_beats_pinned(self) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions,
            metadata={"slack:1.1": {"closed": True, "pinned": True}},
            cutoff=None,
        )
        assert out == []

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "INCOGNITO"])
    def test_ephemeral_threads_are_skipped(self, mode: str) -> None:
        sessions = [_session("slack:1.1")]
        out = channel_slots.eligible_channel_sessions(
            sessions, metadata={"slack:1.1": {"memory_mode": mode}}, cutoff=None
        )
        assert out == []

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_ephemeral_detected_from_listing_too(self, mode: str) -> None:
        """The listing carries memory_mode as well; either source disqualifies."""
        sessions = [_session("slack:1.1", memory_mode=mode)]
        out = channel_slots.eligible_channel_sessions(sessions, metadata={}, cutoff=None)
        assert out == []


class TestSurfaceChannelSession:
    def test_creates_slot_seeded_with_the_conversation(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1785370133.085469", title="Ship the thing"),
            {},
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )
        assert slot is not None
        # Deterministic name = the session key folded to the filename charset.
        assert slot.key == "slack_1785370133.085469"
        assert slot.title == "Ship the thing"
        assert [m["content"] for m in slot.messages] == ["hi", "hello"]

    def test_binds_the_channel_key_as_the_shared_session(self, dashboard_state: Any) -> None:
        """One session, two surfaces — the transcript key is the channel's.

        ``shared_session_key`` governs read, write AND run together, so the tab
        and the channel thread append to a single file. It is deliberately NOT
        ``linked_session_key``, which repoints only the run path and is owned by
        cron/workflow display-mirror slots that still write their own transcript.
        """
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, []
        )
        assert slot is not None
        assert slot.shared_session_key == "slack:1.1"
        assert slot.linked_session_key == ""
        from kiro_crew.dashboard.chat_utils import slot_session_key, slot_transcript_key

        assert slot_transcript_key(slot) == "slack:1.1"
        assert slot_session_key(slot) == "slack:1.1"

    def test_untitled_session_falls_back_to_the_channel_label(
        self, dashboard_state: Any
    ) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("teams:a:direct:b"), {}, []
        )
        assert slot is not None
        assert slot.title == "Teams"
        assert slot._titled is False

    def test_is_idempotent(self, dashboard_state: Any) -> None:
        info = _session("slack:1.1", title="T")
        first = channel_slots.surface_channel_session(dashboard_state, info, {}, [])
        second = channel_slots.surface_channel_session(dashboard_state, info, {}, [])
        assert first is not None
        assert second is None, "second pass must be a no-op"
        assert len(dashboard_state._slots) == 1

    def test_leaves_an_existing_slot_untouched(self, dashboard_state: Any) -> None:
        """A slot restored from open_slots.json already owns its transcript —
        re-hydrating would duplicate messages on top of it."""
        existing = dashboard_state.get_or_create_slot(name="slack_1.1")
        existing.append("user", "already here", "msg msg-u", broadcast=False)
        existing.drain()
        assert (
            channel_slots.surface_channel_session(
                dashboard_state,
                _session("slack:1.1"),
                {},
                [{"role": "user", "content": "should not be duplicated"}],
            )
            is None
        )
        assert [m["content"] for m in existing.messages] == ["already here"]

    def test_ignores_non_channel_keys(self, dashboard_state: Any) -> None:
        assert (
            channel_slots.surface_channel_session(
                dashboard_state, _session("dashboard:chat-1-1"), {}, []
            )
            is None
        )
        assert dashboard_state._slots == {}

    def test_applies_metadata(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1.1"),
            {
                "agent": "kirocrew",
                "model": "claude-opus-5",
                "workspace": "default",
                "project": "p1",
                "folder_id": "f1",
                "pinned": True,
                "created_at": "2026-07-30T00:00:00Z",
            },
            [],
        )
        assert slot is not None
        assert slot.agent == "kirocrew"
        assert slot.model == "claude-opus-5"
        assert slot.project == "p1"
        assert slot.folder_id == "f1"
        assert slot.pinned is True
        assert slot.created_at == "2026-07-30T00:00:00Z"

    def test_redacts_titles_and_messages(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state,
            _session("slack:1.1", title="key AKIAIOSFODNN7EXAMPLE"),
            {},
            [{"role": "assistant", "content": "token AKIAIOSFODNN7EXAMPLE"}],
        )
        assert slot is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.title
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[0]["content"]


class _FakeLog:
    def __init__(self, sessions: list[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> None:
        self._sessions = sessions
        self._meta = meta
        self.message_reads: list[str] = []
        #: key -> transcript. Unset keys read empty, so the reconciler's
        #: slot-key-then-channel-key preference order is observable.
        self.transcripts: dict[str, list[dict[str, Any]]] = {
            s["key"]: [{"role": "user", "content": f"msg for {s['key']}"}] for s in sessions
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)

    def get_metadata(self, key: str) -> dict[str, Any]:
        return dict(self._meta.get(key, {}))

    def read_messages(self, key: str) -> list[dict[str, Any]]:
        self.message_reads.append(key)
        return list(self.transcripts.get(key, []))


class TestReconcilePass:
    def test_surfaces_eligible_and_pushes_once(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [
                _session("slack:1.1"),
                _session("discord:a:direct:b"),
                _session("dashboard:chat-1-1"),
                _session("slack:2.2", modified=NOW - 99999),
            ],
            {},
        )
        pushes: list[int] = []
        dashboard_state.push_slots_update = lambda: pushes.append(1)  # type: ignore[method-assign]

        n = asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30))
        assert n == 2
        assert set(dashboard_state._slots) == {"slack_1.1", "discord_a_direct_b"}
        # get_or_create_slot broadcasts on create; the pass adds a final push so
        # a rebind-only pass (no create) still reaches connected clients.
        assert pushes, "the pass must broadcast the new slots"

    def test_a_closed_tab_is_not_reopened_by_the_next_pass(
        self, dashboard_state: Any
    ) -> None:
        """End-to-end guard for the reopen defect: `closed` lives on the SLOT key."""
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack:1.1")], {"dashboard:slack_1.1": {"closed": True}}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert dashboard_state._slots == {}

    def test_seeds_from_both_transcripts_merged(self, dashboard_state: Any) -> None:
        """Neither side is a superset: the slot holds dashboard replies, the channel
        holds anything said on the channel after surfacing. Re-surfacing needs both."""
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "on slack first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "on slack later", "ts": "2026-07-30T00:03:00Z"},
        ]
        log.transcripts["dashboard:slack_1.1"] = [
            {"role": "user", "content": "on slack first", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "from the dashboard", "ts": "2026-07-30T00:01:00Z"},
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert [m["content"] for m in dashboard_state._slots["slack_1.1"].messages] == [
            "on slack first",
            "from the dashboard",
            "on slack later",
        ]


@contextmanager
def _tz(name: str) -> Iterator[None]:
    """Run the block with the process's local timezone set to *name*.

    ``datetime.astimezone()`` on a naive value reads the process zone, which is
    precisely what makes an un-suffixed channel timestamp ambiguous.
    """
    prev = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


#: ``time.tzset`` is Unix-only, and CI also runs the backend suite on Windows.
_needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is Unix-only")


class TestMergeTranscriptTimezones:
    """The two sides of a merge do not agree on timezone.

    The dashboard writes ISO-8601 UTC; a channel turn can arrive as a naive
    local-time string. Comparing those as text interleaves them wrongly by the
    host's UTC offset, so ordering has to run on absolute instants.
    """

    @_needs_tzset
    def test_naive_local_and_utc_interleave_chronologically(self) -> None:
        # 12:30 in a UTC-07:00 zone is 19:30Z — later than 19:00Z, even though
        # "2026-07-30T12:30:00" sorts BEFORE "2026-07-30T19:00:00+00:00".
        slot = [{"role": "assistant", "content": "utc reply", "ts": "2026-07-30T19:00:00+00:00"}]
        chan = [{"role": "user", "content": "naive local", "ts": "2026-07-30T12:30:00"}]
        with _tz("US/Pacific"):
            out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["utc reply", "naive local"]

    def test_lexicographic_order_would_have_been_wrong(self) -> None:
        """Pin the regression: text order disagrees with chronological order."""
        naive, utc = "2026-07-30T12:30:00", "2026-07-30T19:00:00+00:00"
        assert naive < utc, "if this stops holding the test above proves nothing"

    def test_zulu_and_offset_spellings_of_one_instant_are_ordered_together(self) -> None:
        slot = [{"role": "user", "content": "second", "ts": "2026-07-30T00:00:01+00:00"}]
        chan = [{"role": "user", "content": "first", "ts": "2026-07-30T00:00:00Z"}]
        out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["first", "second"]

    def test_unparseable_timestamps_sort_before_untimestamped_and_keep_order(self) -> None:
        chan = [
            {"role": "user", "content": "none"},
            {"role": "user", "content": "junk-b", "ts": "not-a-date-b"},
            {"role": "user", "content": "real", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "junk-a", "ts": "not-a-date-a"},
        ]
        out = channel_slots.merge_transcripts([], chan)
        assert [m["content"] for m in out] == ["real", "junk-a", "junk-b", "none"]


class TestFrozenPrefixAccounting:
    """A seeded slot must declare how much of its history file it did NOT load.

    ``_save_slot_to_history`` writes ``frozen prefix + serialize(window)``. Left
    at 0, the first save re-emits the omitted older lines AFTER the newer ones.
    """

    def _slot_msgs(self, n: int) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"m{i}", "ts": f"2026-07-30T00:{i:02d}:00Z"}
            for i in range(n)
        ]

    def test_short_transcript_has_no_prefix(self) -> None:
        msgs = self._slot_msgs(3)
        assert channel_slots.frozen_prefix_len(msgs, channel_slots.hydrate_window(msgs)) == 0

    def test_counts_only_the_lines_the_window_omits(self) -> None:
        msgs = self._slot_msgs(channel_slots._HYDRATE_LIMIT + 12)
        window = channel_slots.hydrate_window(msgs)
        assert len(window) == channel_slots._HYDRATE_LIMIT
        assert channel_slots.frozen_prefix_len(msgs, window) == 12

    def test_channel_only_window_freezes_the_whole_file(self) -> None:
        """No slot line survived into the window → every one of them is prefix."""
        msgs = self._slot_msgs(4)
        assert channel_slots.frozen_prefix_len(msgs, [{"role": "user", "content": "x"}]) == 4

    def test_empty_file_has_no_prefix(self) -> None:
        assert channel_slots.frozen_prefix_len([], [{"role": "user", "content": "x"}]) == 0

    def test_seeded_slot_counts_the_prefix_against_the_shared_channel_file(
        self, dashboard_state: Any
    ) -> None:
        """End-to-end: a long legacy slot file plus the live channel transcript.

        The slot now READS AND WRITES the channel key, so both counters describe
        the CHANNEL file. Crediting legacy dashboard-keyed lines here would let a
        trim fold lines that are not in the shared file into its frozen prefix,
        and the next save would rewrite that file short.
        """
        over = 7
        legacy_msgs = self._slot_msgs(channel_slots._HYDRATE_LIMIT + over)
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["dashboard:slack_1.1"] = legacy_msgs
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "said on slack after", "ts": "2026-07-30T23:00:00Z"}
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1.1"]
        # The lone channel line is the newest turn, so it survives into the
        # window — nothing in the shared file is frozen.
        assert slot._disk_older_count == 0
        assert slot._disk_window_len == 1
        assert slot._disk_older_count + slot._disk_window_len == len(log.transcripts["slack:1.1"])
        # Seeding still folds the legacy turns in, so migrating loses nothing.
        assert len(slot.messages) == channel_slots._HYDRATE_LIMIT
        assert slot.messages[-1]["content"] == "said on slack after"
        assert slot.shared_session_key == "slack:1.1"

    def test_empty_content_lines_are_not_seeded_but_still_counted(self) -> None:
        """A blank on-disk line is never appended, so it belongs to the prefix."""
        msgs = [
            {"role": "user", "content": "", "ts": "2026-07-30T00:00:00Z"},
            {"role": "user", "content": "kept", "ts": "2026-07-30T00:01:00Z"},
        ]
        window = channel_slots.hydrate_window(msgs)
        assert [m["content"] for m in window] == ["kept"]
        assert channel_slots.frozen_prefix_len(msgs, window) == 1


class TestMergeTranscripts:
    def test_orders_by_timestamp_across_sources(self) -> None:
        slot = [{"role": "user", "content": "b", "ts": "2026-07-30T00:02:00Z"}]
        chan = [
            {"role": "user", "content": "a", "ts": "2026-07-30T00:01:00Z"},
            {"role": "user", "content": "c", "ts": "2026-07-30T00:03:00Z"},
        ]
        out = channel_slots.merge_transcripts(slot, chan)
        assert [m["content"] for m in out] == ["a", "b", "c"]

    def test_drops_duplicates_already_copied_into_the_slot(self) -> None:
        msg = {"role": "user", "content": "same", "ts": "2026-07-30T00:01:00Z"}
        out = channel_slots.merge_transcripts([dict(msg)], [dict(msg)])
        assert len(out) == 1

    def test_keeps_same_text_at_different_times(self) -> None:
        out = channel_slots.merge_transcripts(
            [{"role": "user", "content": "ping", "ts": "2026-07-30T00:01:00Z"}],
            [{"role": "user", "content": "ping", "ts": "2026-07-30T00:09:00Z"}],
        )
        assert len(out) == 2

    def test_untimestamped_messages_sort_last_and_keep_order(self) -> None:
        out = channel_slots.merge_transcripts(
            [{"role": "user", "content": "no ts 1"}, {"role": "user", "content": "no ts 2"}],
            [{"role": "user", "content": "has ts", "ts": "2026-07-30T00:01:00Z"}],
        )
        assert [m["content"] for m in out] == ["has ts", "no ts 1", "no ts 2"]

    def test_empty_sources(self) -> None:
        assert channel_slots.merge_transcripts([], []) == []


class TestSharedSessionBinding:
    """The single-session contract: one transcript, both surfaces, no sync."""

    def _msgs(self, n: int, *, prefix: str = "m") -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"{prefix}{i}", "ts": f"2026-07-30T00:{i:02d}:00Z"}
            for i in range(n)
        ]

    def test_slot_name_lookup_round_trips_every_namespace(self) -> None:
        """Exact reverse lookup — including keys with inner separators.

        A string substitution cannot recover ``telegram:kirocrew:direct:9:gen3``
        from ``telegram_kirocrew_direct_9_gen3``, which is why this resolves
        against the session list instead.
        """
        keys = [
            "slack:1785457986.925389",
            "discord:123",
            "telegram:kirocrew:direct:9:gen3",
        ]
        sessions = [_session(k) for k in keys]
        for key in keys:
            name = channel_slots.channel_slot_name(key)
            assert channel_slots.channel_key_for_slot_name(name, sessions) == key

    def test_slot_name_lookup_accepts_the_filename_stem_spelling(self) -> None:
        """``list_sessions`` may serve either spelling; both fold to one slot."""
        sessions = [_session("slack_1.1")]
        assert channel_slots.channel_key_for_slot_name("slack_1.1", sessions) == "slack_1.1"

    def test_slot_name_lookup_rejects_ordinary_dashboard_slots(self) -> None:
        """A normal tab must never be mistaken for a channel session."""
        sessions = [_session("slack:1.1"), _session("dashboard_chat-42-1785467660")]
        assert channel_slots.channel_key_for_slot_name("chat-42-1785467660", sessions) == ""
        assert channel_slots.channel_key_for_slot_name("", sessions) == ""
        assert channel_slots.channel_key_for_slot_name("slack_9.9", sessions) == ""

    def test_save_writes_the_channel_key_not_a_second_file(
        self, dashboard_state: Any, tmp_path: Any
    ) -> None:
        """The regression that motivated this: two files meant a stale tab."""
        from kiro_crew.dashboard.chat_utils import slot_transcript_key

        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None
        assert slot_transcript_key(slot) == "slack:1.1"
        # The legacy dashboard-keyed file is NOT the write target any more.
        assert slot_transcript_key(slot) != "dashboard:slack_1.1"

    def test_detail_serves_the_whole_shared_transcript(self, dashboard_state: Any) -> None:
        """The reported bug: 62 messages on disk, 2 served.

        With the counters measured against the shared file, the detail handler's
        ``_disk_older_count > 0`` gate opens and the frozen prefix is read back,
        so the tab shows every turn rather than the first reconcile's window.
        """
        total = channel_slots._HYDRATE_LIMIT + 12
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["slack:1.1"] = self._msgs(total)
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1.1"]
        assert slot._disk_older_count == 12
        assert len(slot.messages) == channel_slots._HYDRATE_LIMIT
        # Frozen prefix + live window reconstructs the full conversation, which
        # is exactly what api_chat_slot_detail concatenates.
        served = log.transcripts["slack:1.1"][: slot._disk_older_count] + list(slot.messages)
        assert len(served) == total
        assert [m["content"] for m in served] == [m["content"] for m in self._msgs(total)]

    def test_resync_picks_up_turns_added_on_the_channel(self, dashboard_state: Any) -> None:
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["slack:1.1"] = self._msgs(2)
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1.1"]
        assert [m["content"] for m in slot.messages] == ["m0", "m1"]

        log.transcripts["slack:1.1"] = self._msgs(2) + [
            {"role": "assistant", "content": "later", "ts": "2026-07-30T23:00:00Z"}
        ]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert [m["content"] for m in slot.messages] == ["m0", "m1", "later"]

    def test_resync_leaves_a_dirty_slot_alone(self, dashboard_state: Any) -> None:
        """The user owns the window once a turn has touched it."""
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None
        slot._dirty = True
        changed = channel_slots.resync_channel_slot(
            slot, self._msgs(5), disk_older=0, disk_window=5
        )
        assert changed is False
        assert len(slot.messages) == 2

    def test_resync_leaves_a_running_slot_alone(self, dashboard_state: Any) -> None:
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None

        async def _drive() -> None:
            # `running` is derived from an unfinished task, so give it one.
            async def _never() -> None:
                await asyncio.sleep(3600)

            slot.task = asyncio.create_task(_never())
            try:
                assert slot.running is True
                assert (
                    channel_slots.resync_channel_slot(
                        slot, self._msgs(5), disk_older=0, disk_window=5
                    )
                    is False
                )
            finally:
                slot.task.cancel()

        asyncio.run(_drive())
        assert len(slot.messages) == 2

    def test_resync_refreshes_counters_even_when_the_window_is_current(
        self, dashboard_state: Any
    ) -> None:
        """A stale count would make the next save rewrite the file short."""
        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None
        assert (
            channel_slots.resync_channel_slot(slot, self._msgs(2), disk_older=4, disk_window=2)
            is False
        )
        assert slot._disk_older_count == 4
        assert slot._disk_window_len == 2

    def test_surfaced_slack_slot_is_bound_for_inbound_replies(
        self, dashboard_state: Any
    ) -> None:
        """``maybe_route_linked_thread`` finds the slot by BARE thread_ts.

        Without this the inbound Slack reply runs Slack's own turn loop against
        the channel session and the dashboard tab never sees it.
        """
        log = _FakeLog([_session("slack:1785457986.925389")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1785457986.925389"]
        assert slot._slack_linked is True
        assert slot._slack_thread_ts == "1785457986.925389"
        assert dashboard_state.get_linked_slot("1785457986.925389") is slot

    def test_non_slack_channels_are_not_slack_bound(self, dashboard_state: Any) -> None:
        log = _FakeLog([_session("discord:9.9")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["discord_9.9"]
        assert slot.shared_session_key == "discord:9.9"
        assert slot._slack_linked is False

    def test_existing_slot_without_the_binding_is_self_healed(
        self, dashboard_state: Any
    ) -> None:
        """Covers a slot surfaced before this change, and a gateway restart."""
        log = _FakeLog([_session("slack:1.1")], {})
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        slot = dashboard_state.get_or_create_slot(name="slack_1.1", agent="")
        assert slot.shared_session_key == ""

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) >= 1
        assert slot.shared_session_key == "slack:1.1"

    def test_title_persists_to_the_shared_session_not_a_second_file(
        self, dashboard_state: Any
    ) -> None:
        """Metadata must ride in the transcript the slot actually writes.

        Keyed off the slot's own name instead, an auto-title would land in a
        ``dashboard:``-keyed file that otherwise does not exist — creating a
        stray metadata-only transcript, and losing the title on restore, which
        reads the shared key.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard import chat_title

        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None
        slot.title = "Ship the thing"

        seen: list[str] = []

        class _Log:
            def set_title(self, key: str, title: str) -> None:
                seen.append(key)

        dashboard_state.conversation_log = _Log()
        _asyncio.run(chat_title._persist_title(dashboard_state, slot))
        assert seen == ["slack:1.1"]

    def test_session_teardown_targets_the_shared_key(self, dashboard_state: Any) -> None:
        """The provider and its semaphore are registered under the run key.

        Removing ``dashboard:<slot.key>`` would leave the real session alive.
        """
        from kiro_crew.dashboard.chat_utils import slot_session_key

        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, self._msgs(2)
        )
        assert slot is not None
        assert slot_session_key(slot) == "slack:1.1"
        assert slot_session_key(slot) != "dashboard:slack_1.1"

    def test_cron_style_linked_slot_keeps_its_own_transcript(
        self, dashboard_state: Any
    ) -> None:
        """Guards the seam: linked_session_key must NOT move the transcript.

        Cron/workflow slots are display mirrors of a run that owns its own
        transcript. Folding them into the shared path would redirect their
        writes onto the cron key and double up with the cron injector.
        """
        from kiro_crew.dashboard.chat_utils import slot_session_key, slot_transcript_key

        slot = dashboard_state.get_or_create_slot(name="cron-abc", agent="")
        slot.linked_session_key = "cron:abc"
        assert slot_session_key(slot) == "cron:abc"
        assert slot_transcript_key(slot) == "dashboard:cron-abc"

    def test_surfaced_slot_takes_precedence_over_a_linked_key(
        self, dashboard_state: Any
    ) -> None:
        from kiro_crew.dashboard.chat_utils import slot_session_key, slot_transcript_key

        slot = channel_slots.surface_channel_session(
            dashboard_state, _session("slack:1.1"), {}, []
        )
        assert slot is not None
        slot.linked_session_key = "cron:abc"
        assert slot_session_key(slot) == "slack:1.1"
        assert slot_transcript_key(slot) == "slack:1.1"


class TestReconcileMore:

    def test_second_pass_reports_no_change_but_keeps_following(
        self, dashboard_state: Any
    ) -> None:
        """Steady state reports 0 changes — but it DOES re-read the transcript.

        The re-read is what makes the tab live-follow a conversation still moving
        on the channel, and it keeps the frozen-prefix counters honest. Reporting
        0 is what stops it broadcasting a slots update every 30s.
        """
        log = _FakeLog([_session("slack:1.1")], {})
        log.transcripts["slack:1.1"] = [
            {"role": "user", "content": "stable", "ts": "2026-07-30T00:00:00Z"}
        ]
        dashboard_state.conversation_log = log
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]

        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        log.message_reads.clear()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0
        assert log.message_reads, "steady state must keep following the shared transcript"

    def test_works_on_stem_form_keys_as_served_by_list_sessions(
        self, dashboard_state: Any
    ) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack_1785370133.085469"), _session("dashboard_chat-1-1")], {}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        slot = dashboard_state._slots["slack_1785370133.085469"]
        assert slot.linked_session_key == "", "one history key only"

    def test_no_conversation_log_is_a_no_op(self, dashboard_state: Any) -> None:
        dashboard_state.conversation_log = None
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0

    def test_list_sessions_failure_is_swallowed(self, dashboard_state: Any) -> None:
        class Boom:
            def list_sessions(self) -> list[dict[str, Any]]:
                raise OSError("disk gone")

        dashboard_state.conversation_log = Boom()
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 0

    def test_one_bad_session_does_not_block_the_others(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dashboard_state.conversation_log = _FakeLog(
            [_session("slack:1.1"), _session("slack:2.2")], {}
        )
        dashboard_state.push_slots_update = lambda: None  # type: ignore[method-assign]
        real = channel_slots.surface_channel_session

        def flaky(
            state: Any, info: dict[str, Any], meta: Any, msgs: Any, **kw: Any
        ) -> Any:
            if info["key"] == "slack:1.1":
                raise RuntimeError("boom")
            return real(state, info, meta, msgs, **kw)

        monkeypatch.setattr(channel_slots, "surface_channel_session", flaky)
        assert asyncio.run(channel_slots.reconcile_channel_slots(dashboard_state, 30)) == 1
        assert "slack_2.2" in dashboard_state._slots
