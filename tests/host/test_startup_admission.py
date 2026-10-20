"""The host validates startup permission before configuration probes or spawn."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HostLaunchRunnerFrame,
    HostStartupClaimFrame,
    HostStartupClaimResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.startup_protocol import StartupAttemptRef


def attempt():
    return StartupAttemptRef(
        startup_id=uuid4().hex,
        attempt_id=uuid4().hex,
        input_id=uuid4().hex,
        runtime_id="runtime-generation",
        host_generation="connection",
    )


def test_claim_and_launch_frames_keep_exact_attempt_metadata():
    ref = attempt()
    frames = [
        HostStartupClaimFrame(
            request_id="claim", session_id=uuid4().hex, runner_id="runner", attempt=ref
        ),
        HostStartupClaimResultFrame(request_id="claim", granted=False),
        HostLaunchRunnerFrame(
            request_id="launch",
            binding_token="test-binding",
            workspace="/opt/work/project",
            session_id=uuid4().hex,
            startup=ref,
        ),
    ]
    for frame in frames:
        assert decode_host_frame(encode_host_frame(frame)) == frame


@pytest.mark.asyncio
async def test_refused_startup_does_no_configuration_probe_or_spawn(monkeypatch, tmp_path):
    host = HostProcess(
        HostIdentity(uuid4().hex, "sample-host"),
        "http://localhost:8000",
        config_path=tmp_path / "config.yaml",
        interactive_shells=["bash"],
    )
    claim = AsyncMock(return_value=False)
    monkeypatch.setattr(host, "_claim_startup", claim)
    probe = Mock(side_effect=AssertionError("preflight must follow admission"))
    monkeypatch.setattr("omnigent.host.connect.harness_is_configured", probe)
    spawn = Mock(side_effect=AssertionError("spawn must follow admission"))
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    result = await host._handle_launch_impl(
        HostLaunchRunnerFrame(
            request_id="launch",
            binding_token="test-binding",
            workspace="/opt/work/project",
            session_id=uuid4().hex,
            harness="codex-native",
            startup=attempt(),
        )
    )
    assert result.status == "failed" and result.error_code == "startup_not_admitted"
    assert claim.await_count == 1
    probe.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_connection", [False, True])
async def test_claim_response_is_bound_to_its_authenticated_connection(
    monkeypatch, tmp_path, replace_connection
):
    host = HostProcess(
        HostIdentity(uuid4().hex, "sample-host"),
        "http://localhost:8000",
        config_path=tmp_path / "config.yaml",
        interactive_shells=["bash"],
    )
    ws = object()
    host._ws = ws
    host._startup_generation = "connection"

    async def reply(connection, raw):
        request = decode_host_frame(raw)
        assert isinstance(request, HostStartupClaimFrame)
        assert connection is ws
        if replace_connection:
            host._startup_generation = "replacement"
        await host._dispatch_host_frame(ws, HostStartupClaimResultFrame(request.request_id, True))

    monkeypatch.setattr(host, "_send_text", reply)
    frame = HostLaunchRunnerFrame(
        request_id="launch",
        binding_token="test-binding",
        workspace="/opt/work/project",
        session_id=uuid4().hex,
        startup=attempt(),
    )
    assert await host._claim_startup(frame) is not replace_connection
    assert host._startup_claims == {}
