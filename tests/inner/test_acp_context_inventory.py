"""Real ACP process-owner boundaries with a hermetic subprocess transport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.inner import acp_executor, goose_executor, qwen_executor
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.inner.qwen_executor import QwenExecutor


@pytest.fixture(params=["acp", "client", "grok", "jcode", "goose", "qwen"])
def owner(request, monkeypatch, tmp_path):
    family = request.param
    if family == "goose":
        executor = GooseExecutor(cwd=str(tmp_path))
        module = goose_executor
    elif family == "qwen":
        executor = QwenExecutor(cwd=str(tmp_path))
        module = qwen_executor
    else:
        executor = AcpExecutor(
            AcpAgentConfig(
                name=family,
                command="fake-acp",
                session_id_mode="client" if family == "client" else "server",
            ),
            cwd=str(tmp_path),
        )
        module = acp_executor
    monkeypatch.setattr(
        executor, "_build_spawn_env", AsyncMock(return_value={}) if family == "qwen" else dict
    )
    if isinstance(executor, AcpExecutor):
        monkeypatch.setattr(executor, "_sandbox_launch", lambda _: ("fake-acp", []))
    else:
        monkeypatch.setattr(executor, "_sandbox_launch_path", lambda _: "fake-acp")
    spawn = AsyncMock()
    # Patch this owner's module view, leaving the event loop's asyncio intact.
    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"create_subprocess_exec": spawn}))
    )
    monkeypatch.setattr(executor._mcp, "session_new_servers", lambda **_: [])
    executor._rpc = AsyncMock(return_value={"result": {"sessionId": "vendor-one"}})
    yield executor, spawn


async def snapshot(executor):
    read = await executor.prepare_context_inventory_v2("session", "read")
    return read().sources[0]


def process():
    async def wait_line():
        await asyncio.Event().wait()

    return SimpleNamespace(
        returncode=None,
        stdout=SimpleNamespace(readline=wait_line),
        stderr=SimpleNamespace(readline=wait_line),
    )


async def start(executor, spawn):
    spawn.return_value = process()
    await executor._start_process()
    # Reader tasks are not under test here; real EOF is exercised separately.
    for task in (executor._reader_task, executor._stderr_task):
        task.cancel()
    await asyncio.gather(executor._reader_task, executor._stderr_task, return_exceptions=True)
    executor._reader_task = executor._stderr_task = None


async def test_primary_binding_reuse_and_per_key_close_stay_partial(owner):
    executor, spawn = owner
    await start(executor, spawn)
    (pending,) = (await snapshot(executor)).contexts
    assert pending.lifecycle == "starting" and pending.vendor_id is None
    assert await executor._ensure_session() == "vendor-one"
    source = await snapshot(executor)
    (record,) = source.contexts
    assert record.key == pending.key and record.lifecycle == "live"
    assert record.vendor_id.value == "vendor-one"
    assert record.operation.action == "create" and record.provenance == "primary"
    assert record.execution_keys == record.aliases == ()
    assert record.stop_proof is record.continuity_proof is None
    assert source.coverage == "partial" and source.revision_authority == "observation_only"
    assert source.work_bindings is source.membership_revision is source.age_upper_bound_s is None
    assert "independent_sdk_key_mapping_unsupported" in {r.reason for r in source.unresolved}
    assert await executor._ensure_session() == "vendor-one"
    executor._rpc.assert_awaited_once()
    await executor.close_session("routing-key")
    assert executor._proc.returncode is None
    source = await snapshot(executor)
    assert source.contexts[0].lifecycle == "unknown"
    assert "per_key_close_unsupported" in {r.reason for r in source.unresolved}


async def test_birth_precedes_spawn_and_lost_reply_retains_unknown(owner):
    executor, spawn = owner
    entered, release = asyncio.Event(), asyncio.Event()

    async def spawn_later(*args, **kwargs):
        entered.set()
        await release.wait()
        return process()

    spawn.side_effect = spawn_later
    task = asyncio.create_task(executor._start_process())
    await entered.wait()
    (pending,) = (await snapshot(executor)).contexts
    assert pending.lifecycle == "starting" and pending.vendor_id is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await snapshot(executor)).contexts[0].lifecycle == "unknown"
    spawn.side_effect = None
    await start(executor, spawn)
    executor._rpc.side_effect = TimeoutError("reply lost")
    with pytest.raises(TimeoutError):
        await executor._ensure_session()
    records = (await snapshot(executor)).contexts
    assert len(records) == 2 and all(r.lifecycle == "unknown" for r in records)
    executor._rpc.side_effect = None
    await executor._ensure_session()
    assert len((await snapshot(executor)).contexts) == 3


async def test_restart_forgets_cached_id_and_fences_old_reply(owner):
    executor, spawn = owner
    await start(executor, spawn)
    await executor._ensure_session()
    old_key = (await snapshot(executor)).contexts[0].key
    executor._system_prompt_sent = True
    executor._proc.returncode = 1
    await start(executor, spawn)
    assert executor._session_id is None and executor._system_prompt_sent is False
    assert len((await snapshot(executor)).contexts) == 2
    entered, release = asyncio.Event(), asyncio.Event()

    async def reply(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"result": {"sessionId": "stale"}}

    executor._rpc.side_effect = reply
    task = asyncio.create_task(executor._ensure_session())
    await entered.wait()
    await start(executor, spawn)
    executor._rpc.side_effect = None
    await executor._ensure_session()
    release.set()
    with pytest.raises(RuntimeError, match="crossed process generation"):
        await task
    source = await snapshot(executor)
    assert source.contexts[0].key == old_key
    assert [r.lifecycle for r in source.contexts] == ["unknown", "unknown", "live"]
    assert executor._session_id == "vendor-one"
    assert source.contexts[-1].vendor_id.value == "vendor-one"
    assert len({r.key for r in source.contexts}) == 3
    assert all(not r.aliases for r in source.contexts)


async def test_eof_and_cancelled_close_never_claim_stop(owner, monkeypatch):
    executor, spawn = owner
    await start(executor, spawn)
    await executor._ensure_session()
    executor._proc.stdout.readline = AsyncMock(return_value=b"")
    await executor._read_stdout()
    assert (await snapshot(executor)).contexts[0].lifecycle == "unknown"
    entered = asyncio.Event()

    async def closing():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "_close_unobserved", closing)
    task = asyncio.create_task(executor.close())
    await entered.wait()
    assert (await snapshot(executor)).contexts[0].lifecycle == "closing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await snapshot(executor)).contexts[0].lifecycle == "unknown"
    assert (await snapshot(executor)).contexts[0].stop_proof is None


async def test_inventory_is_passive_bounded_and_reconciles_at_read(owner):
    executor, spawn = owner
    read = await executor.prepare_context_inventory_v2("session", "read")
    assert read().sources[0].contexts == ()
    spawn.assert_not_awaited()
    executor._rpc.assert_not_awaited()
    await start(executor, spawn)
    await executor._ensure_session()
    assert read().sources[0].contexts[0].lifecycle == "live"
    executor._proc.returncode = 1
    assert read().sources[0].contexts[0].lifecycle == "unknown"
    invalid = await executor.prepare_context_inventory_v2("session", "invalid", timeout_s=0)
    assert invalid().sources[0].coverage == "partial"
    assert invalid().sources[0].unresolved[0].reason == "invalid_read_budget"
    executor._context_source._overflow = True
    assert read().sources[0].coverage == "partial"
    assert read().sources[0].unresolved[0].reason == "context_history_budget"


async def test_prompt_reset_records_new_birth_and_original_reader_observes_eof(owner):
    executor, spawn = owner
    await start(executor, spawn)
    await executor._ensure_session()
    executor._initialized = True
    entered, eof = asyncio.Event(), asyncio.Event()

    async def readline():
        entered.set()
        await eof.wait()
        return b""

    executor._proc.stdout.readline = readline
    reader = asyncio.create_task(executor._read_stdout())
    await entered.wait()

    async def send(message):
        executor._pending[message["id"]].set_result(
            {"error": {"code": -32603, "message": "Session not found"}}
        )

    executor._send = AsyncMock(side_effect=send)
    try:
        events = [
            event
            async for event in executor.run_turn([{"role": "user", "content": "hello"}], [], "")
        ]
        assert any(getattr(event, "retryable", False) for event in events)
        assert executor._session_id is None
        (old,) = (await snapshot(executor)).contexts
        assert old.lifecycle == "unknown"
        pending, reply = asyncio.Event(), asyncio.Event()

        async def rpc(*args, **kwargs):
            pending.set()
            await reply.wait()
            return {"result": {"sessionId": "vendor-two"}}

        executor._rpc.side_effect = rpc
        create = asyncio.create_task(executor._ensure_session())
        await pending.wait()
        records = (await snapshot(executor)).contexts
        assert [r.lifecycle for r in records] == ["unknown", "starting"]
        assert records[1].key != old.key and records[1].operation != old.operation
        assert records[1].vendor_id is None
        reply.set()
        assert await create == "vendor-two"
        assert (await snapshot(executor)).contexts[1].lifecycle == "live"
        eof.set()
        await reader
        assert executor._proc.returncode is None
        records = (await snapshot(executor)).contexts
        assert [r.lifecycle for r in records] == ["unknown", "unknown"]
        assert records[1].vendor_id.value == "vendor-two"
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_old_process_reader_eof_cannot_retire_successor(owner):
    executor, spawn = owner
    await start(executor, spawn)
    await executor._ensure_session()
    entered, eof = asyncio.Event(), asyncio.Event()

    async def readline():
        entered.set()
        await eof.wait()
        return b""

    executor._proc.stdout.readline = readline
    reader = asyncio.create_task(executor._read_stdout())
    await entered.wait()
    try:
        executor._proc.returncode = 1
        await start(executor, spawn)
        await executor._ensure_session()
        eof.set()
        await reader
        assert [r.lifecycle for r in (await snapshot(executor)).contexts] == ["unknown", "live"]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_client_requested_id_without_server_confirmation_is_unknown(monkeypatch, tmp_path):
    executor = AcpExecutor(
        AcpAgentConfig(
            command="fake-acp",
            session_id_mode="client",
        ),
        cwd=str(tmp_path),
    )
    executor._proc = process()
    monkeypatch.setattr(executor._mcp, "session_new_servers", lambda **_: [])
    executor._rpc = AsyncMock(return_value={"result": {}})
    requested = await executor._ensure_session()
    assert requested
    (record,) = (await snapshot(executor)).contexts
    assert record.vendor_id is None and record.lifecycle == "unknown"
