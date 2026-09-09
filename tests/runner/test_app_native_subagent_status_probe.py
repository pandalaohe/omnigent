"""Runner endpoint for read-only Claude-native sub-agent status proof."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent import claude_native_status_probe as status_probe
from omnigent.runner.app import create_runner_app


class _ProcessManager:
    """Small ownership stub; this endpoint must not call into a harness."""

    def __init__(self, *session_ids: str, active_session_ids: set[str] | None = None) -> None:
        self.session_ids = set(session_ids)
        self.active_session_ids = active_session_ids or set()

    def has_session(self, session_id: str) -> bool:
        return session_id in self.session_ids

    def has_active_turn(self, session_id: str) -> bool:
        return session_id in self.active_session_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_id", "parent_registered"),
    [("parent-current", True), ("parent-released", False)],
    ids=["registered", "released"],
)
async def test_native_subagent_status_endpoint_reads_current_bridge_without_harness_call(
    monkeypatch: pytest.MonkeyPatch,
    parent_id: str,
    parent_registered: bool,
) -> None:
    server_requests: list[tuple[str, str]] = []
    bridge_id = f"bridge-{parent_id.removeprefix('parent-')}"

    def server_handler(request: httpx.Request) -> httpx.Response:
        server_requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "labels": {
                    "omnigent.wrapper": "claude-code-native-ui",
                    "omnigent.claude_native.bridge_id": bridge_id,
                }
            },
        )

    expected: status_probe.NativeSubagentProbeResult = {
        "parent_session_id": parent_id,
        "claude_session_id": "claude-session",
        "parent_complete_byte_offset": 420,
        "children": [],
    }
    calls: list[tuple[str, str]] = []

    def fake_probe(*, parent_session_id: str, bridge_id: str) -> Any:
        calls.append((parent_session_id, bridge_id))
        return expected

    monkeypatch.setattr(status_probe, "probe_native_subagent_status", fake_probe)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server_handler),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            process_manager=_ProcessManager(*([parent_id] if parent_registered else [])),  # type: ignore[arg-type]
            server_client=server_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
        ) as client:
            response = await client.get(f"/v1/sessions/{parent_id}/native_subagent_status")

    assert response.status_code == 200
    assert response.json() == {**expected, "bridge_id": bridge_id}
    assert calls == [(parent_id, bridge_id)]
    assert server_requests == [("GET", f"/v1/sessions/{parent_id}/labels")]


@pytest.mark.asyncio
async def test_native_subagent_status_endpoint_rejects_non_claude_server_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_probe(*, parent_session_id: str, bridge_id: str) -> Any:
        calls.append((parent_session_id, bridge_id))
        raise AssertionError("non-Claude session reached the bridge probe")

    monkeypatch.setattr(status_probe, "probe_native_subagent_status", fake_probe)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"labels": {"omnigent.wrapper": "codex-native-ui"}},
            )
        ),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            process_manager=_ProcessManager(),  # type: ignore[arg-type]
            server_client=server_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
        ) as client:
            response = await client.get("/v1/sessions/not-claude/native_subagent_status")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server_response",
    [
        pytest.param(httpx.Response(503), id="server-error"),
        pytest.param(httpx.Response(200, json=[]), id="non-object-payload"),
        pytest.param(httpx.Response(200, json={"labels": []}), id="non-object-labels"),
    ],
)
async def test_native_subagent_status_endpoint_reports_server_label_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
    server_response: httpx.Response,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_probe(*, parent_session_id: str, bridge_id: str) -> Any:
        calls.append((parent_session_id, bridge_id))
        raise AssertionError("label lookup failure reached the bridge probe")

    monkeypatch.setattr(status_probe, "probe_native_subagent_status", fake_probe)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: server_response),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            process_manager=_ProcessManager(),  # type: ignore[arg-type]
            server_client=server_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
        ) as client:
            response = await client.get("/v1/sessions/parent/native_subagent_status")

    assert response.status_code == 503
    assert response.json()["error"] == "server_labels_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_native_subagent_status_endpoint_preserves_probe_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def server_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        )

    def reject_probe(*, parent_session_id: str, bridge_id: str) -> Any:
        del parent_session_id, bridge_id
        raise status_probe.NativeSubagentProbeError(
            http_status=409,
            code="native_parent_identity_changed",
            detail="identity changed",
        )

    monkeypatch.setattr(status_probe, "probe_native_subagent_status", reject_probe)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server_handler),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            process_manager=_ProcessManager("parent-current"),  # type: ignore[arg-type]
            server_client=server_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
        ) as client:
            response = await client.get("/v1/sessions/parent-current/native_subagent_status")

    assert response.status_code == 409
    assert response.json() == {
        "error": "native_parent_identity_changed",
        "detail": "identity changed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_runtime_registered", "child_runtime_active"),
    [(True, False), (False, True)],
)
async def test_native_subagent_status_endpoint_vetoes_independently_resumed_child(
    monkeypatch: pytest.MonkeyPatch,
    child_runtime_registered: bool,
    child_runtime_active: bool,
) -> None:
    def server_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        )

    terminal_child: status_probe.NativeSubagentProbeChild = {
        "server_session_id": "child-resumed",
        "subagent_id": "native-child",
        "tool_use_id": "toolu_original",
        "status": "terminal",
        "terminal_status": "completed",
        "reason": "structured_parent_terminal_evidence",
        "evidence": {
            "claude_session_id": "claude-parent",
            "parent_complete_byte_offset": 120,
            "subagent_id": "native-child",
            "meta_tool_use_id": "toolu_original",
            "evidence_key": "a" * 64,
        },
    }

    def fake_probe(*, parent_session_id: str, bridge_id: str) -> Any:
        del bridge_id
        return {
            "parent_session_id": parent_session_id,
            "claude_session_id": "claude-parent",
            "parent_complete_byte_offset": 120,
            "children": [terminal_child.copy()],
        }

    monkeypatch.setattr(status_probe, "probe_native_subagent_status", fake_probe)
    active = {"child-resumed"} if child_runtime_active else set()
    process_manager = _ProcessManager(
        "parent-current",
        *(["child-resumed"] if child_runtime_registered else []),
        active_session_ids=active,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server_handler),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            process_manager=process_manager,  # type: ignore[arg-type]
            server_client=server_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
        ) as client:
            response = await client.get("/v1/sessions/parent-current/native_subagent_status")

    assert response.status_code == 200
    child = response.json()["children"][0]
    assert child["status"] == "unverified"
    assert child["terminal_status"] is None
    assert child["reason"] == "independent_child_runtime_present"
