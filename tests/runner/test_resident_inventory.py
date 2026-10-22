"""Owner evidence reads never probe or create vendor/terminal resources."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_owner_reader_preserves_missing_registry_and_incomplete_aliases(
    monkeypatch, configured
):
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    resources = SessionResourceRegistry(
        terminal_registry=TerminalRegistry() if configured else None
    )
    resources.list_resources = Mock(side_effect=AssertionError("must read registrations only"))
    app = create_runner_app(
        server_client=NullServerClient(), resource_registry=resources, auth_token="test-auth"
    )
    async with _runner_client(app) as client:
        url = "/v1/startup/owner-inventory?read_id=read-one"
        assert (await client.get(url)).status_code == 401
        response = await client.get(url, headers={"Authorization": "Bearer test-auth"})
        assert response.status_code == 200
        assert response.json() == {
            "read_id": "read-one",
            "native_backends": {
                "owners": [],
                "exit_coverage_complete": True,
                "aliases_complete": False,
            },
            "terminals": [] if configured else None,
            "executor_contexts_complete": False,
            "external_owners_complete": False,
        }


@pytest.mark.asyncio
async def test_reader_keeps_shared_backend_aliases_as_evidence_without_probing(monkeypatch):
    backend = SimpleNamespace(
        resident_incarnation_id="backend-one",
        process_exit_confirmed=False,
        exit_confirmed=False,
        close=Mock(side_effect=AssertionError("read must not close")),
        is_alive=Mock(side_effect=AssertionError("read must not probe")),
    )
    monkeypatch.setattr(
        orchestration,
        "_AUTO_CODEX_APP_SERVERS",
        {
            "session-one": backend,
            "ui-alias": backend,
        },
    )
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    app = create_runner_app(server_client=NullServerClient())
    async with _runner_client(app) as client:
        response = await client.get("/v1/startup/owner-inventory?read_id=owners")
    assert response.status_code == 200
    assert response.json()["native_backends"] == {
        "owners": [
            {
                "incarnation_id": "backend-one",
                "harness": "codex-native",
                "session_ids": ["session-one", "ui-alias"],
                "registered": True,
                "closing": False,
                "process_exit_confirmed": False,
                "exit_confirmed": False,
                "aliases_complete": False,
            }
        ],
        "exit_coverage_complete": True,
        "aliases_complete": False,
    }
    assert response.json()["executor_contexts_complete"] is False
    backend.close.assert_not_called()
    backend.is_alive.assert_not_called()
