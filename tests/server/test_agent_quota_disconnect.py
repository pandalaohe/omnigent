"""Interrupted uploads are diagnosed without accepting a usage snapshot."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnigent.server.routes.agent_quota import _usage_report_body


@pytest.mark.asyncio
async def test_disconnect_during_usage_body_is_bounded_and_not_accepted(caplog):
    chunks = iter(
        [
            {"type": "http.request", "body": b'{"schema_version":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(chunks)

    request = Request(
        {"type": "http", "method": "PUT", "path": "/me/usage/accounts/sample", "headers": []},
        receive,
    )
    with pytest.raises(HTTPException) as caught:
        await _usage_report_body(request)
    assert caught.value.status_code == 499
    assert "Usage upload disconnected" in caplog.text
    assert "schema_version" not in caplog.text


@pytest.mark.asyncio
async def test_disconnected_upload_never_calls_snapshot_writer(client, app, monkeypatch):
    from unittest.mock import Mock

    from starlette.requests import ClientDisconnect

    writer = Mock()
    monkeypatch.setattr(app.state.agent_quota_service, "report_account", writer)

    async def chunks():
        yield b'{"schema_version":'
        raise ClientDisconnect()

    response = await client.put(
        "/v1/me/usage/accounts/sample",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 499
    writer.assert_not_called()
