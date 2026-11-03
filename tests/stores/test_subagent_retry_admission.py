"""Preflight and uncertainty reads use persisted identities, never message text."""

import asyncio
import threading
import time
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnigent.db import workspace_scope
from omnigent.server.host_registry import HostRegistry
from omnigent.server.subagent_retry_admission import SubagentRetryAdmission
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S, Host, HostStore
from tests.stores.test_subagent_retry_store import _open
from tests.stores.test_subagent_retry_store import journal as journal

pytestmark = pytest.mark.asyncio


def _admission(journal, *, hosts=None, registry=None):
    return SubagentRetryAdmission(
        journal.store, journal.conversations, host_store=hosts, host_registry=registry
    )


async def test_server_local_child_needs_a_valid_root_but_no_runner_liveness(journal):
    chain = _open(journal)
    result = await _admission(journal).check_before_retry(chain)
    assert result.ready and result.host_online is True and result.effective_host_id is None
    assert journal.conversations.get_conversation(journal.child).runner_id is None
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


async def test_final_forward_derives_marker_from_the_claimed_receipt(journal):
    from omnigent.entities.subagent_retry import RetryExecutionGuard
    from omnigent.server.subagent_retry_admission import claim_retry_forward, retry_dispatch_scope

    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    claimed = journal.store.claim_input(receipt.input_id, owner=journal.owner)
    assert claimed.claimed
    guard = RetryExecutionGuard(
        chain_id=chain.chain_id,
        input_id=receipt.input_id,
        owner=journal.owner,
        expected_turn_id=chain.expected_turn_id,
        expected_input_generation=chain.expected_input_generation,
        attempt=1,
        of=3,
    )
    event = {"type": "message", "auto_retry": {"attempt": 999}}
    with retry_dispatch_scope(_admission(journal), guard, claimed_version=claimed.receipt.version):
        await claim_retry_forward(journal.child, event, deadline=time.time() + 30)
    assert event["auto_retry"] == {
        "chain_id": chain.chain_id,
        "input_id": receipt.input_id,
        "attempt": 1,
        "of": 3,
    }
    assert event["input_id"] == receipt.input_id
    assert event["retry_guard"] == guard.model_dump(mode="json")
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


@pytest.mark.parametrize("local_connection", [False, True])
@pytest.mark.parametrize("health", ["fresh", "stale", "offline", "missing", "deleted"])
async def test_nearest_ancestor_host_uses_store_ttl_even_on_its_own_replica(
    journal, local_connection, health
):
    host_id = uuid4().hex
    journal.conversations.set_host_id(journal.parent, host_id, "/opt/work/project")
    now = int(time.time())
    host = Host(host_id, "test-host", "local", "online", now, now)
    if health == "stale":
        host = replace(host, updated_at=now - HOST_LIVENESS_TTL_S - 1)
    elif health == "offline":
        host = replace(host, status="offline")
    elif health == "deleted":
        host = replace(host, deleted_at=now)
    hosts, registry = Mock(spec=HostStore), Mock(spec=HostRegistry)
    hosts.get_host.return_value = None if health == "missing" else host
    registry.get.return_value = object() if local_connection else None
    result = await _admission(journal, hosts=hosts, registry=registry).check_before_retry(
        _open(journal)
    )
    assert result.ready is (health == "fresh")
    assert result.host_online is (health == "fresh")
    assert result.effective_host_id == host_id
    hosts.get_host.assert_called_once_with(host_id)
    registry.get.assert_not_called()


async def test_child_host_takes_precedence_but_ancestor_retirement_still_blocks(journal):
    hosts = HostStore(journal.uri)
    child_host, parent_host = uuid4().hex, uuid4().hex
    hosts.upsert_on_connect(child_host, "child-host", "local")
    hosts.upsert_on_connect(parent_host, "parent-host", "local")
    hosts.set_offline(parent_host)
    journal.conversations.set_host_id(journal.child, child_host, "/opt/work/child")
    journal.conversations.set_host_id(journal.parent, parent_host, "/opt/work/parent")
    chain, admission = _open(journal), _admission(journal, hosts=hosts)
    result = await admission.check_before_retry(chain)
    assert result.ready and result.effective_host_id == child_host
    journal.conversations.update_conversation(
        journal.parent, archived=True, close_cli_on_archive=True
    )
    result = await admission.check_before_retry(chain)
    assert not result.ready and result.reason == "child_retired"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


@pytest.mark.parametrize("connected", [False, True])
async def test_registry_fallback_is_used_only_without_a_host_store(journal, connected):
    host_id = uuid4().hex
    journal.conversations.set_host_id(journal.parent, host_id, "/opt/work/project")
    chain, registry = _open(journal), Mock(spec=HostRegistry)
    registry.get.return_value = object() if connected else None
    result = await _admission(journal, registry=registry).check_before_retry(chain)
    assert result.ready is connected and result.host_online is connected
    result = await _admission(journal).check_before_retry(chain)
    assert not result.ready and result.reason == "retry_state_unavailable"


@pytest.mark.parametrize("broken", ["missing", "cycle", "wrong_parent", "claimed"])
async def test_broken_or_retiring_lineage_fails_closed(journal, monkeypatch, broken):
    chain = _open(journal)
    original = journal.conversations.get_conversation

    def read(session_id):
        conv = original(session_id)
        if session_id == journal.parent:
            if broken == "missing":
                return None
            if broken == "cycle":
                return replace(conv, parent_conversation_id=journal.child)
            if broken == "claimed":
                return replace(conv, archive_close_claimed=True)
        if session_id == journal.child and broken == "wrong_parent":
            return replace(conv, parent_conversation_id=uuid4().hex)
        return conv

    monkeypatch.setattr(journal.conversations, "get_conversation", read)
    result = await _admission(journal).check_before_retry(chain)
    assert not result.ready
    assert (
        result.reason
        == {
            "missing": "stale_owner",
            "cycle": "invalid_ancestry",
            "wrong_parent": "invalid_ancestry",
            "claimed": "child_retired",
        }[broken]
    )


@pytest.mark.parametrize("control", [False, True])
async def test_replacement_generation_blocks_even_if_it_ends_in_the_same_failure(journal, control):
    chain = _open(journal)
    journal.store.record_input(
        journal.child,
        input_id=uuid4().hex,
        turn_id=None if control else "replacement-turn",
        expected_generation=1,
        payload={"control": "cancel"} if control else {"text": "replacement"},
    )
    result = await _admission(journal).check_before_retry(chain)
    assert not result.ready and result.reason == "superseded"
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


async def test_owner_epoch_and_binding_are_read_again_on_every_check(journal):
    chain = _open(journal)
    admission = _admission(journal)
    journal.store.claim_owner(journal.parent, "runner-original", expected_epoch=1)
    assert (await admission.check_before_retry(chain)).reason == "stale_owner"
    journal.conversations.replace_runner_id(journal.parent, "new-binding")
    assert (await admission.check_before_retry(chain)).reason == "stale_owner"


@pytest.mark.parametrize("accepted", [False, True])
async def test_uncertain_dispatch_is_closed_or_preserves_its_accepted_receipt(journal, accepted):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    admission = _admission(journal)
    before = await admission.lookup_or_close_input(receipt.input_id, owner=journal.owner)
    assert before.state == "pending" and before.receipt == receipt
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    if accepted:
        journal.store.record_acceptance(
            receipt.input_id, owner=journal.owner, turn_id="accepted-before-timeout"
        )
    resolved = await admission.lookup_or_close_input(
        receipt.input_id, owner=journal.owner, expected_version=receipt.version
    )
    assert resolved.state == ("accepted" if accepted else "closed")
    assert resolved.receipt.input_id == receipt.input_id
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == int(accepted)
    if not accepted:
        assert resolved.receipt.reason == {"code": "acceptance_unknown"}
        assert (
            journal.store.record_acceptance(
                receipt.input_id, owner=journal.owner, turn_id="late-admission"
            )
            is None
        )
    assert (
        await admission.lookup_or_close_input(
            receipt.input_id, owner=journal.owner, expected_version=receipt.version
        )
        == resolved
    )


async def test_stale_receipt_version_remains_pending_without_a_second_input(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    assert journal.store.claim_input(receipt.input_id, owner=journal.owner).claimed
    admission = _admission(journal)
    result = await admission.lookup_or_close_input(
        receipt.input_id, owner=journal.owner, expected_version=receipt.version
    )
    assert result.state == "pending" and result.receipt.version == receipt.version + 1
    result = await admission.lookup_or_close_input(
        receipt.input_id,
        owner=journal.owner,
        expected_version=result.receipt.version,
        reason={"code": "policy_denied", "detail": "input policy refused"},
    )
    assert result.state == "closed"
    assert result.receipt.reason == {"code": "policy_denied", "detail": "input policy refused"}
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0


async def test_receipt_lookup_never_exposes_a_foreign_owner_or_workspace(journal):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    admission = _admission(journal)
    with workspace_scope(987):
        result = await admission.lookup_or_close_input(receipt.input_id, owner=journal.owner)
        assert result.state == "not_found" and result.receipt is None
    current = journal.store.claim_owner(journal.parent, "runner-original", expected_epoch=1)
    result = await admission.lookup_or_close_input(
        receipt.input_id, owner=current, expected_version=receipt.version
    )
    assert result.state == "stale_owner" and result.receipt is None
    assert journal.store.get_input(receipt.input_id) == receipt


async def test_read_failures_are_explicit_and_do_not_close_pending_inputs(journal, monkeypatch):
    chain = _open(journal)
    receipt = journal.store.prepare_attempt(
        chain.chain_id, owner=journal.owner, expected_version=chain.version, now=160
    )
    admission = _admission(journal)

    def fail(*_args):
        raise OSError("induced storage read failure")

    with monkeypatch.context() as patch:
        patch.setattr(journal.store, "get_chain", fail)
        assert (await admission.check_before_retry(chain)).reason == "retry_state_unavailable"
        result = await admission.lookup_or_close_input(
            receipt.input_id, owner=journal.owner, expected_version=receipt.version
        )
        assert result.state == "unavailable" and result.reason == "retry_state_unavailable"
    assert journal.store.get_input(receipt.input_id) == receipt


async def test_check_deadline_bounds_the_entire_read_without_a_retry_or_mutation(
    journal, monkeypatch
):
    chain, admission = _open(journal), _admission(journal)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    check = admission._check

    def delayed(expected):
        try:
            entered.set()
            assert release.wait(2)
            return check(expected)
        finally:
            finished.set()

    monkeypatch.setattr(admission, "_check", delayed)
    monkeypatch.setattr("omnigent.server.subagent_retry_admission._RECONCILIATION_TIMEOUT_S", 0.01)
    try:
        result = await admission.check_before_retry(chain)
        assert entered.is_set() and not result.ready and result.reason == "server_unreachable"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert journal.store.get_chain(chain.chain_id) == chain
