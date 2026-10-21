"""Pi RPC usage and idle policy through the session-bound native relay."""

from __future__ import annotations

import asyncio
import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as relay_bridge
from omnigent.harnesses.pi_native import bridge as pi_bridge


async def _unused_tool(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
    return {}


def _post_relay(
    bridge_dir: Path, payload: object, *, authorized: bool = True
) -> tuple[int, dict[str, object] | None]:
    info = json.loads((bridge_dir / "tool_relay.json").read_text(encoding="utf-8"))
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = f"Bearer {info['token']}"
    request = Request(
        f"{info['url']}/pi/session-usage",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        exc.read()
        return exc.code, None


def _post_oversize_headers(bridge_dir: Path) -> int:
    info = json.loads((bridge_dir / "tool_relay.json").read_text(encoding="utf-8"))
    url = urlsplit(info["url"])
    connection = HTTPConnection(url.hostname, url.port, timeout=5)
    try:
        connection.putrequest("POST", "/pi/session-usage")
        connection.putheader("Authorization", f"Bearer {info['token']}")
        connection.putheader("Content-Length", str(relay_bridge._NATIVE_RELAY_BODY_MAX_BYTES + 1))
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_pi_usage_relay_authenticates_and_captures_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = pi_bridge.prepare_bridge_dir("bound-session")
    forwarded: list[httpx.Request] = []
    allowed = False

    def server(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        if request.content and json.loads(request.content)["data"] == {
            "pi_cache_warming_probe": True
        }:
            return httpx.Response(200, json={"pi_cache_warming_allowed": allowed})
        return httpx.Response(200, json={"queued": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://server"
    ) as client:
        relay = relay_bridge.start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=_unused_tool,
            loop=asyncio.get_running_loop(),
            policy_client=client,
            session_id="bound-session",
        )
        try:
            assert (
                await asyncio.to_thread(
                    _post_relay, bridge_dir, {"context_tokens": 19}, authorized=False
                )
            )[0] == 401
            assert (
                await asyncio.to_thread(
                    _post_relay, bridge_dir, {"session_id": "other", "context_tokens": 19}
                )
            )[0] == 400
            assert (await asyncio.to_thread(_post_relay, bridge_dir, ["context_tokens", 19]))[
                0
            ] == 400
            assert (await asyncio.to_thread(_post_relay, bridge_dir, {"context_tokens": 19})) == (
                200,
                {},
            )
            assert (
                await asyncio.to_thread(_post_relay, bridge_dir, {"pi_cache_warming_probe": True})
            ) == (200, {"pi_cache_warming_allowed": False})
            allowed = True
            assert (
                await asyncio.to_thread(_post_relay, bridge_dir, {"pi_cache_warming_probe": True})
            ) == (200, {"pi_cache_warming_allowed": True})
            assert [request.url.path for request in forwarded] == [
                "/v1/sessions/bound-session/events"
            ] * 3
            assert [json.loads(request.content) for request in forwarded] == [
                {"type": "external_session_usage", "data": {"context_tokens": 19}},
                {"type": "external_session_usage", "data": {"pi_cache_warming_probe": True}},
                {"type": "external_session_usage", "data": {"pi_cache_warming_probe": True}},
            ]
        finally:
            relay.close()


@pytest.mark.asyncio
async def test_pi_usage_relay_rejects_oversize_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = pi_bridge.prepare_bridge_dir("bound-session")
    forwarded = 0

    def server(_request: httpx.Request) -> httpx.Response:
        nonlocal forwarded
        forwarded += 1
        return httpx.Response(503, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://server"
    ) as client:
        relay = relay_bridge.start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=_unused_tool,
            loop=asyncio.get_running_loop(),
            policy_client=client,
            session_id="bound-session",
        )
        try:
            assert await asyncio.to_thread(_post_oversize_headers, bridge_dir) == 413
            assert forwarded == 0
            assert (
                await asyncio.to_thread(_post_relay, bridge_dir, {"pi_cache_warming_probe": True})
            )[0] == 503
            assert forwarded == 1
        finally:
            relay.close()
