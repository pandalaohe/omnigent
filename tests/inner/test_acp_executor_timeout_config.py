"""Tests for ACP executor timeout configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor

_TIMEOUT_ENV = "HARNESS_ACP_PROMPT_TIMEOUT_S"
_PRINT_TIMEOUT = (
    "from omnigent.inner.acp_executor import _PROMPT_TIMEOUT_SECONDS; "
    "print(_PROMPT_TIMEOUT_SECONDS)"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_handshake_and_auth_retry_use_agent_timeout(configured: bool) -> None:
    config = (
        AcpAgentConfig(command="x", omnigent_mcp=False, init_timeout_s=120.5)
        if configured
        else AcpAgentConfig(command="x", omnigent_mcp=False)
    )
    ex = AcpExecutor(config)
    methods: list[str] = []

    async def respond(msg: dict) -> None:
        method = msg["method"]
        methods.append(method)
        if method == "initialize":
            response = {"result": {"authMethods": [{"id": "cached_token"}]}}
        elif method == "session/new" and not ex._authenticated:
            response = {"error": {"code": -32000, "message": "Authentication required"}}
        else:
            response = {"result": {"sessionId": "session"}}
        ex._pending.pop(msg["id"]).set_result(response)

    with (
        patch.object(ex, "_send", side_effect=respond),
        patch.object(ex, "_rpc", wraps=ex._rpc) as rpc,
    ):
        await ex._ensure_initialized()
        assert await ex._ensure_session() == "session"
    assert methods == ["initialize", "session/new", "authenticate", "session/new"]
    assert [call.kwargs["timeout"] for call in rpc.call_args_list] == [
        120.5 if configured else 30.0
    ] * 4


@pytest.mark.asyncio
async def test_initialize_timeout_names_configured_deadline() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="silent acp", name="Silent", init_timeout_s=0.01))
    with patch.object(ex, "_send", new=AsyncMock()):
        with pytest.raises(TimeoutError, match=r"Silent.*initialize within 0\.01s"):
            await ex._ensure_initialized()
    assert not ex._pending


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad", ""])
def test_harness_rejects_invalid_init_timeout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from omnigent.inner.acp_harness import _build_acp_executor

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "x")
    monkeypatch.setenv("HARNESS_ACP_INIT_TIMEOUT_S", value)
    with pytest.raises(ValueError):
        _build_acp_executor()


def _subprocess_env(value: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if value is None:
        env.pop(_TIMEOUT_ENV, None)
    else:
        env[_TIMEOUT_ENV] = value
    return env


def test_prompt_timeout_defaults_and_override() -> None:
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUT],
            env=_subprocess_env(None),
            text=True,
        ).strip()
        == "300.0"
    )
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUT],
            env=_subprocess_env("7200"),
            text=True,
        ).strip()
        == "7200.0"
    )


def test_prompt_timeout_malformed_value_fails_loud() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUT],
        env=_subprocess_env("not-a-number"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert _TIMEOUT_ENV in result.stderr.strip().splitlines()[-1]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_prompt_timeout_rejects_non_positive_or_non_finite_values(value: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUT],
        env=_subprocess_env(value),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert _TIMEOUT_ENV in result.stderr.strip().splitlines()[-1]
