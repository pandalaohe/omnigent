"""An operator releases accounting without stopping work or retrying input."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.entities.session_startup import (
    StartupReceipt,
    StartupReleaseRequest,
    StartupReleaseTarget,
)
from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.host_store import HostStore
from omnigent.stores.session_startup_store import SessionStartupStore


def opening(conversations, *, session_id=None, preparing_from=None):
    store, host = SessionStartupStore(conversations.storage_location), uuid4().hex
    HostStore(conversations.storage_location).upsert_on_connect(host, host[:8], "local")
    conv = (
        conversations.get_conversation(session_id)
        if session_id
        else conversations.create_conversation()
    )
    bound_host, bound_runner = preparing_from if preparing_from is not None else (host, "runner")
    if bound_host is not None:
        if bound_host != host:
            HostStore(conversations.storage_location).upsert_on_connect(
                bound_host, bound_host[:8], "local"
            )
        conversations.set_host_id(conv.id, bound_host, "/opt/work/project")
    if bound_runner is not None:
        conversations.set_runner_id(conv.id, bound_runner)
    [item] = conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "first"}]),
            )
        ],
    )
    store.configure(host, mode="startup_only")
    store.register_host_generation(host, "generation")
    queued = store.enqueue(
        owner_user_id="local",
        host_id=host,
        session_id=conv.id,
        operation_key=uuid4().hex,
        input_id=item.id,
        preparation=(
            {
                "session_id": conv.id,
                "workspace": "/opt/work/project",
                "git": None,
                "user_id": None,
                "account_generation": None,
                "previous_host_id": bound_host,
                "previous_runner_id": bound_runner,
            }
            if preparing_from is not None
            else None
        ),
    )
    attempt = store.reserve(host)
    assert store.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        boundary="workspace_prepare" if preparing_from is not None else "runner_prepare",
        claim_id="claim",
    )
    current = store.get(queued.id)
    request = StartupReleaseRequest(
        current_host_generation="generation",
        targets=[
            StartupReleaseTarget(
                startup_id=current.id,
                session_id=conv.id,
                attempt_id=current.attempt_id,
                input_id=item.id,
                runtime_id="runtime",
                host_generation="generation",
                runner_id=bound_runner,
            )
        ],
    )
    return store, conversations.get_conversation(conv.id), item, current, request


def release(store, host, request):
    return store.release_startups(host, request, actor_user_id=None, actor_account_generation=None)


@pytest.mark.parametrize("store_fixture", ["conversation_store", "split_db_conversation_store"])
@pytest.mark.parametrize("previous_host", [None, "old"])
@pytest.mark.parametrize("moved", [False, True])
def test_preparing_release_owns_attempt_without_a_bound_target_host(
    request, monkeypatch, store_fixture, previous_host, moved
):
    conversations = request.getfixturevalue(store_fixture)
    previous = (uuid4().hex, "previous-runner") if previous_host else (None, None)
    store, conv, item, attempt, body = opening(conversations, preparing_from=previous)
    target_host = attempt.host_id
    assert (conv.host_id, conv.runner_id) == previous
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    if moved:
        other_host = uuid4().hex
        HostStore(conversations.storage_location).upsert_on_connect(other_host, "other", "local")
        conversations.set_host_id(conv.id, other_host, "/opt/work/other")
        with pytest.raises(OmnigentError, match="Startup target changed"):
            release(store, target_host, body)
        assert store.get(attempt.id).occupied
        return
    receipt = release(store, target_host, body)
    assert (
        release(SessionStartupStore(conversations.storage_location), target_host, body) == receipt
    )
    current = store.get(attempt.id)
    assert current.state == "operator_released" and not current.occupied
    assert current.resident and current.input_id == item.id
    assert (current.attempt_id, current.runtime_id, current.host_generation) == (
        attempt.attempt_id,
        attempt.runtime_id,
        attempt.host_generation,
    )
    assert current.preparation == attempt.preparation
    live = conversations.get_conversation(conv.id)
    assert (live.host_id, live.runner_id) == previous
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    assert not store.claim_boundary(
        attempt.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        boundary="runner_prepare",
        claim_id="late-prepare",
    )


def test_queue_marker_has_one_fixed_180_second_deadline(db_uri, monkeypatch):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, mode="startup_only")
    queued = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="first-opening",
        input_id=None,
    )
    attempt = store.reserve(host)
    assert store.claim(queued.id, attempt.attempt_id, runtime_id="runtime")
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    status = SessionStartupStore(db_uri).presentation(queued.session_id)
    assert (status.state, status.reason, status.unconfirmed_startups) == (
        "uncertain",
        "opening_unconfirmed",
        1,
    )
    assert store.get(queued.id).state == "verifying"
    assert not store.expire_observations(host, now=attempt.admitted_at + 179)
    [stuck] = store.expire_observations(host, now=attempt.admitted_at + 180)
    assert (stuck.state, stuck.reason) == ("uncertain", "opening_unconfirmed")
    assert stuck.occupied and stuck.resident
    assert not SessionStartupStore(db_uri).expire_observations(host, now=attempt.admitted_at + 360)


@pytest.mark.parametrize("store_fixture", ["conversation_store", "split_db_conversation_store"])
def test_release_retains_input_and_runtime_and_duplicate_receipt(
    request, monkeypatch, store_fixture
):
    conversations = request.getfixturevalue(store_fixture)
    store, conv, item, attempt, body = opening(conversations)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    # The preflight may have seen a live session. Archive is not a release CAS condition.
    conversations.update_conversation(conv.id, archived=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda _: release(
                    SessionStartupStore(conversations.storage_location), conv.host_id, body
                ),
                range(2),
            )
        )
    assert receipts[0] == receipts[1]
    current = store.get(attempt.id)
    assert current.state == "operator_released" and not current.occupied and current.resident
    assert current.release_receipt == receipts[0].model_dump(mode="json")
    assert current.input_id == item.id and not current.input_accepted
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    assert not store.claim(attempt.id, attempt.attempt_id, runtime_id="runtime")
    assert not store.claim_boundary(
        attempt.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation="generation",
        boundary="native_prepare",
        claim_id="late",
    )
    assert not store.cancel_unissued(attempt.id, attempt.attempt_id)
    assert not store.mark_uncertain(attempt.id, attempt.attempt_id, reason="late-timeout")
    with pytest.raises(ValueError):
        store.enqueue(
            owner_user_id="local",
            host_id=conv.host_id,
            session_id=conv.id,
            operation_key="new-attempt",
            input_id=item.id,
            runtime_generation=2,
        )
    assert store.confirm_stopped(attempt.id, attempt.attempt_id)
    assert store.get(attempt.id).state == "operator_released"
    assert not store.get(attempt.id).resident
    assert release(store, conv.host_id, body) == receipts[0]


@pytest.mark.parametrize(
    "fault", ["young", "target", "runner", "generation", "settled", "workspace", "batch"]
)
def test_release_rejects_changed_or_settled_target_without_freeing_its_slot(
    conversation_store, monkeypatch, fault
):
    store, conv, _item, attempt, body = opening(conversation_store)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch",
        lambda: attempt.admitted_at + (179 if fault == "young" else 180),
    )
    if fault == "target":
        body = body.model_copy(
            update={"targets": [body.targets[0].model_copy(update={"attempt_id": uuid4().hex})]}
        )
    if fault == "runner":
        conversation_store.replace_runner_id(conv.id, "different", expected_runner_id="runner")
    if fault == "batch":
        body = body.model_copy(
            update={
                "targets": [
                    *body.targets,
                    body.targets[0].model_copy(
                        update={"startup_id": uuid4().hex, "session_id": uuid4().hex}
                    ),
                ]
            }
        )
    if fault == "generation":
        store.register_host_generation(conv.host_id, "different")
    if fault == "settled":
        assert store.observe(
            StartupReceipt(
                startup_id=attempt.id,
                attempt_id=attempt.attempt_id,
                input_id=attempt.input_id,
                runtime_id="runtime",
                phase="opening_settled",
                source="runner",
            )
        )
    with workspace_scope(99 if fault == "workspace" else 0), pytest.raises(OmnigentError):
        release(store, conv.host_id, body)
    assert store.get(attempt.id).occupied is (fault != "settled")
    assert store.get(attempt.id).release_receipt is None


def test_release_does_not_unlock_dependent_child_but_unrelated_queue_drains(
    conversation_store, monkeypatch
):
    store, conv, _item, attempt, body = opening(conversation_store)
    child = store.enqueue(
        owner_user_id="local",
        host_id=conv.host_id,
        session_id=uuid4().hex,
        operation_key="child",
        input_id=None,
        parent_startup_id=attempt.id,
    )
    unrelated = store.enqueue(
        owner_user_id="local",
        host_id=conv.host_id,
        session_id=uuid4().hex,
        operation_key="other",
        input_id=None,
    )
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    release(store, conv.host_id, body)
    assert store.reserve(conv.host_id).id == unrelated.id
    assert store.reserve(conv.host_id) is None
    assert store.get(child.id).state == "queued"
