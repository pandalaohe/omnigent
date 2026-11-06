"""REST → host tunnel → filtered settings and catalog re-sync."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host import agent_settings
from omnigent.host.frames import (
    CAP_AGENT_SETTINGS,
    CAP_HARNESS_ACCOUNTS,
    HostAgentSettingsFrame,
    HostAgentSettingsResultFrame,
    HostHelloFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostProbeAccountsFrame,
    HostProbeAccountsResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import websocket_scope as _websocket_scope

_HOST_ID = "f28abbfd54a8b22204184b69d9bfd49f"


@pytest.fixture()
async def _settings_stack(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, ApplicationCommunicator, HostModelCatalogCacheStore]]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home / ".omnigent"))
    monkeypatch.setattr(
        agent_settings,
        "_probe_account",
        lambda _descriptor, _root: {"kind": "none", "label": agent_settings.NOT_SIGNED_IN},
    )
    initial_root = tmp_path / "claude-initial"
    initial_root.mkdir()
    (initial_root / "settings.json").write_text(
        json.dumps({"model": "old-model", "unknown": {"kept": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(initial_root))

    registry = HostRegistry()
    host_store = HostStore(db_uri)
    catalog_store = HostModelCatalogCacheStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            SqlAlchemyConversationStore(db_uri),
            catalog_store=catalog_store,
        ),
        prefix="/v1",
    )
    communicator = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await communicator.send_input({"type": "websocket.connect"})
    assert (await communicator.receive_output(timeout=1.0))["type"] == "websocket.accept"
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="settings-test-host",
        capabilities=[CAP_AGENT_SETTINGS, CAP_HARNESS_ACCOUNTS],
    )
    await communicator.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    async def _answer_host() -> None:
        while True:
            output = await communicator.receive_output(timeout=None)
            if output.get("type") != "websocket.send" or not isinstance(output.get("text"), str):
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostAgentSettingsFrame):
                try:
                    if frame.op == "agent_settings_read":
                        view = agent_settings.read_agent_settings(frame.family, None)
                    elif frame.op == "agent_settings_write":
                        assert frame.key is not None
                        view = agent_settings.write_agent_setting(
                            frame.family, None, frame.key, frame.value, frame.revision
                        )
                    elif frame.op == "agent_settings_undo":
                        assert frame.key is not None
                        view = agent_settings.undo_agent_setting(frame.family, None, frame.key)
                    elif frame.op == "agent_settings_set_root":
                        view = agent_settings.agent_settings_set_root(frame.family, frame.root)
                    else:
                        raise AssertionError(frame.op)
                    reply = HostAgentSettingsResultFrame(frame.request_id, "ok", dict(view))
                except agent_settings.AgentSettingsError as exc:
                    reply = HostAgentSettingsResultFrame(
                        frame.request_id, "failed", None, str(exc)
                    )
            elif isinstance(frame, HostModelOptionsFrame):
                root = agent_settings.resolved_root("claude")
                reply = HostModelOptionsResultFrame(
                    frame.request_id,
                    "ok",
                    models=[{"id": f"model-from-{Path(root).name}"}],
                )
            elif isinstance(frame, HostProbeAccountsFrame):
                reply = HostProbeAccountsResultFrame(frame.request_id, "ok", harness_accounts={})
            else:
                continue
            await communicator.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(reply)}
            )

    task = asyncio.create_task(_answer_host())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield client, communicator, catalog_store
    finally:
        await client.aclose()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5.0)


@pytest.mark.asyncio
async def test_settings_routes_write_undo_and_re_sync_new_root(
    _settings_stack: tuple[AsyncClient, ApplicationCommunicator, HostModelCatalogCacheStore],
    tmp_path: Path,
) -> None:
    client, _communicator, catalog_store = _settings_stack
    url = f"/v1/hosts/{_HOST_ID}/agent-settings/claude"
    response = await client.get(url)
    assert response.status_code == 200, response.text
    before = response.json()
    revision = before["revision"]

    response = await client.put(
        url, json={"key": "model", "value": "new-model", "revision": revision}
    )
    assert response.status_code == 200, response.text
    written = response.json()
    assert (
        next(field for field in written["fields"] if field["key"] == "model")["undo_available"]
        is True
    )

    response = await client.post(f"{url}/undo", json={"key": "model"})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert (
        next(field for field in restored["fields"] if field["key"] == "model")["value"]
        == "old-model"
    )

    new_root = tmp_path / "claude-new"
    new_root.mkdir()
    response = await client.put(f"{url}/root", json={"root": str(new_root)})
    assert response.status_code == 200, response.text
    assert response.json()["root"] == str(new_root)
    rows = catalog_store.list([_HOST_ID])
    assert {row["harness"] for row in rows} == {"claude-native", "claude-sdk"}
    assert all(row["models"] == [{"id": "model-from-claude-new"}] for row in rows)


async def test_settings_route_cannot_write_model_sources(_settings_stack, tmp_path):
    client, _communicator, _catalog_store = _settings_stack
    sentinel = tmp_path / "source-command-ran"
    response = await client.put(
        f"/v1/hosts/{_HOST_ID}/agent-settings/claude",
        json={"key": "model_sources", "value": {"command": f"touch {sentinel}"}},
    )
    assert response.status_code == 400, response.text
    assert not sentinel.exists()
