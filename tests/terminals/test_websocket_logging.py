"""Clipboard payloads cannot enter either transport's protocol diagnostics."""

import logging
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from uvicorn import Config
from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
from uvicorn.server import ServerState
from websockets.frames import OP_PING, OP_TEXT
from websockets.protocol import OPEN, SERVER, Protocol

from omnigent.server.routes.terminal_attach import create_terminal_attach_router
from omnigent.util.websocket_logging import websocket_protocol_logger


@pytest.mark.asyncio
async def test_clipboard_payloads_stay_out_of_debug_protocol_logs(caplog, monkeypatch):
    app = FastAPI()
    app.include_router(create_terminal_attach_router())
    clipboard_text = b"synthetic-clipboard-text"
    reply = b'{"type":"host-clipboard","text":"' + clipboard_text + b'"}'
    browser_logger = logging.getLogger("uvicorn.error")
    tunnel_logger = websocket_protocol_logger("websockets.omnigent.runner_tunnel")
    monkeypatch.setattr(browser_logger, "propagate", True)
    with (
        caplog.at_level(logging.DEBUG, logger=tunnel_logger.name),
        caplog.at_level(logging.DEBUG, logger="uvicorn.error"),
    ):
        browser = WebSocketProtocol(Config(app, log_config=None), ServerState(), {})
        transport = Mock()
        browser.transport = transport
        browser.write_frame_sync(True, OP_TEXT, reply)

        tunnel = Protocol(SERVER, state=OPEN, logger=tunnel_logger)
        tunnel.send_text(b'{"type":"ws_frame","data":' + reply + b"}")
        tunnel.send_binary(clipboard_text, fin=False)
        tunnel.send_continuation(clipboard_text, fin=True)
        tunnel.send_ping(b"keepalive")
        browser.write_frame_sync(True, OP_PING, b"browser keepalive")

    assert any(
        record.name == tunnel_logger.name and "keepalive" in record.getMessage()
        for record in caplog.records
    )
    assert "browser keepalive" in caplog.text
    assert clipboard_text.decode() not in caplog.text
    assert any(clipboard_text in frame for frame in tunnel.data_to_send())
    assert clipboard_text in transport.write.call_args_list[0].args[0]
