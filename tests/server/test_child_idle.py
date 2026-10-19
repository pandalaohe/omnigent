"""Idle reminders through a real store, with clock and notification I/O injected."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter

from omnigent.entities import Conversation
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import pending_elicitations
from omnigent.server.child_idle import (
    CHILD_IDLE_BATCH_LABEL,
    CHILD_IDLE_LABEL,
    ChildIdleSweeper,
    NoticeOutcome,
    batch_claim_valid,
)
from omnigent.server.routes.sessions.routes_peer import register_peer_routes
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.server.user_preferences_store import (
    ChildIdleSettings,
    SqlAlchemyUserPreferencesStore,
    UserPreferencesValidationError,
    read_child_idle_settings,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class _Harness:
    def __init__(self, db_uri: str) -> None:
        self.store = SqlAlchemyConversationStore(db_uri)
        self.prefs = SqlAlchemyUserPreferencesStore(db_uri)
        self.now = 100000
        self.calls: list[tuple[str, str]] = []
        self.owned: set[str] = set()
        self.outcome: NoticeOutcome = "accepted"
        self.raise_on_post = False
        self.sweeper = self.new_sweeper()
        self.settings()

    def new_sweeper(self) -> ChildIdleSweeper:
        sweeper = ChildIdleSweeper(
            conversation_store=self.store,
            permission_store=None,
            owns_parent=lambda conv: conv.id in self.owned,
            post_notice=self.post,
            clock=lambda: self.now,
        )
        sweeper._app = SimpleNamespace(state=SimpleNamespace(user_preferences_store=self.prefs))
        return sweeper

    async def post(self, parent: str, text: str, _claim: str) -> NoticeOutcome:
        self.calls.append((parent, text))
        if self.raise_on_post:
            raise TimeoutError("response lost")
        return self.outcome

    def settings(self, **patch: Any) -> None:
        self.prefs.patch_namespace("local", "child_idle", {"enabled": True, **patch})

    def child(self, title: str = "work", parent: Conversation | None = None) -> Conversation:
        if parent is None:
            parent = self.store.create_conversation(title="mother")
        self.owned.add(parent.id)
        child = self.store.create_conversation(
            title=title,
            kind="sub_agent",
            parent_conversation_id=parent.id,
            workspace="/opt/work/project",
            labels={RUNNING_SINCE_LABEL_KEY: "90000"},
        )
        self.store.set_session_live_status(child.id, "idle")
        return child


@pytest.fixture
def harness(db_uri: str):
    pending_elicitations.reset_for_tests()
    yield _Harness(db_uri)
    pending_elicitations.reset_for_tests()


async def test_each_child_threshold_and_non_sliding_mother_window(harness: _Harness) -> None:
    first = harness.child("first")
    harness.store.append(
        first.id,
        [
            NewConversationItem(
                type="message",
                response_id="response-a",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[
                        {"type": "output_text", "text": "Ready for review"},
                    ],
                ),
            )
        ],
    )
    await harness.sweeper._tick()
    harness.now += 1200
    parent = harness.store.get_conversation(first.parent_conversation_id)
    second = harness.child("second", parent)
    await harness.sweeper._tick()
    harness.now += 2399
    await harness.sweeper._tick()
    assert harness.calls == []
    harness.now += 1
    await harness.sweeper._tick()
    harness.now += 1200
    await harness.sweeper._tick()
    assert harness.calls == []
    harness.now += 600
    await harness.sweeper._tick()
    assert len(harness.calls) == 1
    receiver, text = harness.calls[0]
    assert receiver == first.parent_conversation_id
    assert first.id in text and second.id in text
    assert '"idle_minutes": 90' in text and '"idle_minutes": 70' in text
    assert "Ready for review" in text
    assert '"last_assistant_reply_tail": null' in text
    assert harness.store.list_items(second.id).data == []


async def test_independent_mothers_restart_and_repeat_cooldown(harness: _Harness) -> None:
    first, second = harness.child("first"), harness.child("second")
    await harness.sweeper._tick()
    harness.now += 3600
    await harness.sweeper._tick()
    harness.now += 1800
    await harness.sweeper._tick()
    assert {receiver for receiver, _ in harness.calls} == {
        first.parent_conversation_id,
        second.parent_conversation_id,
    }
    harness.sweeper = harness.new_sweeper()
    harness.now += 14399
    await harness.sweeper._tick()
    assert len(harness.calls) == 2
    harness.now += 1
    await harness.sweeper._tick()
    assert len(harness.calls) == 2
    harness.now += 1800
    await harness.sweeper._tick()
    assert len(harness.calls) == 4


@pytest.mark.parametrize("async_kind", [None, "question", "approval"])
async def test_any_own_pending_card_restarts_idle_clock(
    harness: _Harness, async_kind: str | None
) -> None:
    child = harness.child()
    await harness.sweeper._tick()
    pending_elicitations.record_publish(
        child.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "question",
            "params": {"async_kind": async_kind, "message": "A decision"},
        },
    )
    harness.now += 5400
    await harness.sweeper._tick()
    assert harness.calls == []
    pending_elicitations.resolve(child.id, "question")
    await harness.sweeper._tick()
    harness.now += 3599
    await harness.sweeper._tick()
    assert harness.calls == []


@pytest.mark.parametrize(
    "blocked", ["running", "waiting", "persisted-card", "archived", "mirror", "unknown"]
)
async def test_exclusions(harness: _Harness, blocked: str) -> None:
    harness.settings(mergeWindowSeconds=0)
    child = harness.child()
    await harness.sweeper._tick()
    if blocked == "persisted-card":
        harness.store.set_pending_elicitation_count(child.id, 1)
    elif blocked == "archived":
        harness.store.update_conversation(child.id, archived=True)
    elif blocked == "mirror":
        harness.store.set_labels(child.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    elif blocked == "unknown":
        harness.store.update_conversation(child.id, archived=True)
        child = harness.store.create_conversation(
            kind="sub_agent", parent_conversation_id=child.parent_conversation_id
        )
    else:
        harness.store.set_session_live_status(child.id, blocked)
    harness.now += 5400
    await harness.sweeper._tick()
    assert harness.calls == []


async def test_turn_marker_resets_but_warm_metadata_does_not(harness: _Harness) -> None:
    child = harness.child()
    harness.settings(mergeWindowSeconds=0)
    await harness.sweeper._tick()
    harness.now += 3500
    harness.store.set_labels(child.id, {RUNNING_SINCE_LABEL_KEY: "103400"})
    await harness.sweeper._tick()
    harness.now += 3500
    harness.store.set_labels(child.id, {"omnigent.keep_warm": '{"u":107000}'})
    await harness.sweeper._tick()
    assert harness.calls == []
    harness.now += 100
    await harness.sweeper._tick()
    assert len(harness.calls) == 1


@pytest.mark.parametrize("outcome", ["deferred", "rejected", "uncertain", "exception"])
async def test_definite_rejection_refunds_but_uncertainty_cools_down(
    harness: _Harness, outcome: str
) -> None:
    harness.settings(mergeWindowSeconds=0)
    harness.child()
    await harness.sweeper._tick()
    harness.now += 3600
    if outcome == "exception":
        harness.raise_on_post = True
    else:
        harness.outcome = outcome  # type: ignore[assignment]
    await harness.sweeper._tick()
    assert len(harness.calls) == 1
    harness.now += 60
    harness.outcome, harness.raise_on_post = "accepted", False
    await harness.sweeper._tick()
    assert len(harness.calls) == (2 if outcome in ("deferred", "rejected") else 1)


async def test_lost_ownership_defers_and_new_owner_resumes(harness: _Harness) -> None:
    child = harness.child()
    await harness.sweeper._tick()
    harness.now += 3600
    await harness.sweeper._tick()
    harness.owned.clear()
    harness.now += 1800
    await harness.sweeper._tick()
    assert harness.calls == []
    harness.owned.add(child.parent_conversation_id)
    harness.sweeper = harness.new_sweeper()
    await harness.sweeper._tick()
    assert len(harness.calls) == 1


async def test_archived_mother_and_foreign_pending_card_index(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = harness.child()
    harness.settings(mergeWindowSeconds=0)
    await harness.sweeper._tick()
    harness.now += 3600
    harness.store.update_conversation(child.parent_conversation_id, archived=True)
    await harness.sweeper._tick()
    assert harness.calls == []
    harness.store.update_conversation(child.parent_conversation_id, archived=False)

    def unreadable(_session_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr("omnigent.server.child_idle.snapshot_for", unreadable)
    await harness.sweeper._tick()
    assert harness.calls == []


async def test_notification_entry_checks_owner_idle_and_outcome(db_uri: str) -> None:
    pending_elicitations.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="parent", runner_id="runner-a")
    state = SimpleNamespace()
    app = SimpleNamespace(state=state)
    connected = {"runner-a"}
    posts: list[tuple[str, Any]] = []
    result: Any = {}

    async def post(request: Any, session_id: str, event: Any, **kwargs: Any) -> Any:
        assert request.app is app
        posts.append((session_id, event))
        if isinstance(result, Exception):
            raise result
        return result

    register_peer_routes(
        APIRouter(),
        conversation_store=store,
        post_event_impl=post,
        runner_router=SimpleNamespace(runner_is_online=lambda runner_id: runner_id in connected),
        app_state=state,
    )
    idle = state.child_idle
    idle._app = app
    claim = idle._claim_batch(parent.id)
    assert claim is not None
    assert await idle._post_notice(parent.id, "status data", claim) == "deferred"
    store.set_session_live_status(parent.id, "idle")
    assert await idle._post_notice(parent.id, "status data", claim) == "accepted"
    assert posts[-1][0] == parent.id
    assert posts[-1][1].data["content"][0]["text"] == "status data"
    result = {"forwarded": False}
    assert await idle._post_notice(parent.id, "status data", claim) == "rejected"
    result = TimeoutError("response lost")
    assert await idle._post_notice(parent.id, "status data", claim) == "uncertain"
    count = len(posts)
    store.set_session_live_status(parent.id, "running")
    assert await idle._post_notice(parent.id, "status data", claim) == "deferred"
    store.set_session_live_status(parent.id, "idle")
    pending_elicitations.record_publish(
        parent.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "mother-question",
            "params": {"async_kind": "question"},
        },
    )
    assert await idle._post_notice(parent.id, "status data", claim) == "deferred"
    pending_elicitations.resolve(parent.id, "mother-question")
    connected.clear()
    assert await idle._post_notice(parent.id, "status data", claim) == "deferred"
    assert len(posts) == count


@pytest.mark.parametrize("phase", ["queue", "attempt"])
async def test_overlapping_sweepers_keep_one_mother_batch(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    harness.settings(mergeWindowSeconds=0)
    first = harness.child("first")
    second = harness.child("second", harness.store.get_conversation(first.parent_conversation_id))
    await harness.sweeper._tick()
    harness.now += 3600
    other = harness.new_sweeper()
    reserved, release = threading.Event(), threading.Event()
    original = harness.store.compare_and_set_label

    def cas(conv_id: str, key: str, expected: str, value: str | None) -> bool:
        won = original(conv_id, key, expected, value)
        state = json.loads(value) if key == CHILD_IDLE_LABEL and value is not None else {}
        pause = state.get("q", 0) > 0 if phase == "queue" else state.get("n", 0) > 0
        if (
            won
            and conv_id == first.id
            and key == CHILD_IDLE_LABEL
            and pause
            and not reserved.is_set()
        ):
            reserved.set()
            if not release.wait(10):
                raise TimeoutError("test did not release first reservation")
        return won

    monkeypatch.setattr(harness.store, "compare_and_set_label", cas)
    task = asyncio.create_task(harness.sweeper._tick())
    try:
        assert await asyncio.to_thread(reserved.wait, 10)
        before_other = harness.store.get_conversation(second.id).labels[CHILD_IDLE_LABEL]
        await other._tick()
        assert harness.calls == []
        if phase == "queue":
            untouched = harness.store.get_conversation(second.id).labels[CHILD_IDLE_LABEL]
            assert untouched == before_other
    finally:
        release.set()
        await task
    assert len(harness.calls) == 1
    assert first.id in harness.calls[0][1] and second.id in harness.calls[0][1]


async def test_deferred_refund_does_not_overwrite_new_episode(harness: _Harness) -> None:
    child = harness.child()
    harness.settings(mergeWindowSeconds=0)
    await harness.sweeper._tick()
    harness.now += 3600
    replacement = '{"t":"new-turn","i":103600,"q":0,"n":0}'

    async def post(_parent: str, _text: str, _claim: str) -> NoticeOutcome:
        harness.store.set_labels(child.id, {CHILD_IDLE_LABEL: replacement})
        return "deferred"

    harness.sweeper._post_notice = post
    await harness.sweeper._tick()
    assert harness.store.get_conversation(child.id).labels[CHILD_IDLE_LABEL] == replacement


def test_atomic_label_transition_has_one_winner(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(labels={CHILD_IDLE_LABEL: "pending"})
    barrier = threading.Barrier(2)

    def reserve(value: str) -> bool:
        barrier.wait(timeout=5)
        return store.compare_and_set_label(conv.id, CHILD_IDLE_LABEL, "pending", value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ["attempt-a", "attempt-b"]))
    assert sorted(results) == [False, True]
    winner = store.get_conversation(conv.id).labels[CHILD_IDLE_LABEL]
    assert store.compare_and_set_label(conv.id, CHILD_IDLE_LABEL, "pending", None) is False
    assert store.compare_and_set_label(conv.id, CHILD_IDLE_LABEL, winner, None) is True
    assert store.compare_and_set_label(conv.id, CHILD_IDLE_LABEL, winner, "late") is False


def test_batch_lease_takeover_fences_old_owner(harness: _Harness) -> None:
    child = harness.child()
    parent_id = child.parent_conversation_id
    first = harness.sweeper._claim_batch(parent_id)
    assert first is not None
    assert harness.new_sweeper()._claim_batch(parent_id) is None
    harness.now += 301
    second = harness.new_sweeper()._claim_batch(parent_id)
    assert second is not None and second != first
    parent = harness.store.get_conversation(parent_id)
    assert batch_claim_valid(parent, first, harness.now) is False
    assert batch_claim_valid(parent, second, harness.now) is True
    assert (
        harness.store.compare_and_set_label(parent_id, CHILD_IDLE_BATCH_LABEL, first, None)
        is False
    )


async def test_slow_scan_does_not_create_an_expired_batch_lease(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.settings(mergeWindowSeconds=0)
    harness.child()
    await harness.sweeper._tick()
    harness.now += 3600
    original = harness.sweeper._children

    def slow_scan() -> list[Conversation]:
        children = original()
        harness.now += 600
        return children

    async def post(parent_id: str, text: str, claim: str) -> NoticeOutcome:
        parent = harness.store.get_conversation(parent_id)
        assert batch_claim_valid(parent, claim, harness.now)
        assert json.loads(claim)["until"] == harness.now + 300
        return await harness.post(parent_id, text, claim)

    monkeypatch.setattr(harness.sweeper, "_children", slow_scan)
    harness.sweeper._post_notice = post
    await harness.sweeper._tick()
    assert len(harness.calls) == 1


@pytest.mark.parametrize("change", ["card", "archive", "binding", "running"])
async def test_readiness_rechecks_fresh_mother(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from omnigent.server.routes.sessions import routes_peer

    pending_elicitations.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="parent", runner_id="runner-a")
    store.set_session_live_status(parent.id, "idle")
    state = SimpleNamespace()
    posts: list[str] = []

    async def post(_request: Any, session_id: str, _event: Any, **_kwargs: Any) -> dict[str, Any]:
        posts.append(session_id)
        return {}

    def readiness(_conv: Conversation) -> bool:
        if change == "card":
            pending_elicitations.record_publish(
                parent.id,
                {
                    "type": "response.elicitation_request",
                    "elicitation_id": "new-question",
                    "params": {"async_kind": "question"},
                },
            )
        elif change == "archive":
            store.update_conversation(parent.id, archived=True)
        elif change == "binding":
            store.replace_runner_id(parent.id, "runner-b")
        else:
            monkeypatch.setattr(
                routes_peer, "_session_status_from_cache", lambda *_args: "running"
            )
        return False

    register_peer_routes(
        APIRouter(),
        conversation_store=store,
        post_event_impl=post,
        runner_router=SimpleNamespace(runner_is_online=lambda _runner_id: True),
        app_state=state,
    )
    idle = state.child_idle
    idle._app = SimpleNamespace(state=state)
    claim = idle._claim_batch(parent.id)
    assert claim is not None
    monkeypatch.setattr(routes_peer, "_is_native_terminal_session", readiness)
    try:
        assert await idle._post_notice(parent.id, "status data", claim) == "deferred"
        assert posts == []
    finally:
        pending_elicitations.reset_for_tests()


async def test_disable_and_repeat_zero(harness: _Harness) -> None:
    child = harness.child()
    harness.settings(mergeWindowSeconds=0, repeatSeconds=0)
    await harness.sweeper._tick()
    harness.now += 3600
    await harness.sweeper._tick()
    harness.now += 604800
    await harness.sweeper._tick()
    assert len(harness.calls) == 1
    harness.settings(enabled=False)
    await harness.sweeper._tick()
    assert CHILD_IDLE_LABEL not in harness.store.get_conversation(child.id).labels


def test_preference_defaults_validation_and_owner_roundtrip(db_uri: str) -> None:
    store = SqlAlchemyUserPreferencesStore(db_uri)
    assert read_child_idle_settings(store, "alice") == ChildIdleSettings()
    store.patch_namespace("alice", "child_idle", {"enabled": True, "repeatSeconds": 0})
    assert read_child_idle_settings(store, "alice") == ChildIdleSettings(True, 3600, 1800, 0)
    assert read_child_idle_settings(store, "bob").enabled is False
    for patch in (
        {"enabled": "yes"},
        {"idleThresholdSeconds": True},
        {"mergeWindowSeconds": -1},
        {"repeatSeconds": 1},
    ):
        with pytest.raises(UserPreferencesValidationError):
            store.patch_namespace("alice", "child_idle", patch)
    assert json.loads(json.dumps(store.get("alice")))["settings"]["child_idle"]["enabled"] is True
