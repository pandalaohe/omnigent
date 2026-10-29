"""Real journal transactions survive competing replicas and delivery replay."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from omnigent.db import workspace_scope
from omnigent.entities.subagent_retry import RetryOutcome, retry_identity
from omnigent.entities.subagent_terminal import TerminalEnvelope


@pytest.fixture
def journal(db_uri, conversation_store):
    return _journal(db_uri, conversation_store)


def _journal(db_uri, conversation_store):
    from omnigent.stores.subagent_retry_store.sqlalchemy_store import (
        SqlAlchemySubagentRetryStore,
    )

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(parent_conversation_id=parent.id)
    assert conversation_store.set_runner_id(parent.id, "runner-original")
    store = SqlAlchemySubagentRetryStore(db_uri)
    owner = store.claim_owner(parent.id, "runner-original", expected_epoch=0)
    assert owner is not None
    input_id = uuid4().hex
    control = store.record_input(
        child.id,
        input_id=input_id,
        turn_id="root-turn",
        expected_generation=0,
        payload={"text": "original input"},
    )
    assert control is not None
    terminal = TerminalEnvelope.from_data(
        child.id,
        parent_id=parent.id,
        status="failed",
        data={
            "turn_id": "root-turn",
            "input_id": input_id,
            "input_generation": control.generation,
        },
        error={"code": "capacity", "message": "same provider failure"},
    )
    return SimpleNamespace(
        store=store,
        uri=db_uri,
        conversations=conversation_store,
        parent=parent.id,
        child=child.id,
        owner=owner,
        terminal=terminal,
    )


def test_two_replicas_deduplicate_before_moving_deadline_or_counter(journal):
    stores = [journal.store, type(journal.store)(journal.uri)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda store: store.open_chain(
                    journal.terminal,
                    owner=journal.owner,
                    limit=3,
                    observed_at=100,
                ),
                stores,
            )
        )
    assert all(receipt is not None for receipt in receipts)
    assert sorted(receipt.duplicate for receipt in receipts) == [False, True]
    assert len({receipt.chain.chain_id for receipt in receipts}) == 1
    original = receipts[0].chain
    replay = stores[1].open_chain(
        journal.terminal,
        owner=journal.owner,
        limit=99,
        observed_at=500,
    )
    assert replay.duplicate and replay.chain == original
    assert original.due_at == 160 and original.limit == 3
    assert original.accepted_attempts == 0


def _open(journal, limit=3):
    return journal.store.open_chain(
        journal.terminal, owner=journal.owner, limit=limit, observed_at=100
    ).chain


def _finish(journal, chain, *, reason="host_offline", status="failed"):
    return journal.store.finalize(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        outcome=RetryOutcome(status=status, reason=reason, output="child result"),
        wake_payload={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "child finished"}],
            },
        },
    )


def test_preparation_claim_and_acceptance_are_distinct_durable_phases(journal):
    chain = _open(journal)
    assert (
        journal.store.prepare_attempt(
            chain.chain_id, owner=journal.owner, expected_version=chain.version, now=159.9
        )
        is None
    )
    stores = [journal.store, type(journal.store)(journal.uri)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = list(
            pool.map(
                lambda store: store.prepare_attempt(
                    chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
                ),
                stores,
            )
        )
    [receipt] = [value for value in prepared if value is not None]
    assert receipt.input_id == retry_identity("subagent-retry-input", chain.chain_id, "1")
    assert receipt.state == "prepared" and receipt.attempt == 1
    assert receipt.payload["data"]["content"] == [{"type": "input_text", "text": "retry"}]
    assert receipt.payload["data"]["auto_retry"] == {
        "chain_id": chain.chain_id,
        "input_id": receipt.input_id,
        "attempt": 1,
        "of": 3,
    }
    assert stores[1].get_input(receipt.input_id) == receipt
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda store: store.claim_input(receipt.input_id, owner=journal.owner), stores
            )
        )
    assert sorted(value.claimed for value in claims) == [False, True]
    assert all(value.receipt.state == "dispatching" for value in claims)
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    first = stores[0].record_acceptance(receipt.input_id, owner=journal.owner, turn_id="retry-1")
    second = stores[1].record_acceptance(receipt.input_id, owner=journal.owner, turn_id="retry-1")
    assert first == second and first.state == "accepted" and first.input_generation == 2
    current = stores[1].get_chain(chain.chain_id)
    assert current.phase == "running" and current.accepted_attempts == 1
    assert current.expected_turn_id == "retry-1" and current.expected_input_generation == 2
    with pytest.raises(ValueError, match="another turn"):
        journal.store.record_acceptance(receipt.input_id, owner=journal.owner, turn_id="forged")


@pytest.mark.parametrize("limit", [1, 3, 5])
def test_fast_terminal_accounts_once_and_same_error_does_not_merge_turns(journal, limit):
    chain = _open(journal, limit)
    for attempt in range(1, limit + 1):
        receipt = journal.store.prepare_attempt(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            now=chain.due_at,
        )
        assert receipt is not None
        terminal = journal.terminal.model_copy(
            update={
                "input_id": receipt.input_id,
                "turn_id": f"retry-{attempt}",
                "input_generation": attempt + 1,
            }
        )
        observed = 100 + attempt * 100
        event = journal.store.record_terminal(
            chain.chain_id,
            terminal,
            owner=journal.owner,
            observed_at=observed,
        )
        chain = event.chain
        assert not event.duplicate and chain.accepted_attempts == attempt
        duplicate = journal.store.record_terminal(
            chain.chain_id,
            terminal,
            owner=journal.owner,
            observed_at=observed + 999,
        )
        assert duplicate.duplicate and duplicate.chain == chain
        accepted = journal.store.record_acceptance(
            receipt.input_id,
            owner=journal.owner,
            turn_id=terminal.turn_id,
        )
        assert accepted.state == "accepted"
        assert journal.store.get_chain(chain.chain_id) == chain
        if attempt < limit:
            assert chain.phase == "waiting" and chain.due_at == observed + 60
        else:
            assert chain.phase == "terminal" and chain.due_at is None and chain.retry_closed
    assert (
        journal.store.prepare_attempt(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            now=9999,
        )
        is None
    )
    final = _finish(journal, chain, reason="exhausted")
    assert final.attempts == limit and final.limit == limit
    assert final.outcome.error == journal.terminal.error
    assert journal.store.record_terminal(
        chain.chain_id,
        journal.terminal,
        owner=journal.owner,
        observed_at=9999,
    ).duplicate
    assert journal.store.get_final(chain.chain_id) == final


def test_close_and_accept_compete_without_counting_an_unknown_request(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    stores = [journal.store, type(journal.store)(journal.uri)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        accept = pool.submit(
            stores[0].record_acceptance,
            receipt.input_id,
            owner=journal.owner,
            turn_id="maybe-accepted",
        )
        close = pool.submit(
            stores[1].close_input,
            receipt.input_id,
            owner=journal.owner,
            expected_version=receipt.version,
            reason={"code": "timeout"},
        )
        accepted, closed = accept.result(), close.result()
    persisted = stores[1].get_input(receipt.input_id)
    chain = stores[1].get_chain(chain.chain_id)
    if persisted.state == "accepted":
        assert accepted == closed == persisted
        assert chain.accepted_attempts == 1
    else:
        assert persisted.state == "closed" and accepted is None
        assert closed == persisted and chain.accepted_attempts == 0
        assert chain.retry_closed
        assert (
            stores[0].record_acceptance(
                receipt.input_id,
                owner=journal.owner,
                turn_id="too-late",
            )
            is None
        )


def test_unresolved_admission_blocks_finalization_until_atomically_closed(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    chain = journal.store.get_chain(chain.chain_id)
    with pytest.raises(ValueError, match="Resolve or close"):
        _finish(journal, chain, reason="acceptance_unknown")
    assert journal.store.get_final(chain.chain_id) is None
    closed = journal.store.close_input(
        receipt.input_id,
        owner=journal.owner,
        expected_version=receipt.version,
        reason={"code": "acceptance_unknown", "message": "receipt unavailable"},
    )
    assert closed.state == "closed"
    chain = journal.store.get_chain(chain.chain_id)
    final = _finish(journal, chain, reason="acceptance_unknown")
    assert final.attempts == 0 and final.outcome.reason == "acceptance_unknown"
    assert journal.store.claim_input(receipt.input_id, owner=journal.owner).claimed is False


@pytest.mark.parametrize("restart_phase", ["final", "inbox", "dispatching", "accepted", "drained"])
def test_final_outbox_and_drain_resume_with_frozen_input_identity(journal, restart_phase):
    chain = _open(journal)
    final = _finish(journal, chain)
    notice = journal.store.get_input(final.notice_id)
    assert notice.payload["data"]["stable_id"] == final.notice_id
    assert final.result_id != final.notice_id
    assert not journal.store.record_drain(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    if restart_phase != "final":
        assert journal.store.record_inbox(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
    if restart_phase in {"dispatching", "accepted", "drained"}:
        assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    if restart_phase in {"accepted", "drained"}:
        assert (
            journal.store.record_acceptance(
                final.notice_id,
                owner=journal.owner,
                turn_id="parent-wake",
            ).state
            == "accepted"
        )
    if restart_phase == "drained":
        assert journal.store.record_drain(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
    restored = type(journal.store)(journal.uri)
    replay = restored.finalize(
        chain.chain_id,
        owner=journal.owner,
        expected_version=0,
        outcome=RetryOutcome(status="failed", reason="later_diagnostic", output="changed"),
        wake_payload={"type": "message", "data": {"content": ["must not replace body"]}},
    )
    assert replay.result_id == final.result_id and replay.notice_id == final.notice_id
    assert replay.outcome == final.outcome
    assert restored.get_input(final.notice_id).payload == notice.payload
    if restart_phase == "drained":
        assert restored.list_outbox(owner=journal.owner) == []
        assert not restored.claim_input(final.notice_id, owner=journal.owner).claimed
    else:
        assert [entry.result_id for entry in restored.list_outbox(owner=journal.owner)] == [
            final.result_id
        ]
        assert restored.record_inbox(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
        claim = restored.claim_input(final.notice_id, owner=journal.owner)
        assert claim.claimed == (restart_phase in {"final", "inbox"})
        assert restored.record_acceptance(
            final.notice_id, owner=journal.owner, turn_id="parent-wake"
        )
        assert restored.record_drain(
            chain.chain_id, owner=journal.owner, result_id=final.result_id
        )
        assert restored.list_outbox(owner=journal.owner) == []


def test_final_outcome_and_outbox_are_one_transaction(journal):
    chain = _open(journal)
    engine = journal.store._session.engine

    def lose_outbox(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO subagent_retry_inputs"):
            raise RuntimeError("induced final outbox write loss")

    event.listen(engine, "before_cursor_execute", lose_outbox)
    try:
        with pytest.raises(RuntimeError, match="induced final outbox"):
            _finish(journal, chain)
    finally:
        event.remove(engine, "before_cursor_execute", lose_outbox)
    assert journal.store.get_chain(chain.chain_id) == chain
    assert journal.store.get_final(chain.chain_id) is None
    assert journal.store.get_input(retry_identity("subagent-final", chain.chain_id)) is None
    assert _finish(journal, chain) is not None


@pytest.mark.parametrize("runner", ["runner-original", "replacement-runner"])
def test_binding_and_epoch_fence_stale_owner_and_recovery_never_rearms(journal, runner):
    chain = _open(journal)
    assert journal.store.claim_owner(journal.parent, "not-the-binding", expected_epoch=1) is None
    if runner != "runner-original":
        journal.conversations.replace_runner_id(journal.parent, runner)
    current = journal.store.claim_owner(journal.parent, runner, expected_epoch=1)
    assert current.epoch == 2
    assert (
        journal.store.prepare_attempt(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            now=160,
        )
        is None
    )
    assert (
        journal.store.prepare_attempt(
            chain.chain_id,
            owner=current,
            expected_version=chain.version,
            now=160,
        )
        is None
    )
    recovered = journal.store.take_over(
        chain.chain_id, owner=current, expected_version=chain.version
    )
    assert recovered.retry_closed and recovered.phase == "resolving" and recovered.due_at is None
    assert (
        journal.store.prepare_attempt(
            chain.chain_id,
            owner=current,
            expected_version=recovered.version,
            now=9999,
        )
        is None
    )
    assert (
        journal.store.finalize(
            chain.chain_id,
            owner=journal.owner,
            expected_version=recovered.version,
            outcome=RetryOutcome(status="failed", reason="runner_restarted", output="old owner"),
            wake_payload={"type": "message", "data": {}},
        )
        is None
    )
    journal.owner = current
    final = _finish(journal, recovered, reason="runner_restarted")
    assert final is not None


def test_recovered_success_stays_success_and_late_failure_cannot_rewrite_it(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    completed = journal.terminal.model_copy(
        update={
            "input_id": receipt.input_id,
            "turn_id": "completed-retry",
            "input_generation": 2,
            "status": "completed",
            "error": None,
        }
    )
    chain = journal.store.record_terminal(
        chain.chain_id,
        completed,
        owner=journal.owner,
        observed_at=170,
    ).chain
    current = journal.store.claim_owner(journal.parent, "runner-original", expected_epoch=1)
    chain = journal.store.take_over(chain.chain_id, owner=current, expected_version=chain.version)
    journal.owner = current
    with pytest.raises(ValueError, match="successful turn"):
        _finish(journal, chain, reason="runner_restarted")
    final = _finish(journal, chain, status="completed", reason="completed")
    late = journal.store.record_terminal(
        chain.chain_id,
        completed.model_copy(update={"status": "failed", "error": {"message": "late"}}),
        owner=current,
        observed_at=999,
    )
    assert late.chain.phase == "finalized" and late.chain.retry_closed
    assert journal.store.get_final(chain.chain_id) == final


def test_accepted_replacement_supersedes_while_rejected_input_cannot(journal):
    chain = _open(journal)
    pending = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    chain = journal.store.get_chain(chain.chain_id)
    assert (
        journal.store.supersede(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            replacement_input_id=pending.input_id,
        )
        is None
    )
    replacement_id = uuid4().hex
    accepted = journal.store.record_input(
        journal.child,
        input_id=replacement_id,
        turn_id="replacement",
        expected_generation=1,
        payload={"text": "human intervention"},
    )
    assert accepted.generation == 2
    assert _finish(journal, chain) is None
    assert (
        journal.store.record_input(
            journal.child,
            input_id=replacement_id,
            turn_id="replacement",
            expected_generation=1,
            payload={"text": "human intervention"},
        )
        == accepted
    )
    superseded = journal.store.supersede(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        replacement_input_id=replacement_id,
    )
    assert superseded.phase == "superseded" and superseded.retry_closed
    assert journal.store.get_input(pending.input_id).state == "closed"
    assert (
        journal.store.record_acceptance(pending.input_id, owner=journal.owner, turn_id="stale")
        is None
    )
    assert journal.store.get_final(chain.chain_id) is None


def test_successor_retains_final_and_input_identity_before_and_after_wake(journal):
    chain = _open(journal)
    final = _finish(journal, chain)
    original_body = journal.store.get_input(final.notice_id).payload
    successor = journal.conversations.create_conversation(runner_id="successor-runner")
    successor_owner = journal.store.claim_owner(successor.id, "successor-runner", expected_epoch=0)
    chain = journal.store.get_chain(chain.chain_id)
    transferred = journal.store.transfer_parent(
        chain.chain_id,
        owner=journal.owner,
        successor=successor_owner,
        expected_version=chain.version,
    )
    assert transferred.parent_id == successor.id
    resumed = journal.store.get_final(chain.chain_id)
    assert (resumed.result_id, resumed.notice_id) == (final.result_id, final.notice_id)
    assert journal.store.get_input(final.notice_id).payload == original_body
    assert journal.store.get_input(final.notice_id).target_id == successor.id
    assert not journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.record_inbox(
        chain.chain_id, owner=successor_owner, result_id=final.result_id
    )
    assert journal.store.claim_input(final.notice_id, owner=successor_owner).claimed
    assert journal.store.record_acceptance(final.notice_id, owner=successor_owner, turn_id="wake")
    assert not journal.store.claim_input(final.notice_id, owner=successor_owner).claimed
    assert journal.store.record_drain(
        chain.chain_id, owner=successor_owner, result_id=final.result_id
    )
    assert journal.store.list_outbox(owner=successor_owner) == []


def test_workspace_and_source_identity_are_not_guessed(journal):
    chain = _open(journal)
    final = _finish(journal, chain)
    with workspace_scope(987):
        assert journal.store.get_chain(chain.chain_id) is None
        assert journal.store.get_input(final.notice_id) is None
        assert journal.store.get_final(chain.chain_id) is None
        assert journal.store.get_owner(journal.parent) is None
        assert (
            journal.store.claim_owner(journal.parent, "runner-original", expected_epoch=0) is None
        )
        assert journal.store.list_outbox(owner=journal.owner) == []
    with pytest.raises(ValueError, match="identified admitted"):
        journal.store.open_chain(
            journal.terminal.model_copy(update={"turn_id": None}),
            owner=journal.owner,
            limit=3,
            observed_at=100,
        )
    assert (
        journal.store.open_chain(
            journal.terminal.model_copy(update={"recovered": True}),
            owner=journal.owner,
            limit=3,
            observed_at=100,
        )
        is None
    )


def test_later_independent_same_error_turn_has_a_new_chain_and_old_tombstone(journal):
    old = _open(journal)
    _finish(journal, old)
    input_id = uuid4().hex
    control = journal.store.record_input(
        journal.child,
        input_id=input_id,
        turn_id="later-independent-turn",
        expected_generation=1,
        payload={"text": "next input"},
    )
    terminal = journal.terminal.model_copy(
        update={
            "turn_id": control.turn_id,
            "input_id": input_id,
            "input_generation": control.generation,
        }
    )
    current = journal.store.open_chain(
        terminal, owner=journal.owner, limit=3, observed_at=200
    ).chain
    assert current.chain_id != old.chain_id and current.accepted_attempts == 0
    duplicate = journal.store.open_chain(
        journal.terminal, owner=journal.owner, limit=3, observed_at=999
    )
    assert duplicate.duplicate and duplicate.chain.chain_id == old.chain_id
    assert journal.store.get_chain(current.chain_id) == current


def test_finalization_competitors_freeze_one_logical_notice(journal):
    chain = _open(journal)
    stores = [journal.store, type(journal.store)(journal.uri)]

    def finalize(index):
        return stores[index].finalize(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            outcome=RetryOutcome(
                status="failed", reason=f"reason-{index}", output=f"result-{index}"
            ),
            wake_payload={"type": "message", "data": {"content": [f"notice-{index}"]}},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(finalize, range(2)))
    assert first == second
    assert first.outcome.reason in {"reason-0", "reason-1"}
    assert len(journal.store.list_outbox(owner=journal.owner)) == 1
    assert journal.store.claim_input(first.notice_id, owner=journal.owner) is None
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=first.result_id
    )
    assert journal.store.record_drain(
        chain.chain_id, owner=journal.owner, result_id=first.result_id
    )
    assert journal.store.get_input(first.notice_id).state == "closed"
    assert not journal.store.claim_input(first.notice_id, owner=journal.owner).claimed


def test_claim_epoch_compare_and_swap_has_one_winner(journal):
    stores = [journal.store, type(journal.store)(journal.uri)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(
            pool.map(
                lambda store: store.claim_owner(
                    journal.parent,
                    "runner-original",
                    expected_epoch=1,
                ),
                stores,
            )
        )
    [winner] = [owner for owner in owners if owner is not None]
    assert winner.epoch == 2 and journal.store.get_owner(journal.parent) == winner


def test_control_input_replay_cannot_advance_or_rewind_generation(journal):
    original = journal.store.get_control(journal.child)
    next_id = uuid4().hex
    updated = journal.store.record_input(
        journal.child,
        input_id=next_id,
        turn_id=None,
        expected_generation=1,
        payload={"control": "cancel"},
    )
    assert updated.generation == 2 and updated.turn_id is None
    replayed = journal.store.record_input(
        journal.child,
        input_id=original.input_id,
        turn_id=original.turn_id,
        expected_generation=0,
        payload={"text": "original input"},
    )
    assert replayed == original
    assert journal.store.get_control(journal.child) == updated
    with pytest.raises(ValueError, match="another admission"):
        journal.store.record_input(
            journal.child,
            input_id=next_id,
            turn_id=None,
            expected_generation=2,
            payload={"control": "changed body"},
        )
    assert journal.store.get_control(journal.child) == updated


def test_accepted_input_wins_a_later_uncertainty_close(journal):
    chain = _open(journal)
    pending = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    accepted = journal.store.record_acceptance(
        pending.input_id, owner=journal.owner, turn_id="turn"
    )
    assert (
        journal.store.close_input(
            pending.input_id,
            owner=journal.owner,
            expected_version=pending.version,
            reason={"code": "lost_response"},
        )
        == accepted
    )
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1


def test_successor_does_not_reinject_a_wake_already_admitted(journal):
    chain = _open(journal)
    final = _finish(journal, chain)
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.record_acceptance(
        final.notice_id, owner=journal.owner, turn_id="already-woke"
    )
    successor = journal.conversations.create_conversation(runner_id="new-parent")
    new_owner = journal.store.claim_owner(successor.id, "new-parent", expected_epoch=0)
    chain = journal.store.get_chain(chain.chain_id)
    journal.store.transfer_parent(
        chain.chain_id,
        owner=journal.owner,
        successor=new_owner,
        expected_version=chain.version,
    )
    resumed = journal.store.get_final(chain.chain_id)
    assert resumed.notice_id == final.notice_id and resumed.wake_receipt == final.notice_id
    assert journal.store.get_input(final.notice_id).target_id == journal.parent
    assert journal.store.record_inbox(chain.chain_id, owner=new_owner, result_id=final.result_id)
    assert not journal.store.claim_input(final.notice_id, owner=new_owner).claimed
    assert journal.store.record_drain(chain.chain_id, owner=new_owner, result_id=final.result_id)


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_owner_recovery_reconciles_pending_terminal_without_rearming(journal, claimed, status):
    chain = _open(journal)
    pending = journal.store.prepare_attempt(
        chain.chain_id,
        owner=journal.owner,
        expected_version=chain.version,
        now=160,
    )
    if claimed:
        assert journal.store.claim_input(pending.input_id, owner=journal.owner).claimed
    chain = journal.store.get_chain(chain.chain_id)
    current = journal.store.claim_owner(journal.parent, "runner-original", expected_epoch=1)
    recovered = journal.store.take_over(
        chain.chain_id, owner=current, expected_version=chain.version
    )
    assert recovered.retry_closed and recovered.phase == "resolving"
    resumed_claim = journal.store.claim_input(pending.input_id, owner=current)
    if claimed:
        assert not resumed_claim.claimed
    else:
        assert resumed_claim is None
    terminal = journal.terminal.model_copy(
        update={
            "input_id": pending.input_id,
            "turn_id": "finished-before-restart",
            "input_generation": 2,
            "status": status,
            "recovered": True,
        }
    )
    reconciled = journal.store.record_terminal(
        chain.chain_id,
        terminal,
        owner=current,
        observed_at=200,
    ).chain
    assert reconciled.accepted_attempts == 1
    assert reconciled.phase == "terminal" and reconciled.retry_closed and reconciled.due_at is None
    journal.owner = current
    final = _finish(
        journal,
        reconciled,
        status=status,
        reason="runner_restarted" if status == "failed" else "completed",
    )
    assert final.outcome.status == status and final.attempts == 1


def test_replacement_terminal_before_supersede_cannot_poison_its_own_chain(journal):
    old = _open(journal)
    input_id = uuid4().hex
    control = journal.store.record_input(
        journal.child,
        input_id=input_id,
        turn_id="replacement-failed",
        expected_generation=1,
        payload={"text": "replacement"},
    )
    terminal = journal.terminal.model_copy(
        update={
            "turn_id": control.turn_id,
            "input_id": input_id,
            "input_generation": control.generation,
        }
    )
    assert (
        journal.store.record_terminal(
            old.chain_id,
            terminal,
            owner=journal.owner,
            observed_at=200,
        )
        is None
    )
    assert journal.store.get_chain(old.chain_id) == old
    new = journal.store.open_chain(terminal, owner=journal.owner, limit=3, observed_at=200)
    assert not new.duplicate and new.chain.chain_id != old.chain_id
    assert new.chain.root_turn_id == terminal.turn_id and new.chain.accepted_attempts == 0


@pytest.mark.parametrize("source", ["root", "retry"])
def test_late_terminal_requires_the_persisted_input_turn_and_generation(journal, source):
    chain = _open(journal)
    terminal = journal.terminal
    if source == "retry":
        pending = journal.store.prepare_attempt(
            chain.chain_id,
            owner=journal.owner,
            expected_version=chain.version,
            now=160,
        )
        terminal = terminal.model_copy(
            update={
                "input_id": pending.input_id,
                "turn_id": "retry-failed",
                "input_generation": 2,
            }
        )
        chain = journal.store.record_terminal(
            chain.chain_id,
            terminal,
            owner=journal.owner,
            observed_at=200,
        ).chain
    final = _finish(journal, chain)
    assert (
        journal.store.record_terminal(
            chain.chain_id,
            terminal.model_copy(update={"status": "completed", "input_generation": 999}),
            owner=journal.owner,
            observed_at=999,
        )
        is None
    )
    valid = journal.store.record_terminal(
        chain.chain_id,
        terminal.model_copy(update={"status": "completed"}),
        owner=journal.owner,
        observed_at=999,
    )
    assert not valid.duplicate and journal.store.get_final(chain.chain_id) == final


def test_wake_admission_is_a_real_input_generation_for_a_nested_parent(journal):
    child_chain = _open(journal)
    child_final = _finish(journal, child_chain)
    assert journal.store.record_inbox(
        child_chain.chain_id, owner=journal.owner, result_id=child_final.result_id
    )
    admitted = journal.store.record_acceptance(
        child_final.notice_id,
        owner=journal.owner,
        turn_id="parent-wake-turn",
    )
    control = journal.store.get_control(journal.parent)
    assert control is not None and admitted.input_generation == control.generation == 1
    assert control.input_id == child_final.notice_id and control.turn_id == "parent-wake-turn"
    grandparent = journal.conversations.create_conversation(runner_id="grandparent-runner")
    owner = journal.store.claim_owner(grandparent.id, "grandparent-runner", expected_epoch=0)
    parent_terminal = TerminalEnvelope.from_data(
        journal.parent,
        parent_id=grandparent.id,
        status="failed",
        data={
            "turn_id": control.turn_id,
            "input_id": control.input_id,
            "input_generation": control.generation,
        },
        error={"message": "parent also failed"},
    )
    parent_chain = journal.store.open_chain(parent_terminal, owner=owner, limit=3, observed_at=200)
    assert parent_chain.chain.chain_id != child_chain.chain_id
    assert parent_chain.chain.accepted_attempts == 0
    assert journal.store.get_chain(child_chain.chain_id).accepted_attempts == 0


def test_delayed_wake_ack_does_not_overwrite_newer_human_admission(journal):
    chain = _open(journal)
    final = _finish(journal, chain)
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    human = journal.store.record_input(
        journal.parent,
        input_id=uuid4().hex,
        turn_id="human-won",
        expected_generation=0,
        payload={"text": "human input before wake acknowledgement"},
    )
    assert human.generation == 1
    assert (
        journal.store.record_acceptance(
            final.notice_id,
            owner=journal.owner,
            turn_id="old-wake",
        )
        is None
    )
    assert journal.store.get_control(journal.parent) == human
    assert journal.store.get_input(final.notice_id).state == "dispatching"
    assert journal.store.get_final(chain.chain_id).wake_receipt is None


def test_prepared_wake_captures_current_generation_when_dispatch_is_claimed(journal):
    chain = _open(journal)
    final = _finish(journal, chain)
    human = journal.store.record_input(
        journal.parent,
        input_id=uuid4().hex,
        turn_id="human-first",
        expected_generation=0,
        payload={"text": "human before wake is dispatched"},
    )
    assert journal.store.record_inbox(
        chain.chain_id, owner=journal.owner, result_id=final.result_id
    )
    assert journal.store.claim_input(final.notice_id, owner=journal.owner).claimed
    accepted = journal.store.record_acceptance(
        final.notice_id, owner=journal.owner, turn_id="wake-after-human"
    )
    assert accepted.input_generation == human.generation + 1
    assert journal.store.get_control(journal.parent).turn_id == "wake-after-human"
    newer = journal.store.record_input(
        journal.parent,
        input_id=uuid4().hex,
        turn_id="newer-human",
        expected_generation=2,
        payload={"text": "human after accepted wake"},
    )
    assert (
        journal.store.record_acceptance(
            final.notice_id, owner=journal.owner, turn_id="wake-after-human"
        )
        == accepted
    )
    assert journal.store.get_control(journal.parent) == newer
