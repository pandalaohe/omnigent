"""Real browser ancestor enforcement and schemeful same-site cookie delivery.

Only static SPA documents are served; backend reads use the browser contract.
"""

import ssl
import threading
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import Browser, expect

from omnigent.server.server_config import frame_ancestors_policy
from tests.browser_ui.conftest import _BUILD_OUTPUT, BrowserContract, _SpaHandler
from tests.browser_ui.test_sessions_appearance import appearance_url


@pytest.fixture
def frame_origins(tmp_path, _built_spa):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    policy = {"value": None}

    class Handler(_SpaHandler):
        def do_GET(self):
            if urlparse(self.path).path == "/mount":
                child = parse_qs(urlparse(self.path).query)["url"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f'<iframe title="Board" src="{child}"></iframe>'.encode())
                return
            super().do_GET()

        def end_headers(self):
            if policy["value"] and not self.path.startswith(("/assets/", "/mount")):
                self.send_header("Content-Security-Policy", policy["value"])
            super().end_headers()

    static = frozenset(
        f"/{p.relative_to(_BUILD_OUTPUT).as_posix()}"
        for p in _BUILD_OUTPUT.rglob("*")
        if p.is_file()
    )
    servers = []
    try:
        for _ in range(3):
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                partial(Handler, directory=str(_BUILD_OUTPUT), static_paths=static),
            )
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        origins = [f"https://localhost:{server.server_port}" for server in servers]
        cross_site = origins[1].replace("localhost", "127.0.0.1")
        policy["value"] = frame_ancestors_policy(
            {"frame_ancestors": ["'self'", origins[1], cross_site]}
        )
        yield origins, cross_site
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_signed_in_boards_and_ancestor_boundaries(browser: Browser, frame_origins):
    origins, cross_site = frame_origins
    board, parent, unlisted = origins
    with browser.new_context(ignore_https_errors=True) as context:
        contract = BrowserContract(context=context, base_url=board)
        setup = appearance_url.__wrapped__(contract, {})
        next(setup)
        contract.json(
            "/v1/info",
            {
                "accounts_enabled": True,
                "login_url": "/login",
                "task_board_enabled": True,
                "usage_board_enabled": True,
                "features": {},
            },
        )

        # The 401 is selected from the cookie that Chromium actually attached.
        def identity(route):
            signed_in = "frame_session=" in (route.request.header_value("cookie") or "")
            route.fulfill(
                status=200 if signed_in else 401,
                json={"user_id": "viewer" if signed_in else None, "login_url": "/login"},
            )

        context.route(board + "/v1/me", identity)
        contract.json("/v1/me/usage", {"accounts": []})
        contract.json("/v1/me/usage/activity", {"accounts": []})
        contract.json("/v1/scheduled-tasks", {"scheduled_tasks": []})
        contract.json("/v1/me/tasks/sources", {"sources": []})
        contract.json("/v1/me/tasks", {"tasks": []})
        contract.json("/v1/system/status", {"hosts": []})
        context.add_cookies(
            [
                {
                    "name": "frame_session",
                    "value": "synthetic",
                    "url": board,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()
        for path, heading in [
            ("/frame/task-board", "Task board"),
            ("/frame/usage/board", "Usage board"),
        ]:
            page.goto(parent + "/mount?url=" + board + path)
            frame = page.frame_locator("iframe")
            expect(frame.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            expect(frame.get_by_role("link", name="Sign in to Omnigent")).to_have_count(0)
        # Same-origin page-mount ancestor, then board, retains the cookie.
        page.goto(board + "/mount?url=" + board + "/mount?url=" + board + "/frame/task-board")
        expect(
            page.frame_locator("iframe")
            .frame_locator("iframe")
            .get_by_role("heading", name="Task board", exact=True)
        ).to_be_visible()
        violations = []
        page.on(
            "console",
            lambda message: (
                violations.append(message.text) if "frame-ancestors" in message.text else None
            ),
        )
        for url in [
            unlisted + "/mount?url=" + board + "/frame/task-board",
            unlisted + "/mount?url=" + parent + "/mount?url=" + board + "/frame/task-board",
        ]:
            with page.expect_console_message(
                predicate=lambda message: "frame-ancestors" in message.text
            ):
                page.goto(url)
            assert not any(frame.url == board + "/frame/task-board" for frame in page.frames), (
                "The refused board must never boot"
            )
        assert violations, "Chromium must reject an unlisted ancestor"
        page.goto(cross_site + "/mount?url=" + board + "/frame/task-board")
        frame = page.frame_locator("iframe")
        expect(frame.get_by_role("link", name="Sign in to Omnigent")).to_be_visible()
        expect(frame.get_by_text("Signed in and still seeing this?", exact=False)).to_be_visible()
        page.close()
        setup.close()
