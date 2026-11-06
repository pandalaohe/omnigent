"""Execution acceptance linearizes with human controls and AP retirement."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from omnigent.entities.subagent_retry import InputExecutionClaim, RetryExecutionGuard
from tests.stores.test_subagent_retry_store import _journal, _open
from tests.stores.test_subagent_retry_store import journal as journal


@pytest.mark.parametrize("override", ["cancel", "hold", "new_input", "none"])
def test_recovery_receiver_fences_controls_and_can_retry_only_expired_attempts(journal, override):
    from omnigent.server.host_load_queue import LABEL
    from omnigent.server.turn_recovery import RECOVERY_INPUT_LABEL

    identity = uuid4().hex
    journal.conversations.set_runner_id(journal.child, journal.owner.runner_id)
    journal.conversations.set_labels(journal.child, {RECOVERY_INPUT_LABEL: identity})
    stale = InputExecutionClaim(
        input_id=identity, kind="recovery", turn_id="abandoned", admission_deadline=1
    )
    assert not journal.store.resolve_execution(
        journal.child, stale, runner_id=journal.owner.runner_id, close=True
    ).accepted
    # The same abandoned frame remains fenced; only a new, live attempt may consume it.
    assert (
        journal.store.admit_execution(
            journal.child, stale, runner_id=journal.owner.runner_id
        ).reason
        == "input_closed"
    )
    if override in {"cancel", "new_input"}:
        journal.conversations.set_labels(
            journal.child, {RECOVERY_INPUT_LABEL: f"cancelled:{identity}"}
        )
    elif override == "hold":
        journal.conversations.set_labels(journal.child, {LABEL: "queued@host"})
    fresh = stale.model_copy(
        update={"turn_id": "replacement", "admission_deadline": time.time() + 30}
    )
    result = journal.store.admit_execution(journal.child, fresh, runner_id=journal.owner.runner_id)
    assert result.accepted is (override == "none")
    if result.accepted:
        duplicate = fresh.model_copy(update={"turn_id": "duplicate"})
        assert not journal.store.admit_execution(
            journal.child, duplicate, runner_id=journal.owner.runner_id
        ).accepted


def test_closed_recovery_attempt_never_revives_and_successor_waits_for_deadline(
    journal, monkeypatch
):
    from omnigent.server.turn_recovery import RECOVERY_INPUT_LABEL

    journal.conversations.set_runner_id(journal.child, journal.owner.runner_id)
    identity = uuid4().hex
    journal.conversations.set_labels(journal.child, {RECOVERY_INPUT_LABEL: identity})
    first = InputExecutionClaim(
        input_id=identity, turn_id="abandoned", kind="recovery", admission_deadline=200
    )
    monkeypatch.setattr(
        "omnigent.stores.subagent_retry_store.sqlalchemy_store.time.time", lambda: 100
    )
    assert not journal.store.resolve_execution(
        journal.child, first, runner_id=journal.owner.runner_id, close=True
    ).accepted
    assert not journal.store.admit_execution(
        journal.child, first, runner_id=journal.owner.runner_id
    ).accepted
    successor = first.model_copy(update={"turn_id": "successor", "admission_deadline": 300})
    assert not journal.store.admit_execution(
        journal.child, successor, runner_id=journal.owner.runner_id
    ).accepted
    monkeypatch.setattr(
        "omnigent.stores.subagent_retry_store.sqlalchemy_store.time.time", lambda: 201
    )
    assert journal.store.admit_execution(
        journal.child, successor, runner_id=journal.owner.runner_id
    ).accepted


def test_every_abandoned_recovery_attempt_is_fenced(journal, monkeypatch):
    from omnigent.server.turn_recovery import RECOVERY_INPUT_LABEL

    journal.conversations.set_runner_id(journal.child, journal.owner.runner_id)
    identity = uuid4().hex
    journal.conversations.set_labels(journal.child, {RECOVERY_INPUT_LABEL: identity})
    first = InputExecutionClaim(
        input_id=identity, turn_id="first", kind="recovery", admission_deadline=200
    )
    second = first.model_copy(update={"turn_id": "second", "admission_deadline": 250})
    monkeypatch.setattr(
        "omnigent.stores.subagent_retry_store.sqlalchemy_store.time.time", lambda: 100
    )
    for claim in (first, second):
        assert not journal.store.resolve_execution(
            journal.child, claim, runner_id=journal.owner.runner_id, close=True
        ).accepted
    monkeypatch.setattr(
        "omnigent.stores.subagent_retry_store.sqlalchemy_store.time.time", lambda: 201
    )
    assert not journal.store.admit_execution(
        journal.child, second, runner_id=journal.owner.runner_id
    ).accepted
    third = first.model_copy(update={"turn_id": "third", "admission_deadline": 300})
    monkeypatch.setattr(
        "omnigent.stores.subagent_retry_store.sqlalchemy_store.time.time", lambda: 251
    )
    assert journal.store.admit_execution(
        journal.child, third, runner_id=journal.owner.runner_id
    ).accepted


@pytest.mark.parametrize(
    "key", ["omnigent.interrupted_turn.recovery_id", "omnigent.startup.host_load"]
)
def test_recovery_control_labels_take_admission_conversation_lock(journal, monkeypatch, key):
    locked = []
    original = journal.conversations._lock_conversation

    def lock(db, identity):
        locked.append(identity)
        return original(db, identity)

    monkeypatch.setattr(journal.conversations, "_lock_conversation", lock)
    journal.conversations.set_labels(journal.child, {key: "cancelled"})
    assert journal.child in locked


@pytest.mark.parametrize("drained", [False, True])
def test_final_wake_receiver_admits_one_turn_and_cannot_be_claimed_as_human(journal, drained):
    from tests.stores.test_subagent_retry_store import _finish

    chain = _open(journal)
    final = _finish(journal, chain)
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    human = InputExecutionClaim(input_id=final.notice_id, turn_id="wake-turn")
    assert (
        journal.store.admit_execution(
            journal.parent, human, runner_id=journal.owner.runner_id
        ).reason
        == "input_identity_conflict"
    )
    # An intervening human admission does not consume the final input.
    assert journal.store.record_input(
        journal.parent,
        input_id="human-intervention",
        turn_id="human-turn",
        expected_generation=0,
        payload={"text": "human work"},
    )
    if drained:
        assert journal.store.record_drain(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
    claim = human.model_copy(update={"kind": "wake"})
    result = journal.store.admit_execution(
        journal.parent, claim, runner_id=journal.owner.runner_id
    )
    if drained:
        assert not result.accepted
        assert journal.store.get_control(journal.parent).turn_id == "human-turn"
    else:
        assert result.accepted and result.receipt.input_generation == 2
        assert (
            journal.store.admit_execution(journal.parent, claim, runner_id=journal.owner.runner_id)
            == result
        )
        duplicate_turn = claim.model_copy(update={"turn_id": "second-wake-turn"})
        assert (
            journal.store.admit_execution(
                journal.parent, duplicate_turn, runner_id=journal.owner.runner_id
            ).reason
            == "input_already_consumed"
        )
        assert journal.store.get_control(journal.parent).generation == 2
        assert journal.store.get_final(chain.chain_id).wake_receipt == final.notice_id


def test_abandoned_wake_attempt_fences_its_turn_without_losing_the_logical_input(journal):
    from tests.stores.test_subagent_retry_store import _finish

    chain = _open(journal)
    final = _finish(journal, chain)
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    first = InputExecutionClaim(
        input_id=final.notice_id, kind="wake", turn_id="abandoned-wake-turn"
    )
    resolved = journal.store.resolve_execution(
        journal.parent, first, runner_id=journal.owner.runner_id, close=True
    )
    assert resolved.reason == "wake_attempt_abandoned" and resolved.receipt.state == "dispatching"
    assert (
        journal.store.admit_execution(
            journal.parent, first, runner_id=journal.owner.runner_id
        ).reason
        == "wake_attempt_abandoned"
    )
    second = first.model_copy(update={"turn_id": "replacement-wake-turn"})
    admitted = journal.store.admit_execution(
        journal.parent, second, runner_id=journal.owner.runner_id
    )
    assert admitted.accepted and admitted.receipt.input_id == final.notice_id
    assert (
        journal.store.resolve_execution(
            journal.parent, second, runner_id=journal.owner.runner_id, close=True
        )
        == admitted
    )
    assert journal.store.get_final(chain.chain_id).wake_receipt == final.notice_id


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("wake_phase", ["prepared", "dispatching", "accepted"])
def test_recorded_succession_recovers_after_old_runner_unbinds(
    journal, split_db_conversation_store, split, wake_phase
):
    from tests.stores.test_subagent_retry_store import _finish

    if split:
        conversations = split_db_conversation_store
        journal = _journal(conversations.storage_location, conversations)
        journal.store = type(journal.store)(
            conversations.storage_location, conversations.conversation_storage_location
        )
    chain = _open(journal)
    final = _finish(journal, chain)
    original_payload = journal.store.get_input(final.notice_id).payload
    if wake_phase != "prepared":
        assert journal.store.record_inbox(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
        assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    if wake_phase == "accepted":
        assert journal.store.admit_execution(
            journal.parent,
            InputExecutionClaim(input_id=final.notice_id, kind="wake", turn_id="old-parent-wake"),
            runner_id=journal.owner.runner_id,
        ).accepted
    successor = journal.conversations.create_conversation(
        title="successor", runner_id="successor-runner"
    )
    owner = journal.store.claim_owner(successor.id, "successor-runner", expected_epoch=0)
    chain = journal.store.get_chain(chain.chain_id)
    assert (
        journal.store.adopt_parent(chain.chain_id, successor=owner, expected_version=chain.version)
        is None
    )
    assert not journal.store.list_chains(owner=owner)
    direct, _moved = journal.conversations.reassign_live_children(
        journal.parent, successor.id, uuid4().hex
    )
    assert direct == [journal.child]
    journal.conversations.clear_runner_id(journal.parent)
    [listed] = journal.store.list_chains(owner=owner)
    assert listed.chain_id == chain.chain_id
    adopted = journal.store.adopt_parent(
        chain.chain_id, successor=owner, expected_version=chain.version
    )
    assert adopted.parent_id == successor.id and adopted.owner == owner and adopted.retry_closed
    actual = journal.store.get_final(chain.chain_id)
    assert (actual.notice_id, actual.result_id, actual.outcome) == (
        final.notice_id,
        final.result_id,
        final.outcome,
    )
    assert journal.store.get_input(final.notice_id).payload == original_payload
    assert journal.store.get_input(final.notice_id).target_id == (
        journal.parent if wake_phase == "accepted" else successor.id
    )
    assert not journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.record_inbox(chain.chain_id, owner=owner, result_id=final.result_id)
    if wake_phase == "accepted":
        assert actual.wake_receipt == final.notice_id
        assert not journal.store.claim_input(final.notice_id, owner=owner).claimed
        return
    assert journal.store.claim_input(final.notice_id, owner=owner).claimed
    assert (
        journal.store.admit_execution(
            journal.parent,
            InputExecutionClaim(
                input_id=final.notice_id, kind="wake", turn_id="late-old-parent-wake"
            ),
            runner_id=journal.owner.runner_id,
        ).reason
        == "stale_runner"
    )
    assert journal.store.admit_execution(
        successor.id,
        InputExecutionClaim(input_id=final.notice_id, kind="wake", turn_id="successor-wake"),
        runner_id=owner.runner_id,
    ).accepted


def _prepared(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    claim = journal.store.claim_input(receipt.input_id, owner=journal.owner)
    assert claim.claimed
    guard = RetryExecutionGuard(
        chain_id=chain.chain_id,
        input_id=receipt.input_id,
        owner=journal.owner,
        expected_turn_id=chain.expected_turn_id,
        expected_input_generation=chain.expected_input_generation,
        attempt=receipt.attempt,
        of=chain.limit,
    )
    return chain, InputExecutionClaim(
        input_id=receipt.input_id, turn_id="actual-harness-turn", retry=guard
    )


def test_claimed_transport_does_not_count_until_final_receiver_admission(journal):
    chain, claim = _prepared(journal)
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    assert journal.conversations.set_runner_id(journal.child, "child-runner")
    first = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    second = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert first.accepted and second == first
    assert first.receipt.input_generation == 2
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1


@pytest.mark.parametrize("human_first", [False, True])
@pytest.mark.parametrize("control", [False, True])
def test_human_and_retry_have_one_durable_admission_order(journal, human_first, control):
    chain, retry = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    human = InputExecutionClaim(input_id=uuid4().hex, turn_id=None if control else "human-turn")
    claims = [human, retry] if human_first else [retry, human]
    results = [
        journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
        for claim in claims
    ]
    assert results[0].accepted
    assert results[1].accepted is (not human_first)
    control = journal.store.get_control(journal.child)
    assert control.input_id == human.input_id and control.turn_id == human.turn_id
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(not human_first)


@pytest.mark.parametrize("target", ["child", "parent"])
def test_retirement_between_transport_and_consumption_refuses_execution(journal, target):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    journal.conversations.update_conversation(
        getattr(journal, target), archived=True, close_cli_on_archive=True
    )
    receipt = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert not receipt.accepted and receipt.reason == "child_retired"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


def test_rebound_receiver_cannot_accept_a_previously_forwarded_retry(journal):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "replacement-runner")
    receipt = journal.store.admit_execution(journal.child, claim, runner_id="old-runner")
    assert not receipt.accepted and receipt.reason == "stale_runner"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


@pytest.mark.parametrize("split", [False, True])
def test_ap_retirement_cannot_overtake_final_admission(
    journal, split_db_conversation_store, split, monkeypatch
):
    if split:
        conversations = split_db_conversation_store
        journal = _journal(conversations.storage_location, conversations)
        journal.store = type(journal.store)(
            conversations.storage_location, conversations.conversation_storage_location
        )
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    locked, release, archiving = threading.Event(), threading.Event(), threading.Event()
    bindings = journal.store._bindings

    def pause(db, ids):
        locked.set()
        assert release.wait(5)
        return bindings(db, ids)

    monkeypatch.setattr(journal.store, "_bindings", pause)

    def archive():
        archiving.set()
        return journal.conversations.update_conversation(
            journal.parent, archived=True, close_cli_on_archive=True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(
            journal.store.admit_execution, journal.child, claim, runner_id="child-runner"
        )
        assert locked.wait(5)
        retired = pool.submit(archive)
        assert archiving.wait(5)
        try:
            assert not retired.done()
        finally:
            release.set()
        assert accepted.result(timeout=5).accepted
        assert retired.result(timeout=5).archived
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
    after = journal.store.admit_execution(
        journal.child,
        InputExecutionClaim(input_id=uuid4().hex, turn_id="later-input"),
        runner_id="child-runner",
    )
    assert not after.accepted and after.reason == "child_retired"


def test_closed_label_fences_before_execution(journal):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    journal.conversations.set_labels(journal.parent, {"omnigent.closed": "true"})
    result = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert not result.accepted and result.reason == "child_retired"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


@pytest.mark.parametrize("accepted_first", [False, True])
def test_uncertainty_closes_same_input_and_acceptance_wins_when_already_committed(
    journal, accepted_first
):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    pending = journal.store.get_input(claim.input_id)
    if accepted_first:
        assert journal.store.admit_execution(
            journal.child, claim, runner_id="child-runner"
        ).accepted
    closed = journal.store.close_input(
        claim.input_id,
        owner=journal.owner,
        expected_version=pending.version,
        reason={"code": "acceptance_unknown"},
    )
    assert closed.state == ("accepted" if accepted_first else "closed")
    result = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert result.accepted is accepted_first
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(accepted_first)


def test_native_binding_joins_receiver_admission_and_never_overwrites_newer_human(journal):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    assert journal.store.admit_execution(journal.child, claim, runner_id="child-runner").accepted
    human = InputExecutionClaim(input_id=uuid4().hex, turn_id="native-human", native_generation=4)
    assert journal.store.admit_execution(journal.child, human, runner_id="child-runner").accepted
    native = journal.store.bind_native_turn(
        journal.child,
        input_id=claim.input_id,
        turn_id="real-native-retry",
        runner_id="child-runner",
        native_generation=3,
    )
    assert native.accepted and native.receipt.turn_id == "real-native-retry"
    assert journal.store.get_control(journal.child).turn_id == "native-human"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
    stale = journal.store.admit_execution(
        journal.child,
        InputExecutionClaim(input_id=uuid4().hex, turn_id="delayed-native", native_generation=2),
        runner_id="child-runner",
    )
    assert not stale.accepted and stale.reason == "superseded"


def test_native_hook_without_receiver_admission_is_not_execution(journal):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    hook = journal.store.bind_native_turn(
        journal.child,
        input_id=claim.input_id,
        turn_id="native-hook-only",
        runner_id="child-runner",
        native_generation=1,
    )
    assert not hook.accepted and hook.reason == "execution_not_admitted"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


@pytest.mark.parametrize("source", ["rpc", "status"])
def test_native_reservation_commits_only_on_native_acceptance(journal, source):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    reserve = claim.model_copy(update={"boundary": "native_reserve"})
    first = journal.store.admit_execution(journal.child, reserve, runner_id="child-runner")
    assert first.state == "reserved" and not first.accepted
    assert journal.store.get_control(journal.child).turn_id == "root-turn"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    if source == "rpc":
        accept = reserve.model_copy(
            update={"boundary": "native_accept", "native_turn_id": "native-turn"}
        )
        result = journal.store.admit_execution(journal.child, accept, runner_id="child-runner")
        assert (
            journal.store.admit_execution(journal.child, accept, runner_id="child-runner")
            == result
        )
    else:
        result = journal.store.bind_native_turn(
            journal.child,
            input_id=claim.input_id,
            turn_id="native-turn",
            native_generation=2,
            runner_id="child-runner",
        )
    assert result.accepted and result.receipt.turn_id == "native-turn"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
    assert journal.store.get_control(journal.child).turn_id == "native-turn"
    refused = reserve.model_copy(
        update={"boundary": "native_refuse", "native_reason": "late_refusal"}
    )
    assert journal.store.admit_execution(journal.child, refused, runner_id="child-runner").accepted
    assert journal.store.get_final(chain.chain_id) is None


def test_human_wins_before_reserved_native_acceptance(journal):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    reserve = claim.model_copy(update={"boundary": "native_reserve"})
    assert (
        journal.store.admit_execution(journal.child, reserve, runner_id="child-runner").state
        == "reserved"
    )
    human = InputExecutionClaim(input_id=uuid4().hex, turn_id="human-turn", native_generation=2)
    assert journal.store.admit_execution(journal.child, human, runner_id="child-runner").accepted
    late = journal.store.bind_native_turn(
        journal.child,
        input_id=claim.input_id,
        turn_id="late-native",
        runner_id="child-runner",
        native_generation=1,
    )
    assert not late.accepted and late.reason == "superseded"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    assert journal.store.get_control(journal.child).turn_id == "human-turn"


@pytest.mark.parametrize("stale", ["runner", "owner", "human", "guard"])
def test_unreserved_native_refusal_cannot_settle_a_stale_dispatch(journal, stale):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    runner_id = "child-runner"
    if stale == "runner":
        runner_id = "old-runner"
    elif stale == "owner":
        owner = claim.retry.owner.model_copy(update={"epoch": claim.retry.owner.epoch + 1})
        claim = claim.model_copy(update={"retry": claim.retry.model_copy(update={"owner": owner})})
    elif stale == "guard":
        claim = claim.model_copy(
            update={
                "retry": claim.retry.model_copy(update={"expected_turn_id": "another-predecessor"})
            }
        )
    else:
        human = InputExecutionClaim(input_id=uuid4().hex, turn_id="new-human-turn")
        assert journal.store.admit_execution(journal.child, human, runner_id=runner_id).accepted
    before = journal.store.get_control(journal.child)
    refusal = claim.model_copy(
        update={
            "boundary": "native_refuse",
            "native_reason": "agent_unavailable",
            "native_detail": "Connection refused before submission",
        }
    )
    result = journal.store.admit_execution(journal.child, refusal, runner_id=runner_id)
    assert not result.accepted
    assert journal.store.get_input(claim.input_id).state == "dispatching"
    assert journal.store.get_control(journal.child) == before
    assert journal.store.get_final(chain.chain_id) is None
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


def _http_client(journal):
    from omnigent.server.subagent_retry_admission import SubagentRetryAdmission
    from tests.server.routes.test_session_event_batches import _make_client

    client = _make_client(journal.conversations)
    client.app.state.subagent_retry_admission = SubagentRetryAdmission(
        journal.store, journal.conversations, host_store=None, host_registry=None
    )
    return client


@pytest.mark.parametrize("outcome", ["accept", "refuse"])
def test_http_same_id_resolve_preserves_native_outcome(journal, outcome):
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    chain, claim = _prepared(journal)
    journal.conversations.update_conversation(journal.child, harness_override="codex-native")
    token = "native-receiver"
    journal.conversations.set_runner_id(journal.child, token_bound_runner_id(token))
    client = _http_client(journal)
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: token}
    url = f"/sessions/{journal.child}/input-admission"
    reserved = claim.model_copy(update={"boundary": "native_reserve"})
    assert (
        client.post(url, json=reserved.model_dump(), headers=headers).json()["state"] == "reserved"
    )
    observed = claim.model_copy(
        update={
            "boundary": "native_" + outcome,
            "native_turn_id": "codex_native-turn" if outcome == "accept" else None,
            "native_reason": "codex_native_input_refused" if outcome == "refuse" else None,
        }
    )
    first = client.post(url + "/resolve", json=observed.model_dump(), headers=headers)
    repeated = client.post(url + "/resolve", json=observed.model_dump(), headers=headers)
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert first.json()["state"] == ("accepted" if outcome == "accept" else "refused")
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(outcome == "accept")
    if outcome == "refuse":
        assert (
            journal.store.get_final(chain.chain_id).outcome.reason == "codex_native_input_refused"
        )
        closed = client.post(url + "/resolve", json=reserved.model_dump(), headers=headers)
        assert closed.json()["reason"] == "codex_native_input_refused"


def test_http_execution_and_lookup_require_exact_bound_receiver_and_owner(journal):
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    parent_token, child_token = "parent-admission-token", "child-admission-token"
    journal.conversations.clear_runner_id(journal.parent)
    journal.conversations.set_runner_id(journal.parent, token_bound_runner_id(parent_token))
    journal.owner = journal.store.claim_owner(
        journal.parent, token_bound_runner_id(parent_token), expected_epoch=journal.owner.epoch
    )
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, token_bound_runner_id(child_token))
    client = _http_client(journal)
    url = f"/sessions/{journal.child}/input-admission"
    payload = claim.model_dump(mode="json")
    for token in (None, parent_token, "stale-receiver"):
        headers = {RUNNER_TUNNEL_TOKEN_HEADER: token} if token else {}
        assert client.post(url, json=payload, headers=headers).status_code == 403
    accepted = client.post(url, json=payload, headers={RUNNER_TUNNEL_TOKEN_HEADER: child_token})
    assert accepted.status_code == 200 and accepted.json()["accepted"]
    lookup = f"/sessions/{journal.child}/retry-inputs/{claim.input_id}/lookup"
    control = {"owner": journal.owner.model_dump(), "expected_version": 1}
    assert client.post(lookup, json=control).status_code == 403
    assert (
        client.post(
            lookup, json=control, headers={RUNNER_TUNNEL_TOKEN_HEADER: child_token}
        ).status_code
        == 403
    )
    found = client.post(lookup, json=control, headers={RUNNER_TUNNEL_TOKEN_HEADER: parent_token})
    assert found.status_code == 200 and found.json()["state"] == "accepted"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_actual_scaffold_parks_at_receipt_before_calling_the_executor(accepted):
    from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
    from omnigent.server.schemas import CreateResponseRequest, InputExecutionClaimEvent

    ran = []

    class Harness(HarnessApp):
        async def run_turn(self, request, ctx):
            ran.append(ctx.response_id)

    ctx = TurnContext("real-scaffold-turn", asyncio.Queue(), asyncio.Event(), "child")
    harness = Harness()
    task = asyncio.create_task(
        harness._guarded_run_turn(
            CreateResponseRequest(
                model="test", input=[], input_id="input", input_admission_required=True
            ),
            ctx,
        )
    )
    event = await asyncio.wait_for(ctx._event_queue.get(), timeout=1)
    assert isinstance(event, InputExecutionClaimEvent)
    assert event.claim.turn_id == "real-scaffold-turn" and ran == []
    from omnigent.entities.subagent_retry import InputExecutionReceipt

    assert ctx._complete_input_claim(
        event.claim_id,
        InputExecutionReceipt(state="accepted" if accepted else "refused", reason="test"),
    )
    if accepted:
        await task
        assert ran == ["real-scaffold-turn"]
    else:
        with pytest.raises(RuntimeError, match="admission refused"):
            await task
        assert ran == []


@pytest.mark.parametrize("cancel_accepted", [False, True])
def test_control_reservation_blocks_execution_until_actual_delivery_settles(
    journal, cancel_accepted
):
    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    control_id = uuid4().hex
    assert journal.store.begin_control(journal.child, control_id, runner_id="child-runner")
    pending = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert pending.state == "pending" and pending.reason == "control_pending"
    assert journal.store.get_control(journal.child).generation == 1
    assert (
        journal.store.finish_control(journal.child, control_id, accepted=cancel_accepted)
        is cancel_accepted
    )
    settled = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert settled.accepted is (not cancel_accepted)
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(not cancel_accepted)


@pytest.mark.asyncio
async def test_cancelled_waiter_preserves_pending_admission_until_same_id_closure(
    journal, monkeypatch
):
    from omnigent.server.subagent_retry_admission import SubagentRetryAdmission

    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = journal.store.admit_execution

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(journal.store, "admit_execution", delayed)
    service = SubagentRetryAdmission(
        journal.store, journal.conversations, host_store=None, host_registry=None
    )
    waiter = asyncio.create_task(
        service.admit_execution(journal.child, claim, runner_id="child-runner")
    )
    assert await asyncio.to_thread(entered.wait, 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    closed = journal.store.resolve_execution(
        journal.child, claim, runner_id="child-runner", close=True
    )
    assert closed.state == "refused" and closed.receipt.state == "closed"
    release.set()
    assert await asyncio.to_thread(done.wait, 2)
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


def test_resolving_unseen_ordinary_input_tombstones_a_late_worker_without_generation(journal):
    journal.conversations.set_runner_id(journal.child, "child-runner")
    claim = InputExecutionClaim(input_id=uuid4().hex, turn_id="ordinary-turn")
    closed = journal.store.resolve_execution(
        journal.child, claim, runner_id="child-runner", close=True
    )
    assert not closed.accepted
    late = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert not late.accepted and journal.store.get_control(journal.child).generation == 1


@pytest.mark.parametrize("delivery_accepted", [False, True])
def test_real_control_route_holds_retry_fence_across_the_runner_effect(
    journal, monkeypatch, delivery_accepted
):
    from omnigent.errors import ErrorCode
    from omnigent.server.routes.sessions import routes_events
    from omnigent.server.subagent_retry_admission import ControlDeliveryRefused

    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    client = _http_client(journal)
    entered, release = threading.Event(), threading.Event()

    async def deliver(*args, **kwargs):
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        if not delivery_accepted:
            raise ControlDeliveryRefused("Control refused", code=ErrorCode.RUNNER_UNAVAILABLE)

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", deliver)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(
            client.post,
            f"/sessions/{journal.child}/events",
            json={"type": "interrupt", "data": {}},
        )
        try:
            assert entered.wait(3)
            pending = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
            assert pending.state == "pending" and pending.reason == "control_pending"
        finally:
            release.set()
        result = response.result(timeout=5)
    assert (result.status_code < 300) is delivery_accepted
    after = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert after.accepted is (not delivery_accepted)
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(not delivery_accepted)


def test_control_with_lost_runner_response_keeps_the_reservation(journal, monkeypatch):
    import httpx

    from omnigent.server.routes.sessions import routes_events

    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    client = _http_client(journal)
    effects = []

    async def deliver(*args, **kwargs):
        effects.append("interrupt applied")
        raise httpx.ReadTimeout("acknowledgement lost")

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", deliver)
    result = client.post(
        f"/sessions/{journal.child}/events", json={"type": "interrupt", "data": {}}
    )
    assert result.status_code >= 400 and effects == ["interrupt applied"]
    pending = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert pending.state == "pending" and pending.reason == "control_pending"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


def test_control_known_not_sent_releases_the_reservation(journal, monkeypatch):
    from unittest.mock import AsyncMock

    from omnigent.server.routes._sessions import helpers

    chain, claim = _prepared(journal)
    journal.conversations.set_runner_id(journal.child, "child-runner")
    client = _http_client(journal)
    monkeypatch.setattr(helpers, "_get_runner_client", AsyncMock(return_value=None))
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)
    result = client.post(
        f"/sessions/{journal.child}/events", json={"type": "interrupt", "data": {}}
    )
    assert result.status_code >= 400
    assert journal.store.list_controls(journal.child) == []
    assert journal.store.admit_execution(journal.child, claim, runner_id="child-runner").accepted
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1


def test_receiver_receipt_resolves_control_before_its_transport_ack_is_lost(journal, monkeypatch):
    import httpx

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.routes.sessions import routes_events
    from omnigent.server.subagent_retry_admission import control_transport_event

    chain, claim = _prepared(journal)
    token = "control-receiver"
    journal.conversations.set_runner_id(journal.child, token_bound_runner_id(token))
    client = _http_client(journal)
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: token}
    applied = []

    async def deliver(*args, **kwargs):
        reference = control_transport_event({"type": "interrupt"})["input_control"]
        applied.append(reference["input_id"])
        url = f"/sessions/{journal.child}/input-controls/{reference['input_id']}"
        pending = client.get(url, headers=headers)
        assert pending.status_code == 200 and pending.json()["state"] == "prepared"
        settled = client.post(
            url + "/resolve",
            headers=headers,
            json={
                "outcome": "accepted",
                "expected_version": pending.json()["version"],
            },
        )
        assert settled.status_code == 200 and settled.json()["state"] == "accepted"
        raise httpx.ReadTimeout("Original HTTP control acknowledgement lost")

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", deliver)
    result = client.post(
        f"/sessions/{journal.child}/events", json={"type": "interrupt", "data": {}}
    )
    assert result.status_code >= 400 and len(applied) == 1
    assert journal.store.list_controls(journal.child) == []
    assert journal.store.get_control(journal.child).generation == 2
    assert not journal.store.admit_execution(
        journal.child, claim, runner_id=token_bound_runner_id(token)
    ).accepted
    next_input = journal.store.admit_execution(
        journal.child,
        InputExecutionClaim(input_id=uuid4().hex, turn_id="next-human"),
        runner_id=token_bound_runner_id(token),
    )
    assert next_input.accepted and next_input.receipt.input_generation == 3
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


def test_control_unknown_can_close_only_after_old_retry_is_finalized(journal):
    from omnigent.entities.subagent_retry import RetryOutcome
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    chain, claim = _prepared(journal)
    token = "control-recovery-receiver"
    runner_id = token_bound_runner_id(token)
    journal.conversations.set_runner_id(journal.child, runner_id)
    control_id = uuid4().hex
    assert journal.store.begin_control(journal.child, control_id, runner_id=runner_id)
    client = _http_client(journal)
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: token}
    url = f"/sessions/{journal.child}/input-controls/{control_id}/resolve"
    payload = {"outcome": "retired", "expected_version": 0}
    assert client.post(url, json=payload).status_code == 403
    assert client.post(url, json=payload, headers=headers).json()["state"] == "prepared"
    pending = journal.store.get_input(claim.input_id)
    journal.store.close_input(
        claim.input_id,
        owner=journal.owner,
        expected_version=pending.version,
        reason={"code": "acceptance_unknown"},
    )
    current = journal.store.get_chain(chain.chain_id)
    final = journal.store.finalize(
        chain.chain_id,
        owner=journal.owner,
        expected_version=current.version,
        outcome=RetryOutcome(
            status="failed",
            reason="acceptance_unknown",
            output="held error",
            error=current.latest_error,
        ),
        wake_payload={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "held error"}]},
        },
    )
    assert final is not None and final.outcome.error == journal.terminal.error
    closed = client.post(url, json=payload, headers=headers)
    assert closed.status_code == 200 and closed.json()["state"] == "closed"
    assert closed.json()["reason"]["code"] == "control_outcome_unknown"
    assert journal.store.get_control(journal.child).generation == 2
    fresh = journal.store.admit_execution(
        journal.child,
        InputExecutionClaim(input_id=uuid4().hex, turn_id="fresh"),
        runner_id=runner_id,
    )
    assert fresh.accepted and fresh.receipt.input_generation == 3
    assert journal.store.get_final(chain.chain_id) == final
