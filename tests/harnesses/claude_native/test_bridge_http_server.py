"""Socket ownership and pool fallback for the shared native HTTP bridge."""

import socket
from http.server import BaseHTTPRequestHandler
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge


@pytest.mark.parametrize("is_windows", [True, False], ids=["windows", "posix"])
@pytest.mark.parametrize("pool_exhausted", [False, True], ids=["next-port", "ephemeral"])
def test_bridge_http_server_socket_options_and_bind_retry(
    monkeypatch: pytest.MonkeyPatch, is_windows: bool, pool_exhausted: bool
) -> None:
    """A Windows reusable socket must not steal an occupied relay port."""
    exclusive_option = -5  # Winsock SO_EXCLUSIVEADDRUSE; absent on POSIX hosts.
    sockets: list[Mock] = []
    attempts: list[int] = []
    options: list[dict[int, int]] = []

    def make_socket(*args: object, **kwargs: object) -> Mock:
        sock = Mock()
        configured: dict[int, int] = {}

        def set_option(level: int, option: int, value: int) -> None:
            assert level == socket.SOL_SOCKET
            configured[option] = value

        def bind(address: tuple[str, int]) -> None:
            port = address[1]
            attempts.append(port)
            occupied = port == 28700 or (pool_exhausted and port == 28701)
            # Model Winsock: SO_REUSEADDR permits another listener's port.
            reuses_taken_port = is_windows and configured.get(socket.SO_REUSEADDR) == 1
            if occupied and not reuses_taken_port:
                raise OSError("address already in use")
            sock.getsockname.return_value = (address[0], port or 49152)

        sock.setsockopt.side_effect = set_option
        sock.bind.side_effect = bind
        sockets.append(sock)
        options.append(configured)
        return sock

    monkeypatch.setattr(claude_native_bridge, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive_option, raising=False)
    monkeypatch.setattr(socket, "socket", make_socket)
    monkeypatch.setattr(claude_native_bridge, "_bridge_bind_hosts", lambda: ("127.0.0.1",) * 2)
    monkeypatch.setattr(claude_native_bridge, "_bridge_port_pool", lambda: (28700, 28701))
    server, url = claude_native_bridge._start_bridge_http_server(BaseHTTPRequestHandler)
    try:
        assert attempts == ([28700, 28701, 0] if pool_exhausted else [28700, 28701])
        assert url == f"http://127.0.0.1:{49152 if pool_exhausted else 28701}"
        for configured in options:
            if is_windows:
                assert socket.SO_REUSEADDR not in configured
                assert configured[exclusive_option] == 1
            else:
                assert configured[socket.SO_REUSEADDR] == 1
                assert exclusive_option not in configured
        for failed_socket in sockets[:-1]:
            failed_socket.close.assert_called_once()
        sockets[-1].listen.assert_called_once()
    finally:
        server.server_close()
