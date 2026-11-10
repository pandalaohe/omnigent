"""Context notice delivery and durable rotation recovery at their IO boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from omnigent.entities import ErrorData
from omnigent.server.auto_rotation import FRESH_ROUND_LABEL_KEY, NOTICED_COUNT_LABEL_KEY
from omnigent.server.auto_rotation_notices import AutoRotationNotices
from omnigent.stores.conversation_store import ROTATE_REQUESTED_LABEL_KEY, SUCCEEDED_BY_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def service(store, client, posts, state, outcome):
    async def true_state(conv):
        return state[0], True

    async def post(request, session_id, event, **kwargs):
        posts.append(event.data)
        return outcome[0]

    return AutoRotationNotices(
        conversation_store=store,
        permission_store=None,
        prefs_store=None,
        runner_router=SimpleNamespace(
            client_for_session_resources=lambda session_id: SimpleNamespace(client=client)
        ),
        owner_for=lambda conv: None,
        owns_parent=lambda conv: True,
        true_state=true_state,
        post_event=post,
    )


def due_session(store, harness="antigravity-native", enabled="on"):
    conv = store.create_conversation(harness_override=harness)
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": enabled,
            "omnigent.auto_rotation.compactions": "1",
            "omnigent.auto_rotation_state.inferred_compactions": "1",
            FRESH_ROUND_LABEL_KEY: "1",
            "omnigent.last_context_tokens": "150000",
            "omnigent.last_context_window": "200000",
        },
    )
    return conv


@pytest.mark.asyncio
async def test_notice_waits_retries_then_records_once(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    posts, state, outcome = [], ["busy"], [{"forwarded": False}]
    notices = service(store, None, posts, state, outcome)
    await notices.start(SimpleNamespace())
    try:
        notices.mark_due(conv.id, 1)
        await notices._deliveries[(0, conv.id)]
        assert posts == []
        state[0] = "steerable"
        await notices.tick()
        await notices._deliveries[(0, conv.id)]
        assert NOTICED_COUNT_LABEL_KEY not in store.get_conversation(conv.id).labels
        outcome[0] = {"forwarded": True}
        await notices.tick()
        await notices._deliveries[(0, conv.id)]
        assert store.get_conversation(conv.id).labels[NOTICED_COUNT_LABEL_KEY] == "1"
        assert "[System: context]" in posts[-1]["content"][0]["text"]
        assert "approximate for this session type" in posts[-1]["content"][0]["text"]
        assert "rotate=true and auto=true" in posts[-1]["content"][0]["text"]
        assert (
            "For this rotation, this notice supersedes earlier instructions to hand over "
            "with rotate=false only or never to clear."
        ) in posts[-1]["content"][0]["text"]
        await notices.tick()
        assert len(posts) == 2
    finally:
        await notices.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["denied", "new_round", "off"])
async def test_dropped_notice_writes_no_marker(db_uri, change):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    posts, outcome = [], [{"denied": True, "forwarded": False}]
    notices = service(store, None, posts, ["idle"], outcome)
    notices.mark_due(conv.id, 1)
    if change == "new_round":
        store.set_labels(conv.id, {"omnigent.auto_rotation_state.inferred_compactions": "2"})
    elif change == "off":
        store.set_labels(conv.id, {"omnigent.auto_rotation": "off"})
    await notices.tick()
    await notices._deliveries[(0, conv.id)]
    assert NOTICED_COUNT_LABEL_KEY not in store.get_conversation(conv.id).labels
    await notices.tick()
    assert len(posts) == (1 if change == "denied" else 0)


@pytest.mark.asyncio
async def test_recovery_rediscovers_bounds_attempts_and_reports_failure(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "not_requested"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        state = ["busy"]
        notices = service(store, client, [], state, [{}])
        await notices.start(SimpleNamespace())
        try:
            await notices.tick()
            state[0] = "idle"
            await notices.tick()
            assert calls == []
            await notices.tick()
            assert calls == [f"/v1/sessions/{conv.id}/pending-rotation"]
            for _ in range(4):
                await notices.tick()
            assert len(calls) == 2
            errors = store.list_items(conv.id, type="error").data
            assert len(errors) == 1
            assert errors[0].data.code == "session_rotation_failed"
            assert "Type /clear" in errors[0].data.message
            await notices.tick()
            assert len(calls) == 2
        finally:
            await notices.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unconfirmed", "draft_may_remain"])
async def test_injected_rotation_warns_without_another_retry(db_uri, outcome):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": outcome})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        await notices.start(SimpleNamespace())
        try:
            for _ in range(6):
                await notices.tick()
            assert len(calls) == 1
            errors = store.list_items(conv.id, type="error").data
            assert len(errors) == 1
            error = cast(ErrorData, errors[0].data)
            assert error.code == "session_rotation_failed"
            assert "unconfirmed" in error.message
            if outcome == "draft_may_remain":
                assert "draft may remain" in error.message
                assert "before pressing Enter" in error.message
            else:
                assert "will not be retried automatically" in error.message
        finally:
            await notices.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["off", "superseded", "revision", "removed", "archived"])
async def test_recovery_drops_invalidated_request(db_uri, stop):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200)
        ),
        base_url="http://runner",
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        notices.track(conv.id, "0")
        await notices.tick()
        if stop == "off":
            store.set_labels(conv.id, {"omnigent.auto_rotation": "off"})
        elif stop == "superseded":
            store.set_labels(conv.id, {SUCCEEDED_BY_LABEL_KEY: "next"})
        elif stop == "revision":
            store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "1"})
        elif stop == "removed":
            store.delete_label(conv.id, ROTATE_REQUESTED_LABEL_KEY)
        else:
            store.update_conversation(conv.id, archived=True)
        for _ in range(6):
            await notices.tick()
        assert calls == []


@pytest.mark.asyncio
async def test_new_round_replaces_notice_while_readiness_is_pending(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    posts = []
    notices = service(store, None, posts, ["idle"], [{"forwarded": True}])
    entered, release = asyncio.Event(), asyncio.Event()

    async def readiness(conv):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return "idle", True

    notices._state = readiness
    await notices.start(SimpleNamespace())
    try:
        notices.mark_due(conv.id, 1)
        old = notices._deliveries[(0, conv.id)]
        await asyncio.wait_for(entered.wait(), 2)
        store.set_labels(
            conv.id,
            {
                "omnigent.auto_rotation_state.inferred_compactions": "2",
                FRESH_ROUND_LABEL_KEY: "2",
            },
        )
        notices.mark_due(conv.id, 2)
        release.set()
        await old
        replacement = notices._deliveries.get((0, conv.id))
        if replacement is not None:
            await replacement
        assert len(posts) == 1
        assert "2 times" in posts[0]["content"][0]["text"]
        assert store.get_conversation(conv.id).labels[NOTICED_COUNT_LABEL_KEY] == "2"
    finally:
        await notices.shutdown()


@pytest.mark.asyncio
async def test_switch_disabled_during_probe_prevents_retrigger(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    calls = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200)
        ),
        base_url="http://runner",
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        notices.track(conv.id, "0")
        await notices.tick()

        async def readiness(conv):
            store.set_labels(conv.id, {"omnigent.auto_rotation": "off"})
            return "idle", True

        notices._state = readiness
        await notices.tick()
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unconfirmed", "draft_may_remain"])
async def test_failed_attempt_allows_notice_only_after_another_interval(db_uri, outcome):
    from omnigent.server.auto_rotation import auto_rotation_for

    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": outcome})
        ),
        base_url="http://runner",
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        await notices.start(SimpleNamespace())
        try:
            for _ in range(2):
                await notices.tick()
            assert not auto_rotation_for(store.get_conversation(conv.id), store).due
            store.set_labels(
                conv.id,
                {
                    "omnigent.auto_rotation_state.inferred_compactions": "2",
                    FRESH_ROUND_LABEL_KEY: "2",
                },
            )
            status = auto_rotation_for(store.get_conversation(conv.id), store)
            assert status.due is (outcome == "unconfirmed")
            assert store.get_conversation(conv.id).labels[ROTATE_REQUESTED_LABEL_KEY] == "0"
            # Rediscovery must not re-trigger a completed attempt.
            notices._rediscover()
            assert not notices._retry
        finally:
            await notices.shutdown()


@pytest.mark.asyncio
async def test_legacy_draft_failure_survives_rediscovery(db_uri):
    import hashlib

    from omnigent.entities import NewConversationItem
    from omnigent.server.auto_rotation import DRAFT_LABEL_KEY, auto_rotation_for

    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="error",
                response_id="failure",
                stable_id=hashlib.sha256(f"rotation-failed:{conv.id}:0".encode()).hexdigest()[:32],
                data=ErrorData(
                    source="harness",
                    code="session_rotation_failed",
                    message="Rotation unconfirmed; draft may remain.",
                ),
            )
        ],
    )
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                calls.append(request) or httpx.Response(200, json={"status": "draft_may_remain"})
            )
        ),
        base_url="http://runner",
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        notices._rediscover()
        assert store.get_conversation(conv.id).labels[DRAFT_LABEL_KEY] == "true"
        for _ in range(4):
            await notices.tick()
        store.set_labels(
            conv.id,
            {"omnigent.auto_rotation_state.inferred_compactions": "9", FRESH_ROUND_LABEL_KEY: "9"},
        )
        assert not auto_rotation_for(store.get_conversation(conv.id), store).due
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_finishing_old_failure_keeps_new_attempt_tracking(db_uri, monkeypatch):
    from omnigent.server.auto_rotation import ATTEMPT_LABEL_KEY

    store = SqlAlchemyConversationStore(db_uri)
    conv = due_session(store)
    store.set_labels(conv.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "unconfirmed"})
        ),
        base_url="http://runner",
    ) as client:
        notices = service(store, client, [], ["idle"], [{}])
        notices.track(conv.id, "0")
        original = store.append

        def append(sid, items):
            result = original(sid, items)
            store.set_labels(sid, {ATTEMPT_LABEL_KEY: "next-attempt"})
            notices.track(sid, "0")
            return result

        monkeypatch.setattr(store, "append", append)
        await notices.tick()
        await notices.tick()
        assert notices._retry[(0, conv.id)].attempt == "0:next-attempt"
