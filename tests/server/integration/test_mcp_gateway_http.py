"""HTTP MCP credentials through the policy adapter and runner client."""

import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.e2e.test_mcp_registry import demo_mcp as demo_mcp
from tests.server.helpers import create_test_session
from tests.server.test_credential_store import _FakeCipher

_ENV_NAME = "TEST_MCP_SERVICE_BEARER"
_ENV_HEADER = "X-Test-Mcp-Auth"
_ENV_TOKEN = "fake-test-sentinel-mcp-key"


@pytest.fixture
def app(runtime_init, db_uri, tmp_path, monkeypatch, request):
    from omnigent.runtime import get_agent_cache, get_agent_store, get_conversation_store
    from omnigent.server.app import create_app
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    header = getattr(request, "param", None)
    if header is not None:
        monkeypatch.setenv(_ENV_NAME, _ENV_TOKEN)
        monkeypatch.delenv("TEST_MCP_MISSING_BEARER", raising=False)
        registry = McpRegistry(
            McpRegistryConfig(
                services=[
                    McpService(
                        id=service_id,
                        title=service_id,
                        url="https://mcp.example.test/mcp",
                        auth="env",
                        token_env=variable_name,
                        **({"header": header} if header != "Authorization" else {}),
                        tools=["read_ticket"],
                    )
                    for service_id, variable_name in (
                        ("tracker", _ENV_NAME),
                        ("missing", "TEST_MCP_MISSING_BEARER"),
                    )
                ]
            ),
            None,
        )
    else:
        demo = request.getfixturevalue("demo_mcp")
        monkeypatch.setenv(
            "OMNIGENT_MCP_OAUTH_STATE_SECRET", "local-test-signing-key-32-characters-long"
        )
        entry = McpService(
            id="tracker",
            title="Tracker",
            url=demo + "/mcp",
            auth="oauth",
            allow_http=True,
            tools=["whoami", "read_ticket"],
            oauth={
                "authorize_url": demo + "/authorize",
                "token_url": demo + "/token",
                "client_id": "demo",
            },
        )
        registry = McpRegistry(
            McpRegistryConfig(public_url="http://localhost", services=[entry]),
            CredentialStore(db_uri, _FakeCipher()),
        )
    return create_app(
        agent_store=get_agent_store(),
        conversation_store=get_conversation_store(),
        agent_cache=get_agent_cache(),
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        file_store=SqlAlchemyFileStore(db_uri),
        mcp_registry=registry,
    )


@pytest.mark.asyncio
async def test_real_http_oauth_refresh_through_gateway(client, app, demo_mcp):
    registry = app.state.mcp_registry
    store = registry.store
    start = await client.get("/v1/connections/mcp-tracker/connect")
    assert start.status_code in (302, 307), start.text
    fields = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    async with httpx.AsyncClient() as provider:
        consent = await provider.post(
            demo_mcp + "/authorize", data={**fields, "user": "alice@example.test"}
        )
    callback = await client.get(consent.headers["location"])
    assert "mcp-tracker=connected" in callback.headers["location"]
    catalog = await client.get("/v1/mcp-registry/services")
    assert catalog.json()["data"][0]["connected"]
    connection_test = await client.post("/v1/mcp-registry/services/tracker/test")
    assert connection_test.json() == {"tools": ["whoami", "read_ticket"]}, connection_test.text
    session = await create_test_session(client, name="http-gateway")
    sid = session["id"]
    attached = await client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers", json={"name": "tracker", "transport": "registry"}
    )
    assert attached.status_code == 200, attached.text
    spec = AgentSpec(
        spec_version=1,
        name="http-gateway",
        mcp_servers=[MCPServerConfig(name="tracker", transport="registry")],
    )
    proxy = ProxyMcpManager(sid, client)
    schemas = await proxy.schemas_for(spec)
    assert schemas.tool_names == {"tracker__whoami", "tracker__read_ticket"}
    grant = store.get("local", "mcp:tracker", with_secret=True)
    assert grant is not None
    # Force the real provider refresh path without waiting for the token TTL.
    store.update_secret("local", "mcp:tracker", secret=grant.secret, metadata={"expires_at": 0})
    result = await proxy.call_tool(spec, "tracker__whoami", {})
    assert "alice@example.test" in result
    assert '"refresh_count":1' in result.replace(" ", "")
    fresh = ProxyMcpManager(sid, client)
    assert "TEST-123" in await fresh.call_tool(
        spec, "tracker__read_ticket", {"ticket_id": "TEST-123"}
    )
    # A replacement registry also reuses the persisted account and refreshed grant.
    replacement = McpRegistry(registry.config, store)
    result = await replacement.execute(
        "tracker", "local", app.state, tool="read_ticket", arguments={"ticket_id": "TEST-123"}
    )
    assert "TEST-123" in result.content[0].text
    with pytest.raises(ConnectionError, match="not allowed"):
        await registry.execute("tracker", "local", app.state, tool="delete_ticket")
    disconnected = await client.delete("/v1/mcp-registry/services/tracker/connection")
    assert disconnected.status_code == 200
    assert "Connect" in await proxy.call_tool(spec, "tracker__whoami", {})
    with pytest.raises(ConnectionError, match="Connect"):
        await replacement.execute("tracker", "local", app.state, tool="whoami")
    assert (await client.get("/v1/mcp-registry/services")).json()["data"][0]["connected"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app", "expected_header"),
    [("Authorization", "Authorization"), (_ENV_HEADER, _ENV_HEADER)],
    indirect=["app"],
)
async def test_env_credential_is_confined_to_upstream_http(
    client, monkeypatch, caplog, expected_header
):
    from omnigent.server import mcp_registry

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(_ENV_NAME, "fake-env-change-after-startup")
    calls = []
    mode = "result"

    def upstream(request):
        # Use the real MCP SDK with a transport double for the upstream.
        assert request.url.host == "mcp.example.test"
        assert request.headers[expected_header] == f"Bearer {_ENV_TOKEN}"
        if expected_header != "Authorization":
            assert "Authorization" not in request.headers
        if request.method == "GET":
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        calls.append(method)
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "read_ticket",
                        "description": f"Echo {_ENV_TOKEN}",
                        "inputSchema": {
                            "type": "object",
                            "properties": {_ENV_TOKEN: {"default": _ENV_TOKEN}},
                        },
                    }
                ]
            }
        else:
            assert method == "tools/call"
            if mode == "failure":
                raise httpx.ConnectError(f"Connection failed: {_ENV_TOKEN}", request=request)
            if mode == "redirect":
                return httpx.Response(
                    307, headers={"Location": f"https://redirect.example.test/{_ENV_TOKEN}"}
                )
            if mode == "rpc_error":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32000, "message": f"Upstream error: {_ENV_TOKEN}"},
                    },
                )
            result = {
                "content": [{"type": "text", "text": f"Error with {_ENV_TOKEN}: {_ENV_TOKEN}"}],
                "isError": True,
            }
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"event: message\ndata: {json.dumps(payload)}\n\n",
            )
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_registry.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )

    def surface(response):
        for marker in [_ENV_TOKEN, _ENV_NAME, _ENV_HEADER, "TEST_MCP_MISSING_BEARER"]:
            assert marker not in response.text
        return response.json()

    catalog = surface(await client.get("/v1/mcp-registry/services"))
    assert {
        row["id"]: (row["auth"], row["connected"], row["status"], row["connect_provider"])
        for row in catalog["data"]
    } == {
        "tracker": ("env", True, "configured", None),
        "missing": ("env", False, "unavailable", None),
    }
    surface(await client.get("/openapi.json"))
    assert (await client.post("/v1/mcp-registry/services/missing/test")).status_code == 404
    assert (
        await client.put(
            "/v1/mcp-registry/services/tracker/connection", json={"token": "personal-test-token"}
        )
    ).status_code == 400
    assert (await client.delete("/v1/mcp-registry/services/tracker/connection")).status_code == 400
    assert calls == []

    session_id = (await create_test_session(client, name="env-gateway"))["id"]
    agent_path = f"/v1/sessions/{session_id}/agent"
    attached = await client.post(
        f"{agent_path}/mcp-servers", json={"name": "tracker", "transport": "registry"}
    )
    assert attached.status_code == 200
    surface(await client.get(agent_path))

    async def rpc(path, method, params=None):
        return surface(
            await client.post(
                path,
                headers={"X-Omnigent-Session-Id": session_id},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            )
        )

    gateway = "/v1/mcp/tracker"
    discovered = (await rpc(gateway, "tools/list"))["result"]["tools"][0]
    assert discovered["description"] == "Echo [redacted]"
    assert discovered["inputSchema"]["properties"] == {"[redacted]": {"default": "[redacted]"}}
    for path, name in (
        (gateway, "read_ticket"),
        (f"/v1/sessions/{session_id}/mcp", "tracker__read_ticket"),
    ):
        for outcome in ("result", "failure", "redirect", "rpc_error"):
            mode = outcome
            payload = await rpc(path, "tools/call", {"name": name})
            if mode == "result":
                assert payload["result"]["isError"] is True
                assert (
                    payload["result"]["content"][0]["text"] == "Error with [redacted]: [redacted]"
                )
            else:
                expected = {
                    "failure": "transport failed",
                    "redirect": "HTTP 307",
                    "rpc_error": "MCP request failed",
                }[mode]
                assert expected in payload["error"]["message"]
    assert calls.count("tools/call") == 8
    assert _ENV_TOKEN not in caplog.text
    assert _ENV_HEADER not in caplog.text
    assert _ENV_NAME not in caplog.text
