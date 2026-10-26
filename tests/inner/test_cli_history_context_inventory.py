"""CLI execution and reusable history remain distinct from canonical contexts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import hermes_executor, kimi_executor
from omnigent.inner.hermes_executor import HermesExecutor
from omnigent.inner.kimi_executor import KimiExecutor
from tests.inner.test_kimi_harness import _FakeProcess


@pytest.fixture(params=["hermes", "kimi"])
def owner(request, monkeypatch, tmp_path):
    family = request.param
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    if family == "hermes":
        monkeypatch.setattr(HermesExecutor, "_setup_hermes_home", lambda _: None)
        executor = HermesExecutor(hermes_path="fake-hermes", cwd=str(tmp_path))

        async def spawn(*args, **kwargs):
            calls.append(args)
            proc = SimpleNamespace(returncode=None)

            async def communicate():
                if len(calls) == 2:
                    entered.set()
                    await release.wait()
                proc.returncode = 0
                return b"session_id: saved-history\nhello", b""

            async def wait():
                proc.returncode = 0
                return 0

            proc.communicate = communicate
            proc.wait = wait
            proc.terminate = release.set
            return proc

        monkeypatch.setattr(
            hermes_executor,
            "asyncio",
            SimpleNamespace(**(vars(asyncio) | {"create_subprocess_exec": spawn})),
        )
    else:
        executor = KimiExecutor(binary_path="fake-kimi", cwd=str(tmp_path))
        monkeypatch.setattr(
            kimi_executor,
            "shutil",
            SimpleNamespace(**(vars(kimi_executor.shutil) | {"which": lambda _: "/opt/bin/kimi"})),
        )
        monkeypatch.setattr(executor, "_seed_wire_checkpoint", lambda: None)
        monkeypatch.setattr(executor, "_collect_turn_usage", lambda _: None)
        monkeypatch.setattr(executor, "_sandbox_launch_path", lambda _: "fake-kimi")

        async def spawn(*args, **kwargs):
            calls.append(args)
            proc = _FakeProcess(
                [
                    json.dumps(
                        {
                            "role": "meta",
                            "type": "session.resume_hint",
                            "session_id": "saved-history",
                        }
                    ),
                    json.dumps({"role": "assistant", "content": "hello"}),
                ],
                b"",
            )
            proc._returncode = None
            if len(calls) == 2:
                original = proc.stdout

                class HeldStdout:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        entered.set()
                        await release.wait()
                        return await original.__anext__()

                proc.stdout = HeldStdout()

            async def wait():
                proc._returncode = 0
                return 0

            proc.wait = wait
            proc.terminate = release.set
            return proc

        monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", spawn)
    monkeypatch.setattr(executor, "_build_spawn_env", dict)
    return family, executor, calls, entered, release


async def test_resume_history_active_cli_interrupt_and_mapping_removal_remain_unsupported(owner):
    family, executor, calls, entered, release = owner
    read = await executor.prepare_context_inventory_v2("session", "read")

    def unsupported():
        (source,) = read().sources
        assert source.coverage == "unsupported" and source.contexts == ()
        assert source.unresolved[0].reason.startswith(family + "_")
        assert source.producer_incarnation is source.work_bindings is source.work_revision is None

    async def turn():
        return [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "hello", "session_id": "execution"}], [], ""
            )
        ]

    unsupported()
    assert not calls
    await turn()
    assert len(calls) == 1
    unsupported()
    task = asyncio.create_task(turn())
    await entered.wait()
    try:
        flag = "--resume" if family == "hermes" else "-S"
        assert calls[1][calls[1].index(flag) + 1] == "saved-history"
        unsupported()
        assert await executor.interrupt_session("execution")
        unsupported()
        release.set()
        await task
        await executor.close_session("execution")
        if family == "hermes":
            assert executor._session_map == {}
        else:
            assert executor._session_id is None
        unsupported()
        assert len(calls) == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cli_inventory_never_spawns_reads_history_or_interrupts(owner, monkeypatch):
    _family, executor, calls, _entered, _release = owner
    monkeypatch.setattr(executor, "run_turn", Mock(side_effect=AssertionError("no model work")))
    monkeypatch.setattr(
        executor, "close_session", AsyncMock(side_effect=AssertionError("no close"))
    )
    monkeypatch.setattr(
        executor, "_build_spawn_env", Mock(side_effect=AssertionError("no env read"))
    )
    read = await executor.prepare_context_inventory_v2("session", "read")
    assert read().sources[0].coverage == "unsupported"
    assert read().sources[0].coverage == "unsupported"
    assert not calls
    executor.run_turn.assert_not_called()
    executor.close_session.assert_not_awaited()
    executor._build_spawn_env.assert_not_called()
    invalid = await executor.prepare_context_inventory_v2("session", "read", timeout_s=0)
    assert invalid().sources[0].unresolved[0].reason == "invalid_read_budget"
