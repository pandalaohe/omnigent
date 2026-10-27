"""Persistent SDK owners reuse existing hermetic SDK fixtures."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from omnigent.inner.antigravity_executor import AntigravityExecutor
from omnigent.inner.copilot_executor import CopilotExecutor, _CopilotSessionState
from omnigent.inner.cursor_executor import CursorExecutor, _CursorSessionState
from tests.inner.test_antigravity_executor import _install_fake_sdk as install_antigravity
from tests.inner.test_copilot_executor import _install_fake_copilot as install_copilot
from tests.inner.test_cursor_executor import _install_fake_sdk as install_cursor


@pytest.fixture(params=["copilot", "cursor", "antigravity"])
def owner(request, monkeypatch, tmp_path):
    family = request.param
    if family == "copilot":
        captured = install_copilot(monkeypatch, [[], []])
        executor = CopilotExecutor(
            cwd=str(tmp_path), github_host="example.test", github_token="fixture-token"
        )
        target, method = sys.modules["copilot"].CopilotClient, "create_session"

        async def ensure(key):
            state = executor._session_states.setdefault(key, _CopilotSessionState())
            await executor._ensure_session(state, None, [], "system")
            return state
    elif family == "cursor":
        captured = install_cursor(monkeypatch, [{}, {}])
        executor = CursorExecutor(cwd=str(tmp_path), api_key="fixture-key")
        target, method = sys.modules["cursor_sdk"].AsyncAgent, "create"

        async def ensure(key):
            state = executor._session_states.setdefault(key, _CursorSessionState())
            await executor._ensure_session(state, "auto-smart", [])
            return state
    else:
        captured = install_antigravity(monkeypatch, scripts=[[], []])
        executor = AntigravityExecutor(api_key="fixture-key")
        target, method = executor, "_open_agent"

        async def ensure(key):
            state, _ = await executor._ensure_agent(
                key, model=None, system_prompt="system", tools=[]
            )
            return state

    return SimpleNamespace(
        executor=executor,
        ensure=ensure,
        target=target,
        method=method,
        captured=captured,
        family=family,
    )


async def snapshot(executor):
    read = await executor.prepare_context_inventory_v2("session", "read")
    return read().sources[0]


async def test_sdk_primary_reuse_two_execution_keys_and_passive_read(owner):
    executor = owner.executor
    read = await executor.prepare_context_inventory_v2("session", "read")
    assert read().sources[0].contexts == ()
    assert executor._session_states == {}
    first = await owner.ensure("one")
    await owner.ensure("two")
    assert await owner.ensure("one") is first
    source = read().sources[0]
    assert len(source.contexts) == 2
    assert [r.execution_keys for r in source.contexts] == [("one",), ("two",)]
    assert len({r.key for r in source.contexts}) == 2
    for record in source.contexts:
        assert record.provenance == "primary" and record.operation.action == "create"
        assert record.lifecycle == "live" and record.vendor_id is None
        assert not record.aliases and record.stop_proof is record.continuity_proof is None
    assert source.coverage == "partial" and source.revision_authority == "observation_only"
    assert source.work_bindings is source.work_revision is source.membership_revision is None
    assert "sdk_vendor_identity_unconfirmed" in {r.reason for r in source.unresolved}
    executor._session_states.pop("one")
    assert read().sources[0].contexts[0].lifecycle == "unknown"
    await executor.close()
    assert all(r.lifecycle == "unknown" for r in read().sources[0].contexts)


async def test_sdk_pending_close_and_same_key_successor_fence_late_reply(owner, monkeypatch):
    executor = owner.executor
    original = getattr(owner.target, owner.method)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def create(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner.target, owner.method, create)
    task = asyncio.create_task(owner.ensure("one"))
    await entered.wait()
    (pending,) = (await snapshot(executor)).contexts
    assert pending.lifecycle == "starting" and pending.execution_keys == ("one",)
    assert pending.vendor_id is None
    await executor.close_session("one")
    assert (await snapshot(executor)).contexts[0].lifecycle == "unknown"
    successor = await owner.ensure("one")
    release.set()
    with pytest.raises(RuntimeError, match="retired during startup"):
        await task
    assert executor._session_states["one"] is successor
    records = (await snapshot(executor)).contexts
    assert [r.lifecycle for r in records] == ["unknown", "live"]
    assert records[0].key != records[1].key
    assert records[0].operation != records[1].operation
    if owner.family == "antigravity":
        assert owner.captured["agents"][-1].closed
    else:
        assert owner.captured["client_closed"] == 1
    await executor.close()


async def test_sdk_lost_start_reply_retains_birth_and_allows_fresh_operation(owner, monkeypatch):
    executor = owner.executor
    original = getattr(owner.target, owner.method)
    entered = asyncio.Event()

    async def create(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(owner.target, owner.method, create)
    task = asyncio.create_task(owner.ensure("one"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await snapshot(executor)).contexts[0].lifecycle == "unknown"
    monkeypatch.setattr(owner.target, owner.method, original)
    await owner.ensure("one")
    assert [r.lifecycle for r in (await snapshot(executor)).contexts] == ["unknown", "live"]
    await executor.close()


async def test_sdk_closing_remains_visible_after_mapping_pop_and_cancel(owner, monkeypatch):
    executor = owner.executor
    await owner.ensure("one")
    entered = asyncio.Event()

    async def close(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    method = "_close_agent" if owner.family == "antigravity" else "_close_state_unobserved"
    monkeypatch.setattr(executor, method, close)
    task = asyncio.create_task(executor.close_session("one"))
    await entered.wait()
    assert "one" not in executor._session_states
    assert (await snapshot(executor)).contexts[0].lifecycle == "closing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (record,) = (await snapshot(executor)).contexts
    assert record.lifecycle == "unknown" and record.stop_proof is None


async def test_sdk_real_turn_signature_rebuild_uses_new_operation(owner):
    executor = owner.executor
    messages = [{"role": "user", "content": "hello", "session_id": "one"}]
    for prompt in ("first", "second"):
        _events = [event async for event in executor.run_turn(messages, [], prompt)]
    records = (await snapshot(executor)).contexts
    assert [r.lifecycle for r in records] == ["unknown", "live"]
    assert records[0].execution_keys == records[1].execution_keys == ("one",)
    assert records[0].key != records[1].key
    await executor.close()


async def test_public_sdk_turn_start_failure_cannot_close_same_key_successor(owner, monkeypatch):
    executor = owner.executor
    original = getattr(owner.target, owner.method)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def create(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    async def turn():
        return [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "hello", "session_id": "one"}], [], "system"
            )
        ]

    monkeypatch.setattr(owner.target, owner.method, create)
    task = asyncio.create_task(turn())
    await entered.wait()
    await executor.close_session("one")
    successor = await owner.ensure("one")
    release.set()
    events = await task
    assert any("retired during startup" in getattr(e, "message", "") for e in events)
    assert executor._session_states["one"] is successor
    assert [r.lifecycle for r in (await snapshot(executor)).contexts] == ["unknown", "live"]
    await executor.close()


async def test_public_sdk_rebuild_close_cannot_replace_same_key_successor(owner, monkeypatch):
    executor = owner.executor
    await owner.ensure("one")
    method = "_close_agent" if owner.family == "antigravity" else "_close_state_unobserved"
    original = getattr(executor, method)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def close(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    async def turn():
        return [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "hello", "session_id": "one"}], [], "changed"
            )
        ]

    monkeypatch.setattr(executor, method, close)
    task = asyncio.create_task(turn())
    await entered.wait()
    await executor.close_session("one")
    successor = await owner.ensure("one")
    release.set()
    events = await task
    assert any("retired during rebuild" in getattr(e, "message", "") for e in events)
    assert executor._session_states["one"] is successor
    assert [r.lifecycle for r in (await snapshot(executor)).contexts] == ["unknown", "live"]
    await executor.close()


async def test_sdk_inventory_budget_and_long_execution_key_do_not_break_creation(owner):
    executor = owner.executor
    await owner.ensure("x" * 257)
    source = await snapshot(executor)
    assert source.coverage == "partial"
    assert source.unresolved[0].reason == "sdk_execution_key_invalid"
    invalid = await executor.prepare_context_inventory_v2("session", "read", timeout_s=0)
    assert invalid().sources[0].unresolved[0].reason == "invalid_read_budget"
    executor._context_source._overflow = True
    assert (await snapshot(executor)).unresolved[0].reason == "context_history_budget"
    await executor.close()
