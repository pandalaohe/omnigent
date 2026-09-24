"""Launch staging of the session-scoped instructions preamble for native TUIs.

The instructions a native session starts under — author text, framework
instructions, the server-held global text — are staged in the session's bridge
dir at terminal launch, then prefixed to the first message the executor injects.
Pi / cursor / kiro / goose / hermes / qwen / kimi / antigravity reach the
channel through their launch adapters.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.native.native_bridge_common import read_agent_instructions_preamble
from omnigent.runner.native import (
    NativeLaunchContext,
    _launch_antigravity,
    _launch_cursor,
    _launch_goose,
    _launch_hermes,
    _launch_kimi,
    _launch_kiro,
    _launch_pi,
    _launch_qwen,
)
from omnigent.runtime.prompt import EMBEDDED_BROWSER_PRIORITY_INSTRUCTION
from omnigent.spec.types import AgentSpec, ExecutorSpec

_SESSION_ID = "47f049b9d13df4db397c7f46859b825f"
_HARNESSES = ["pi", "cursor", "kiro", "goose", "hermes", "qwen", "kimi", "antigravity"]
_LAUNCHERS = {
    "pi": _launch_pi,
    "cursor": _launch_cursor,
    "kiro": _launch_kiro,
    "goose": _launch_goose,
    "hermes": _launch_hermes,
    "qwen": _launch_qwen,
    "kimi": _launch_kimi,
    "antigravity": _launch_antigravity,
}


class _StopAfterStaging(Exception):
    """Raised by the shim installed on the call right after the staging write."""


def _raise_stop(*_args: Any, **_kwargs: Any) -> Any:
    raise _StopAfterStaging


def _spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="preamble-agent",
        instructions="Author brief.",
        executor=ExecutorSpec(),
    )


def _stub_launch(harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch one harness's launch to stop right after the preamble write.

    Each launch resolves its bridge dir first and stages the preamble straight
    after, so short-circuiting the call that follows proves the write happened
    during launch prep without standing up a terminal.
    """
    from omnigent.runner.native import orchestration

    bridge_dir = tmp_path / harness / "bridge"
    if harness == "pi":
        import omnigent.harnesses.pi_native.bridge as pi_bridge

        async def _fake_pi_launch_config(**_kwargs: Any) -> Any:
            return SimpleNamespace(workspace=tmp_path)

        monkeypatch.setattr(orchestration, "_pi_native_launch_config", _fake_pi_launch_config)
        monkeypatch.setattr(pi_bridge, "prepare_bridge_dir", lambda session_id: bridge_dir)
        monkeypatch.setattr(pi_bridge, "clear_inbox", _raise_stop)
    elif harness == "cursor":
        import omnigent.harnesses.cursor_native.bridge as cursor_bridge

        monkeypatch.setattr(
            cursor_bridge, "bridge_dir_for_session_id", lambda session_id: bridge_dir
        )
        monkeypatch.setattr(orchestration, "_pi_native_launch_config", _raise_stop)
    elif harness == "kiro":
        import omnigent.harnesses.kiro_native.bridge as kiro_bridge
        import omnigent.harnesses.kiro_native.main as kiro_main

        async def _fake_kiro_launch_config(**_kwargs: Any) -> Any:
            return SimpleNamespace(
                workspace=tmp_path,
                terminal_launch_args=None,
                external_session_id=None,
                model_override=None,
            )

        monkeypatch.setattr(orchestration, "_kiro_native_launch_config", _fake_kiro_launch_config)
        monkeypatch.setattr(kiro_bridge, "prepare_bridge_dir", lambda session_id: bridge_dir)
        monkeypatch.setattr(kiro_main, "build_kiro_launch", _raise_stop)
    elif harness == "goose":
        import omnigent.harnesses.goose_native.bridge as goose_bridge
        import omnigent.harnesses.goose_native.forwarder as goose_forwarder

        monkeypatch.setattr(
            goose_bridge, "bridge_dir_for_session_id", lambda session_id: bridge_dir
        )
        monkeypatch.setattr(goose_forwarder, "clear_goose_bridge_state", _raise_stop)
    elif harness == "hermes":
        import omnigent.harnesses.hermes_native.bridge as hermes_bridge
        import omnigent.harnesses.hermes_native.forwarder as hermes_forwarder

        monkeypatch.setattr(
            hermes_bridge, "bridge_dir_for_session_id", lambda session_id: bridge_dir
        )
        monkeypatch.setattr(hermes_forwarder, "clear_hermes_bridge_state", _raise_stop)
    elif harness == "qwen":
        import omnigent.harnesses.qwen_native.bridge as qwen_bridge
        import omnigent.harnesses.qwen_native.forwarder as qwen_forwarder

        monkeypatch.setattr(
            qwen_bridge, "bridge_dir_for_session_id", lambda session_id: bridge_dir
        )
        monkeypatch.setattr(qwen_forwarder, "clear_qwen_bridge_state", _raise_stop)
    elif harness == "kimi":
        import omnigent.harnesses.kimi_native.bridge as kimi_bridge
        import omnigent.harnesses.kimi_native.forwarder as kimi_forwarder

        monkeypatch.setattr(
            kimi_bridge, "bridge_dir_for_session_id", lambda session_id: bridge_dir
        )
        monkeypatch.setattr(kimi_forwarder, "clear_kimi_bridge_state", _raise_stop)
    else:
        import omnigent.harnesses.antigravity_native.bridge as antigravity_bridge

        async def _fake_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"workspace": str(tmp_path)}

        monkeypatch.setattr(orchestration, "_session_payload_for_host_spawn_check", _fake_snapshot)
        monkeypatch.setattr(antigravity_bridge, "prepare_bridge_dir", lambda bridge_id: bridge_dir)
        monkeypatch.setattr(antigravity_bridge, "clear_bridge_state", _raise_stop)
    return bridge_dir


def _context(harness: str, agent_spec: AgentSpec | None, global_instructions: str | None) -> Any:
    return NativeLaunchContext(
        session_id=_SESSION_ID,
        resource_registry=SimpleNamespace(),  # type: ignore[arg-type]
        publish_event=lambda _sid, _evt: None,
        agent_spec=agent_spec,
        global_instructions=global_instructions,
        # antigravity's launch requires a bound server_client (it fetches the
        # session snapshot through it, stubbed to {} in _stub_launch); the
        # other harnesses ignore it here.
        server_client=SimpleNamespace() if harness == "antigravity" else None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", _HARNESSES)
async def test_launch_stages_startup_preamble(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec plus the global text stage one preamble, global text last."""
    bridge_dir = _stub_launch(harness, tmp_path, monkeypatch)

    with pytest.raises(_StopAfterStaging):
        await _LAUNCHERS[harness](_context(harness, _spec(), "GLOBAL-MARKER"))

    preamble = read_agent_instructions_preamble(bridge_dir)
    assert preamble is not None
    assert "Author brief." in preamble
    assert EMBEDDED_BROWSER_PRIORITY_INSTRUCTION in preamble
    assert preamble.endswith("GLOBAL-MARKER")


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", _HARNESSES)
async def test_launch_without_spec_or_global_text_stages_nothing(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is staged when there is neither a spec nor a global text."""
    bridge_dir = _stub_launch(harness, tmp_path, monkeypatch)

    with pytest.raises(_StopAfterStaging):
        await _LAUNCHERS[harness](_context(harness, None, None))

    assert read_agent_instructions_preamble(bridge_dir) is None
