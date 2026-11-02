"""Intervention and phase recovery keep one final result and never re-arm retries."""

import sqlite3

import httpx
import pytest

from omnigent.entities import NewConversationItem
from omnigent.runner import subagent_work
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.subagent_retry import SubagentRetryRuntime
from omnigent.runner.subagent_retry_spool import RetryFinalSpool
from omnigent.runner.tool_dispatch import _cleanup_drained_subagent_work, _drain_inbox
from tests._helpers.polling import poll_until_async
from tests.runner.test_subagent_retry import runtime_case


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "replacement", "refused", "history_only"])
async def test_intervention_requires_confirmed_admission(db_uri, tmp_path, control):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        j, rt = case.journal, case.runtime
        ack = await rt.accept_terminal(j.terminal, "held provider failure")
        state = rt.chains[ack.chain_id]
        timer = state.timer
        if control in {"cancel", "refused"}:
            assert j.conversations.set_runner_id(j.child, "child-control-runner")
            assert j.store.begin_control(j.child, "cancel-input", runner_id="child-control-runner")
            assert j.store.finish_control(
                j.child, "cancel-input", accepted=control == "cancel"
            ) == (control == "cancel")
        elif control == "replacement":
            assert j.store.record_input(
                j.child,
                input_id="replacement-input",
                turn_id="replacement-turn",
                expected_generation=1,
                payload={"text": "human replacement"},
            )
        else:
            j.conversations.append(
                j.child,
                [
                    NewConversationItem(
                        type="message",
                        response_id="history-only",
                        data={
                            "role": "user",
                            "content": [{"type": "input_text", "text": "human text stored only"}],
                        },
                    )
                ],
            )
        await rt.observe_control(j.child)
        assert not case.sent
        if control == "cancel":
            final = j.store.get_final(ack.chain_id)
            assert final.outcome.status == "cancelled" and final.outcome.reason == "cancelled"
            assert (
                final.outcome.error == j.terminal.error
                and "held provider failure" in final.outcome.output
            )
            assert final.attempts == 0 and state.timer is None
            assert case.inbox.qsize() == len(case.wakes) == 1
        elif control == "replacement":
            assert j.store.get_chain(ack.chain_id).phase == "superseded"
            assert state.timer is None and j.store.get_final(ack.chain_id) is None
            assert case.inbox.empty() and not case.wakes
            assert case.entry.retry_chain_id is None and case.entry.status == "running"
        else:
            assert state.timer is timer and state.chain.due_at == 160
            assert not case.entry.delivered and case.inbox.empty()
            case.now[0] = 160
            await rt._run(ack.chain_id)
            assert len(case.sent) == 1


@pytest.mark.asyncio
async def test_deadline_outage_survives_new_runtime_even_when_new_chains_are_off(
    db_uri, tmp_path, monkeypatch
):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "original provider error")
        case.now[0] = 160

        async def unavailable(*_args, **_kwargs):
            raise httpx.ConnectError("server unavailable")

        with monkeypatch.context() as outage:
            outage.setattr(case.client, "post", unavailable)
            await case.runtime._run(ack.chain_id)
        assert not case.sent and case.inbox.empty()
        [intent] = spool.pending(case.journal.parent)
        assert intent.chain_id == ack.chain_id and intent.reason == "server_unreachable"
        assert case.runtime.chains[ack.chain_id].timer is None
        await case.runtime.close()
        restored = SubagentRetryRuntime(
            case.client, limit=0, spool=RetryFinalSpool(spool.path), clock=lambda: case.now[0]
        )
        try:
            assert await restored.recover_parent(case.journal.parent)
            await poll_until_async(
                lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = case.journal.store.get_final(ack.chain_id)
            assert (
                final.notice_id == intent.notice_id
                and final.outcome.reason == "server_unreachable"
            )
            assert final.outcome.error == case.journal.terminal.error and final.attempts == 0
            assert final.wake_receipt == final.notice_id and not spool.pending(case.journal.parent)
            assert case.inbox.qsize() == len(case.wakes) == 1 and not case.sent
            assert await restored.recover_parent(case.journal.parent)
            assert case.inbox.qsize() == len(case.wakes) == 1
            assert all(state.timer is None for state in restored.chains.values())
            text = await _drain_inbox(
                case.inbox, server_client=case.client, conversation_id=case.journal.parent
            )
            assert "original provider error" in text and case.inbox.empty()
            assert case.journal.store.get_final(ack.chain_id).drain_receipt == final.result_id
            assert await restored.recover_parent(case.journal.parent)
            assert case.inbox.empty() and len(case.wakes) == 1
        finally:
            await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_phase", ["finalize", "inbox", "wake"])
async def test_lost_phase_response_rehydrates_one_result_without_second_wake(
    db_uri, tmp_path, monkeypatch, lost_phase
):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "held error")
        post = case.client.post
        lost = False

        async def lose_response(path, **kwargs):
            nonlocal lost
            result = await post(path, **kwargs)
            if str(path).endswith("/" + lost_phase) and not lost:
                lost = True
                raise httpx.ReadError("response lost after commit")
            return result

        with monkeypatch.context() as loss:
            loss.setattr(case.client, "post", lose_response)
            if lost_phase == "wake":
                await case.runtime.finalize(case.runtime.chains[ack.chain_id], "host_offline")
            else:
                with pytest.raises(httpx.ReadError):
                    await case.runtime.finalize(case.runtime.chains[ack.chain_id], "host_offline")
        assert lost
        committed = case.journal.store.get_final(ack.chain_id)
        await case.runtime.close()
        while not case.inbox.empty():
            case.inbox.get_nowait()
        subagent_work.unregister_subagent_work_for_session(case.journal.child)
        restored = SubagentRetryRuntime(case.client, spool=RetryFinalSpool(spool.path))
        try:
            assert await restored.recover_parent(case.journal.parent)
            assert await restored.recover_parent(case.journal.parent)
            await poll_until_async(
                lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = case.journal.store.get_final(ack.chain_id)
            assert (final.result_id, final.notice_id, final.outcome) == (
                committed.result_id,
                committed.notice_id,
                committed.outcome,
            )
            assert case.inbox.qsize() == len(case.wakes) == 1
            payload = case.inbox.get_nowait()
            assert payload["retry_final"]["result_id"] == final.result_id
            assert await _cleanup_drained_subagent_work(payload, server_client=case.client)
            assert await restored.recover_parent(case.journal.parent)
            assert case.inbox.empty() and len(case.wakes) == 1 and not case.sent
        finally:
            await restored.close()


@pytest.mark.asyncio
async def test_lost_drain_receipt_retains_then_settles_the_same_payload(
    db_uri, tmp_path, monkeypatch
):
    async with runtime_case(db_uri, spool=RetryFinalSpool(tmp_path / "finals.sqlite3")) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "held error")
        await case.runtime.finalize(case.runtime.chains[ack.chain_id], "host_offline")
        post = case.client.post

        async def lose_drain(path, **kwargs):
            result = await post(path, **kwargs)
            if str(path).endswith("/drain"):
                raise httpx.ReadError("drain response lost")
            return result

        with monkeypatch.context() as loss:
            loss.setattr(case.client, "post", lose_drain)
            assert "held error" in await _drain_inbox(
                case.inbox, server_client=case.client, conversation_id=case.journal.parent
            )
        assert case.inbox.qsize() == 1
        final = case.journal.store.get_final(ack.chain_id)
        assert final.drain_receipt == final.result_id
        assert "held error" in await _drain_inbox(
            case.inbox, server_client=case.client, conversation_id=case.journal.parent
        )
        assert case.inbox.empty() and len(case.wakes) == 1


@pytest.mark.asyncio
async def test_parent_initialization_can_reenter_recovery_during_final_wake(
    db_uri, tmp_path, monkeypatch
):
    import asyncio

    from tests.runner.test_member_obligations import _build_app

    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "held error")
        await case.runtime._spool(case.runtime.chains[ack.chain_id], "server_unreachable")
        await case.runtime.close()
        restored = SubagentRetryRuntime(case.client, spool=spool)
        app, _manager, _harness = _build_app(case.client)
        app.state.subagent_retry_runtime = restored
        post = case.client.post
        entered = []

        async def initialize_parent(path, **kwargs):
            if str(path).endswith("/wake"):
                entered.append(path)
                await app.state.recover_undrained_subagent_results(case.journal.parent)
            return await post(path, **kwargs)

        try:
            monkeypatch.setattr(case.client, "post", initialize_parent)
            await asyncio.wait_for(
                app.state.recover_undrained_subagent_results(case.journal.parent), 5
            )
            await poll_until_async(
                lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            assert len(entered) == len(case.wakes) == case.inbox.qsize() == 1
            final = case.journal.store.get_final(ack.chain_id)
            assert final.wake_receipt == final.notice_id
        finally:
            await restored.close()


@pytest.mark.asyncio
async def test_new_owner_fences_old_timer_and_old_spool(db_uri, tmp_path):
    from omnigent.runner.identity import token_bound_runner_id

    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        j = case.journal
        ack = await case.runtime.accept_terminal(j.terminal, "original error")
        await case.runtime._spool(case.runtime.chains[ack.chain_id], "old diagnostic")
        j.conversations.clear_runner_id(j.parent)
        assert j.conversations.set_runner_id(j.parent, token_bound_runner_id("replacement-owner"))
        case.client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "replacement-owner"
        restored = SubagentRetryRuntime(case.client, spool=RetryFinalSpool(spool.path))
        try:
            assert await restored.recover_parent(j.parent)
            await poll_until_async(
                lambda: j.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = j.store.get_final(ack.chain_id)
            case.now[0] = 160
            await case.runtime._run(ack.chain_id)
            assert j.store.get_final(ack.chain_id) == final
            assert case.inbox.qsize() == len(case.wakes) == 1 and not case.sent
            assert not any(state.timer for state in restored.chains.values())
        finally:
            await restored.close()


@pytest.mark.asyncio
async def test_known_completed_attempt_wins_recovery_candidate_and_uses_its_output(
    db_uri, tmp_path
):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        j = case.journal
        ack = await case.runtime.accept_terminal(j.terminal, "old failure")
        case.now[0] = 160
        await case.runtime._run(ack.chain_id)
        state = case.runtime.chains[ack.chain_id]
        receipt = j.store.get_input(state.chain.pending_input_id)
        await case.runtime._spool(state, "server_unreachable")
        completed = j.terminal.model_copy(
            update={
                "turn_id": receipt.turn_id,
                "input_id": receipt.input_id,
                "input_generation": receipt.input_generation,
                "status": "completed",
                "error": None,
            }
        )
        assert j.store.record_terminal(
            ack.chain_id, completed, owner=state.chain.owner, observed_at=161
        )
        j.conversations.append(
            j.child,
            [
                NewConversationItem(
                    type="message",
                    response_id=receipt.turn_id,
                    data={
                        "role": "assistant",
                        "agent": "worker",
                        "content": [{"type": "output_text", "text": "verified successful output"}],
                    },
                )
            ],
        )
        await case.runtime.close()
        restored = SubagentRetryRuntime(case.client, spool=spool)
        try:
            assert await restored.recover_parent(j.parent)
            await poll_until_async(
                lambda: j.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = j.store.get_final(ack.chain_id)
            assert final.outcome.status == "completed" and final.outcome.reason == "completed"
            assert final.outcome.output == "verified successful output" and final.attempts == 1
            assert len(case.sent) == len(case.wakes) == case.inbox.qsize() == 1
        finally:
            await restored.close()


@pytest.mark.asyncio
async def test_local_completed_observation_replaces_unconfirmed_failure_candidate(
    db_uri, tmp_path, monkeypatch
):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        j = case.journal
        ack = await case.runtime.accept_terminal(j.terminal, "old failure")
        case.now[0] = 160
        await case.runtime._run(ack.chain_id)
        state = case.runtime.chains[ack.chain_id]
        receipt = j.store.get_input(state.chain.pending_input_id)
        await case.runtime._spool(state, "server_unreachable")
        completed = j.terminal.model_copy(
            update={
                "turn_id": receipt.turn_id,
                "input_id": receipt.input_id,
                "input_generation": receipt.input_generation,
                "status": "completed",
                "error": None,
            }
        )

        async def unavailable(*_args, **_kwargs):
            raise httpx.ConnectError("server unavailable")

        with monkeypatch.context() as outage:
            outage.setattr(case.client, "post", unavailable)
            with pytest.raises(httpx.ConnectError):
                await case.runtime.accept_terminal(completed, "success observed before crash")
        [candidate] = spool.pending(j.parent)
        assert (
            candidate.terminal == completed and candidate.output == "success observed before crash"
        )
        assert j.store.get_chain(ack.chain_id).latest_terminal.status == "failed"
        await case.runtime.close()
        restored = SubagentRetryRuntime(case.client, spool=RetryFinalSpool(spool.path))
        try:
            assert await restored.recover_parent(j.parent)
            await poll_until_async(
                lambda: j.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = j.store.get_final(ack.chain_id)
            assert final.outcome.status == "completed" and final.outcome.output == candidate.output
            assert (
                final.attempts == 1
                and len(case.sent) == len(case.wakes) == case.inbox.qsize() == 1
            )
            payload = case.inbox.get_nowait()
            assert payload["auto_retry"] == {
                "chain_id": ack.chain_id,
                "attempts": 1,
                "of": 3,
                "reason": "completed",
            }
        finally:
            await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", ["entry", "terminal", "duplicate"])
async def test_first_forwarded_recovery_edge_never_rearms_child_retry(db_uri, tmp_path, recovered):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "held failure")
        await case.runtime.close()
        if recovered == "entry":
            subagent_work.unregister_subagent_work_for_session(case.journal.child)
            case.entry = subagent_work.register_subagent_work(
                parent_session_id=case.journal.parent,
                child_session_id=case.journal.child,
                agent="restored worker",
                title="restored task",
                registered_by="recovery",
                recovered=True,
                work_id="restored-original-work",
            )
            assert case.entry.retry_chain_id is None
        terminal = case.journal.terminal.model_copy(update={"recovered": recovered == "terminal"})
        restored = SubagentRetryRuntime(case.client, spool=spool)
        try:
            reply = await restored.accept_terminal(terminal, "held failure")
            assert reply.chain_id == ack.chain_id
            chain = case.journal.store.get_chain(ack.chain_id)
            assert chain.retry_closed and chain.due_at is None
            assert not any(state.timer for state in restored.chains.values()) and not case.sent
            await poll_until_async(
                lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            assert case.journal.store.get_final(ack.chain_id).outcome.reason == "runner_restarted"
            assert case.inbox.qsize() == len(case.wakes) == 1
            if recovered == "entry":
                assert case.entry.delivered and case.entry.retry_chain_id == ack.chain_id
                assert "restored worker:restored task" in await _drain_inbox(
                    case.inbox, server_client=case.client, conversation_id=case.journal.parent
                )
                assert subagent_work.get_subagent_work(case.journal.child) is None
        finally:
            await restored.close()


@pytest.mark.asyncio
async def test_local_only_candidate_follows_recorded_parent_succession(
    db_uri, tmp_path, monkeypatch
):
    from omnigent.runner.app import _rekey_subagent_work_for_succession

    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        j = case.journal

        async def unavailable(*_args, **_kwargs):
            raise httpx.ConnectError("server unavailable")

        with monkeypatch.context() as outage:
            outage.setattr(case.client, "post", unavailable)
            with pytest.raises(httpx.ConnectError):
                await case.runtime.accept_terminal(j.terminal, "local-only failed result")
        [original] = spool.pending(j.parent)
        assert original.chain is None
        successor = j.conversations.create_conversation(
            title="successor",
            agent_id=j.conversations.get_conversation(j.parent).agent_id,
            runner_id=j.conversations.get_conversation(j.parent).runner_id,
        )
        assert j.conversations.reassign_live_children(
            j.parent, successor.id, "local-spool-succession"
        )[0] == [j.child]
        j.conversations.clear_runner_id(j.parent)
        subagent_work._session_inboxes_ref[successor.id] = subagent_work._session_inboxes_ref.pop(
            j.parent
        )
        _rekey_subagent_work_for_succession(j.parent, successor.id)
        try:
            await case.runtime.succeed_parent(j.parent, successor.id)
            await poll_until_async(
                lambda: (f := j.store.get_final(original.chain_id)) and f.wake_receipt, timeout_s=5
            )
            final = j.store.get_final(original.chain_id)
            assert final.parent_id == successor.id and final.notice_id == original.notice_id
            assert (
                final.outcome.output.endswith("local-only failed result") and final.attempts == 0
            )
            assert case.inbox.qsize() == len(case.wakes) == 1 and not case.sent
        finally:
            subagent_work._session_inboxes_ref.pop(successor.id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["hold", "final"])
async def test_unwritable_spool_stops_retry_and_keeps_server_final_delivery(
    db_uri, tmp_path, stage
):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("fixture")
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        if stage == "hold":
            spool.path = blocked / "finals.sqlite3"
        ack = await case.runtime.accept_terminal(case.journal.terminal, "original failure")
        if stage == "final":
            assert ack.reason == "held" and case.inbox.empty()
            spool.path = blocked / "finals.sqlite3"
            await case.runtime.finalize(case.runtime.chains[ack.chain_id], "host_offline")
        final = case.journal.store.get_final(ack.chain_id)
        assert final.outcome.reason == (
            "retry_state_unavailable" if stage == "hold" else "host_offline"
        )
        assert final.outcome.error["message"] == case.journal.terminal.error["message"]
        assert final.outcome.error["retry_diagnostics"] == [{"code": "spool_unavailable"}]
        assert final.attempts == 0 and case.runtime.chains[ack.chain_id].timer is None
        assert await case.runtime.recover_parent(case.journal.parent)
        await poll_until_async(
            lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
        )
        assert case.inbox.qsize() == len(case.wakes) == 1 and not case.sent
        assert "original failure" in await _drain_inbox(
            case.inbox, server_client=case.client, conversation_id=case.journal.parent
        )
        assert case.journal.store.get_final(ack.chain_id).drain_receipt == final.result_id


@pytest.mark.asyncio
async def test_corrupt_candidate_keeps_authoritative_final_delivery(db_uri, tmp_path):
    spool = RetryFinalSpool(tmp_path / "finals.sqlite3")
    async with runtime_case(db_uri, spool=spool) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "held failure")
        await case.runtime._spool(case.runtime.chains[ack.chain_id], "server_unreachable")
        await case.runtime.close()
        with sqlite3.connect(spool.path) as db:
            db.execute(
                "UPDATE final_intents SET payload=? WHERE chain_id=?",
                ('{"invalid":true}', ack.chain_id),
            )
        restored = SubagentRetryRuntime(case.client, spool=RetryFinalSpool(spool.path))
        try:
            assert await restored.recover_parent(case.journal.parent)
            await poll_until_async(
                lambda: case.journal.store.get_final(ack.chain_id).wake_receipt, timeout_s=5
            )
            final = case.journal.store.get_final(ack.chain_id)
            assert final.outcome.reason == "retry_state_unavailable" and final.attempts == 0
            assert final.outcome.error["message"] == case.journal.terminal.error["message"]
            assert final.outcome.error["retry_diagnostics"] == [{"code": "spool_unavailable"}]
            assert case.inbox.qsize() == len(case.wakes) == 1 and not case.sent
            assert not spool.pending(case.journal.parent)
            assert all(state.timer is None for state in restored.chains.values())
            assert "retry_state_unavailable" in await _drain_inbox(
                case.inbox, server_client=case.client, conversation_id=case.journal.parent
            )
            assert case.journal.store.get_final(ack.chain_id).drain_receipt == final.result_id
            assert await restored.recover_parent(case.journal.parent)
            assert case.inbox.empty() and len(case.wakes) == 1
        finally:
            await restored.close()
