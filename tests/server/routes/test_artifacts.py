"""Tests for the artifact capability-URL routes (mint / revoke / open / serve).

Drives the real sessions router with an in-memory conversation store, a
permission-store stub and either a stub runner app over a real httpx
client (success paths) or a host-tunnel auto-replier (runner-offline
paths). Bundles the silent-failure seams: containment gating on both
readers, the lexical bundle rules, the size cap, the inline panel-script
injection, and the HTML error pages.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation, ResolvedAccess
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    CAP_FS_READ_RAW,
    HostFsRequestFrame,
    HostHelloFrame,
    decode_host_frame,
)
from omnigent.runtime import (
    _globals,
    session_stream,
    set_runner_client,
    set_runner_direct_attach_resolver,
    set_runner_router,
)
from omnigent.server.artifact_links import (
    decode_artifact_token,
    encode_artifact_token,
    session_key_bytes,
)
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import artifacts as artifacts_module
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.conversation_store import ARTIFACT_LINK_KEY_LABEL

pytestmark = pytest.mark.asyncio

_SESSION_ID = "conv_artifact_session"
_HOST_ID = "host_artifact_test"
_OWNER = "owner@example.com"
_VIEWER = "viewer@example.com"
_WORKSPACE = "/workspace/artifacts"
_FS_ROUTE = (
    "/v1/sessions/{session_id}/resources/environments/{environment_id}"
    "/filesystem/{relative_path:path}"
)
_ENTRY = "reports/index.html"
_ROOT_BUNDLE_ENTRY = "index.html"
_CSP = (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox allow-modals allow-top-navigation allow-downloads"
)
_MIB = 1024 * 1024


# ── In-memory collaborators ─────────────────────────────────────────


class _ConversationStore:
    """Minimal in-memory conversation store with the label methods mint uses."""

    def __init__(self, conversations: dict[str, Conversation]) -> None:
        """Store the canned conversations, a lock, and observed workspace ids."""
        self._conversations = conversations
        self._lock = threading.Lock()
        self.seen_workspaces: list[int] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Record the ambient workspace, then return the conversation or ``None``."""
        self.seen_workspaces.append(current_workspace_id())
        return self._conversations.get(conversation_id)

    def insert_label_if_absent(self, conversation_id: str, key: str, value: str) -> str:
        """First-writer-wins label insert, mirroring the store contract."""
        conv = self._conversations[conversation_id]
        with self._lock:
            stored = conv.labels.get(key)
            if stored is None:
                conv.labels[key] = value
                stored = value
            return stored

    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """Merge label updates (used by the revoke route)."""
        del updated_at
        self._conversations[conversation_id].labels.update(updates)


class _FixedAuthProvider(AuthProvider):
    """Auth provider returning a settable, fixed user id."""

    def __init__(self, user_id: str = _OWNER) -> None:
        """Initialize with the identity every request resolves to."""
        self.user_id = user_id

    def get_user_id(self, request: Any) -> str | None:
        """Return the configured user id."""
        return self.user_id


class _PermissionStore:
    """Resolved-access stub keyed by ``(user, session)``."""

    def __init__(self) -> None:
        """Initialize with no grants."""
        self.levels: dict[tuple[str, str], int] = {}
        self.is_admin_flag = False

    def grant(self, user_id: str, session_id: str, level: int) -> None:
        """Record a direct grant."""
        self.levels[(user_id, session_id)] = level

    def resolve_access(self, user_id: str | None, conversation_id: str) -> ResolvedAccess:
        """Return the canned snapshot for the pair."""
        return ResolvedAccess(
            is_admin=self.is_admin_flag,
            user_grant_level=self.levels.get((user_id or "", conversation_id)),
            public_grant_level=None,
        )

    def is_admin(self, user_id: str) -> bool:
        """Return the canned admin flag."""
        return self.is_admin_flag

    def check_access(self, user_id: str | None, conversation_id: str, required_level: int) -> bool:
        """Return whether the pair's canned grant covers the level."""
        level = self.levels.get((user_id or "", conversation_id))
        return level is not None and level >= required_level


def _conversation(session_id: str = _SESSION_ID) -> Conversation:
    """Build the test session: owner-present workspace sharing is on."""
    return Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=session_id,
        agent_id="agent_artifact",
        workspace=_WORKSPACE,
        share_workspace_files=True,
    )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    """Reset the process-wide runner globals around each test."""
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    prior_direct = _globals._runner_direct_attach_resolver
    set_runner_client(None)
    set_runner_router(None)
    set_runner_direct_attach_resolver(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)
    set_runner_direct_attach_resolver(prior_direct)


@pytest.fixture
def app(runner_globals_reset: None) -> FastAPI:
    """Build the sessions router app with the artifact routes wired."""
    del runner_globals_reset
    store = _ConversationStore({_SESSION_ID: _conversation()})
    host_registry = HostRegistry()
    auth_provider = _FixedAuthProvider()
    permission_store = _PermissionStore()
    permission_store.grant(_OWNER, _SESSION_ID, LEVEL_OWNER)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    class _StubAgentStore:
        """Agent store stub: no agents resolve (absolute reach is mocked)."""

        def get(self, agent_id: str) -> None:
            """Return ``None`` for every agent id."""
            del agent_id

    app.include_router(
        create_sessions_router(
            store,  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
            host_registry=host_registry,
            auth_provider=auth_provider,
            permission_store=permission_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    app.state.test_store = store
    app.state.test_host_registry = host_registry
    app.state.test_auth_provider = auth_provider
    app.state.test_permission_store = permission_store
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the test app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


@pytest.fixture
def asset_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the in-frame asset lookup at an empty temp web-ui dir."""
    web_ui = tmp_path / "web-ui"
    web_ui.mkdir()
    monkeypatch.setattr(artifacts_module, "_WEB_UI_DIR", web_ui)
    monkeypatch.setattr(artifacts_module, "_SOURCE_ASSET_DIR", tmp_path / "no-source-assets")
    return web_ui


@pytest.fixture
def unconfined_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make absolute targets pass the owner gate without a real agent bundle.

    The route tests run with a stub agent store (nothing to load), so the
    spec loader is patched to an unconfined environment — the reach
    ``_authorize_absolute_browse`` resolves an absolute target against.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.spec.types import AgentSpec

    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    monkeypatch.setattr(
        sessions_module,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: spec,
    )


# ── Runner / host doubles ───────────────────────────────────────────


class _RoutedRunner:
    """Routed-runner stand-in carrying the client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Bind the runner id and client."""
        self.runner_id = "runner_one"
        self.client = client


class _RunnerRouter:
    """Router returning the stub runner for every session."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Bind the shared client."""
        self.client = client

    def client_for_session_resources(
        self,
        session_id: str,
        *,
        conversation: Conversation | None = None,
    ) -> _RoutedRunner:
        """Return the stub runner."""
        del session_id, conversation
        return _RoutedRunner(self.client)


class _OfflineRunnerRouter:
    """Router whose bound runner tunnel is never on this replica."""

    def client_for_session_resources(
        self,
        session_id: str,
        *,
        conversation: Conversation | None = None,
    ) -> _RoutedRunner:
        """Fail exactly like the real router does for an offline runner."""
        del conversation
        raise OmnigentError(
            f"runner for conversation {session_id!r} is offline",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _stub_runner_app(
    body: bytes,
    *,
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
    extra_headers: dict[str, str] | None = None,
    captured: list[dict[str, Any]] | None = None,
    streaming: bool = False,
) -> FastAPI:
    """Build a stub runner serving the filesystem download route.

    :param body: Response body.
    :param content_type: Response content type.
    :param status_code: Response status; 200 sends a download-shaped body.
    :param extra_headers: Headers merged over the attachment defaults.
    :param captured: When given, records each request's path/params.
    :param streaming: Send a body with no Content-Length (a streaming runner).
    """
    runner = FastAPI()

    @runner.get(_FS_ROUTE)
    async def _serve(
        session_id: str,
        environment_id: str,
        relative_path: str,
        download: bool = False,
        within: str | None = None,
    ) -> Response:
        del session_id, environment_id
        if captured is not None:
            captured.append(
                {"relative_path": relative_path, "download": download, "within": within}
            )
        if status_code != 200:
            return JSONResponse(
                status_code=status_code,
                content={"error": {"code": "not_found", "message": "nope"}},
            )
        headers = {"Content-Disposition": "attachment; filename=x"}
        if extra_headers:
            headers.update(extra_headers)
        if streaming:
            return StreamingResponse(iter([body]), media_type=content_type, headers=headers)
        return Response(content=body, media_type=content_type, headers=headers)

    return runner


class _ReadErrorStream(httpx.AsyncByteStream):
    """A runner body that dies after one chunk, as a dropped connection does."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("runner connection dropped")


class _ReadErrorTransport(httpx.AsyncBaseTransport):
    """Transport answering every runner download with a dying streamed body."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Return a 200 attachment whose body fails mid-read."""
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "content-disposition": "attachment; filename=x",
            },
            stream=_ReadErrorStream(),
            request=request,
        )


@contextlib.asynccontextmanager
async def _use_runner(
    runner: FastAPI | httpx.AsyncBaseTransport,
) -> AsyncIterator[httpx.AsyncClient]:
    """Install *runner* — a stub app or a raw transport — as the routed runner."""
    transport = (
        runner if isinstance(runner, httpx.AsyncBaseTransport) else httpx.ASGITransport(app=runner)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as runner_http:
        set_runner_router(_RunnerRouter(runner_http))  # type: ignore[arg-type]
        yield runner_http


class _FakeWebSocket:
    """Minimal host WebSocket stand-in."""

    async def send_text(self, data: str) -> None:
        """Accept outbound frames (they ride the registry queue)."""
        del data


@contextlib.asynccontextmanager
async def _online_host(
    registry: HostRegistry,
    *,
    capabilities: tuple[str, ...] = (CAP_FS_READ_RAW,),
    reply: dict[str, Any] | None = None,
    frames: list[Any] | None = None,
) -> AsyncIterator[None]:
    """Register a host connection that auto-replies to ``host.fs_request``."""
    conn = registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type]
        hello=HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name="artifact-host",
            capabilities=list(capabilities),
        ),
        owner=None,
    )

    async def _drain() -> None:
        while True:
            text = await conn.outbound_queue.get()
            if text is None:
                return
            frame = decode_host_frame(text)
            if frames is not None:
                frames.append(frame)
            if isinstance(frame, HostFsRequestFrame) and reply is not None:
                future = conn.pending_fs_requests.pop(frame.request_id, None)
                if future is not None and not future.done():
                    future.set_result(dict(reply))

    task = asyncio.create_task(_drain())
    try:
        yield
    finally:
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except TimeoutError:
            task.cancel()


# ── Helpers ─────────────────────────────────────────────────────────


def _conv(app: FastAPI) -> Conversation:
    """Return the test conversation."""
    return app.state.test_store.get_conversation(_SESSION_ID)


def _bind_host(app: FastAPI) -> None:
    """Bind the test conversation to the artifact test host and workspace."""
    conv = _conv(app)
    conv.host_id = _HOST_ID
    conv.workspace = _WORKSPACE


def _set_viewer(app: FastAPI, level: int = LEVEL_READ) -> None:
    """Make every request resolve to the viewer with the given grant."""
    app.state.test_auth_provider.user_id = _VIEWER
    app.state.test_permission_store.grant(_VIEWER, _SESSION_ID, level)


def _token_from_url(url: str) -> str:
    """Extract the token segment from a minted artifact URL."""
    parts = url.split("/")
    assert parts[:3] == ["", "v1", "artifacts"], url
    return parts[3]


async def _mint(
    client: httpx.AsyncClient,
    *,
    path: str = _ENTRY,
    view: str = "panel",
    base: str | None = None,
) -> dict[str, Any]:
    """Mint an artifact link through the route, asserting success."""
    body: dict[str, Any] = {"path": path, "view": view}
    if base is not None:
        body["base"] = base
    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/artifacts", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _host_file_payload(
    content: str,
    *,
    path: str = "reports/style.css",
    content_type: str = "text/css",
    within_enforced: bool = True,
    truncated: bool = False,
) -> dict[str, Any]:
    """Build a host read payload as ``WorkspaceReader.list_or_read`` returns."""
    return {
        "status": "ok",
        "payload": {
            "object": "session.environment.filesystem.file_content",
            "path": path,
            "encoding": "utf-8",
            "content": content,
            "content_type": content_type,
            "bytes": len(content.encode("utf-8")),
            "truncated": truncated,
            "within_enforced": within_enforced,
        },
    }


# ── Mint ────────────────────────────────────────────────────────────


async def test_mint_workspace_html_is_deterministic_bundle(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """An HTML entry mints a bundle token; the same target mints the same URL.

    Determinism is what makes a copied link survive until revoke/archive:
    the key and descriptor must not change between mints.
    """
    first = await _mint(client)
    second = await _mint(client)

    assert first["kind"] == "bundle"
    assert first["url"].startswith("/v1/artifacts/a1.")
    assert first["url"].endswith("/index.html")
    assert first["nonce"] == second["nonce"]
    assert first["url"] == second["url"]
    assert ARTIFACT_LINK_KEY_LABEL in _conv(app).labels


async def test_mint_non_html_is_a_file_token(client: httpx.AsyncClient) -> None:
    """A non-HTML entry mints a file token ending at the entry name."""
    minted = await _mint(client, path="notes.txt")

    assert minted["kind"] == "file"
    assert minted["url"].endswith("/notes.txt")


@pytest.mark.parametrize(
    ("path", "root"),
    [("/opt/reports/index.html", "/opt/reports"), ("/index.html", "/")],
)
async def test_mint_absolute_bundle_keeps_an_absolute_root(
    client: httpx.AsyncClient,
    unconfined_browse: None,
    path: str,
    root: str,
) -> None:
    """An absolute target's token root keeps its leading slash.

    Stripping it would root the runner under its own workspace and hand the
    host fallback's authorization a relative path the server resolves against
    its cwd — a different directory than the one authorized at mint.
    """
    minted = await _mint(client, path=path, base="host")
    claims = decode_artifact_token(_token_from_url(minted["url"]))

    assert minted["kind"] == "bundle"
    assert claims is not None
    assert claims.absolute is True
    assert claims.root == root
    assert claims.root.startswith("/")


async def test_mint_absolute_host_path_by_non_owner_is_forbidden(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """``base=host`` authorizes the final absolute target, so a viewer 403s.

    The owner gate must apply after normalization: authorizing the raw
    workspace-relative form first would let a viewer mint a link to the
    owner's machine.
    """
    _set_viewer(app)

    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts",
        json={"path": "Users/x/report.html", "base": "host", "view": "panel"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == ErrorCode.FORBIDDEN


async def test_mint_read_level_viewer_without_sharing_is_forbidden(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A read grant alone does not share workspace bytes."""
    _set_viewer(app)
    _conv(app).share_workspace_files = False

    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts",
        json={"path": _ENTRY, "view": "panel"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == ErrorCode.FORBIDDEN


async def test_mint_read_level_viewer_with_sharing_is_allowed(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """With sharing on, the read grant is enough to mint."""
    _set_viewer(app)

    minted = await _mint(client)

    assert minted["kind"] == "bundle"


async def test_mint_archived_session_conflicts(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """An archived session refuses new links with a 409 conflict."""
    _conv(app).archived = True

    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts",
        json={"path": _ENTRY, "view": "panel"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == ErrorCode.CONFLICT


async def test_mint_invalid_segments_is_rejected_before_authorization(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A ``..`` component is a 400 before any store or auth work."""
    _set_viewer(app, LEVEL_OWNER)

    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts",
        json={"path": "../secret.html", "view": "panel"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


async def test_concurrent_first_mints_converge_on_one_key(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Two racing first mints store one key and both URLs verify.

    Without the atomic insert each mint would sign with its own key and the
    loser's URL would be born revoked.
    """
    results = await asyncio.gather(
        _mint(client, path="reports/index.html"),
        _mint(client, path="reports/index.html"),
    )

    stored = _conv(app).labels.get(ARTIFACT_LINK_KEY_LABEL)
    assert stored
    runner = _stub_runner_app(b"<html><body>x</body></html>", content_type="text/html")
    async with _use_runner(runner):
        for minted in results:
            served = await client.get(minted["url"])
            assert served.status_code == 200, served.text


# ── Serve: token / lifecycle ────────────────────────────────────────


async def test_forged_token_serves_404_html_page(client: httpx.AsyncClient) -> None:
    """A flipped mac byte is a 404 HTML page — never JSON, never the SPA."""
    minted = await _mint(client, path="notes.txt")
    token = _token_from_url(minted["url"])
    header, payload, mac = token.split(".")
    bad_mac = ("B" if mac[0] != "B" else "C") + mac[1:]

    resp = await client.get(f"/v1/artifacts/{header}.{payload}.{bad_mac}/notes.txt")

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "404" in resp.text
    assert '"error"' not in resp.text


async def test_out_of_range_workspace_id_serves_404_html_page(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A token whose ``w`` overflows the workspace column is a 404 page.

    The unverified claim reaches the store lookup before the mac check, so an
    unbounded value would raise there and answer a header-less JSON 500.
    """
    await _mint(client, path="notes.txt")
    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    token = encode_artifact_token(
        key,
        session_id=_SESSION_ID,
        workspace_id=2**63,
        root="",
        absolute=False,
        entry="notes.txt",
        kind="f",
        view="r",
    )
    store = app.state.test_store
    store.seen_workspaces.clear()

    resp = await client.get(f"/v1/artifacts/{token}/notes.txt")

    assert resp.status_code == 404
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["content-security-policy"] == _CSP
    assert resp.headers["cache-control"] == "no-store"
    assert '"error"' not in resp.text
    assert store.seen_workspaces == []


async def test_revoked_token_is_gone(client: httpx.AsyncClient) -> None:
    """The revoke route rotates the key, so old URLs answer 410."""
    minted = await _mint(client)

    revoke = await client.post(f"/v1/sessions/{_SESSION_ID}/artifacts/revoke")
    served = await client.get(minted["url"])

    assert revoke.status_code == 204
    assert served.status_code == 410
    assert served.headers["content-type"].startswith("text/html")


async def test_archived_session_serves_gone(client: httpx.AsyncClient, app: FastAPI) -> None:
    """An archived session answers 410 without consulting the reader."""
    minted = await _mint(client)
    _conv(app).archived = True

    resp = await client.get(minted["url"])

    assert resp.status_code == 410


async def test_archive_then_unarchive_keeps_old_url_gone(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Unarchive does not revive links: the archive deleted the key."""
    minted = await _mint(client)
    conv = _conv(app)
    conv.archived = True
    # The store's archive branch deletes the key in the same transaction.
    conv.labels.pop(ARTIFACT_LINK_KEY_LABEL, None)
    conv.archived = False

    resp = await client.get(minted["url"])
    reminted = await _mint(client)
    runner = _stub_runner_app(b"<html><body>x</body></html>", content_type="text/html")
    async with _use_runner(runner):
        served = await client.get(reminted["url"])

    assert resp.status_code == 410
    assert served.status_code == 200


async def test_serve_looks_up_the_conversation_in_the_tokens_workspace(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The credential-free serve rebinds the token's minting workspace.

    Stores scope every row by ``current_workspace_id()``; without the rebind
    a link minted in workspace N would look its conversation up in whatever
    workspace the bare request implies and find nothing.
    """
    await _mint(client)
    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    token = encode_artifact_token(
        key,
        session_id=_SESSION_ID,
        workspace_id=7,
        root="reports",
        absolute=False,
        entry="index.html",
        kind="b",
        view="p",
    )
    store = app.state.test_store
    store.seen_workspaces.clear()
    runner = _stub_runner_app(b"<html><body>x</body></html>", content_type="text/html")

    async with _use_runner(runner):
        resp = await client.get(f"/v1/artifacts/{token}/index.html")

    assert resp.status_code == 200
    assert store.seen_workspaces == [7]
    assert current_workspace_id() == 0


# ── Serve: path rules ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_relpath",
    ["%2e%2e/secret.txt", "..%2Fsecret.txt", "a//b.css", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"],
)
async def test_bundle_traversal_paths_are_not_found(
    client: httpx.AsyncClient,
    bad_relpath: str,
) -> None:
    """Traversal and empty segments never reach the reader."""
    minted = await _mint(client)
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(b"x", captured=captured)

    async with _use_runner(runner):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/{bad_relpath}")

    assert resp.status_code == 404
    assert captured == []


async def test_file_token_rejects_a_different_relpath(client: httpx.AsyncClient) -> None:
    """A file token serves exactly its entry."""
    minted = await _mint(client, path="notes.txt")

    resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/other.txt")

    assert resp.status_code == 404


@pytest.mark.parametrize("name", [".env", ".git/config", "keys/id_rsa"])
async def test_bundle_subresource_key_names_are_not_found_without_reader(
    client: httpx.AsyncClient,
    name: str,
) -> None:
    """Dotfiles, dot-directories and key-material names 404 before any read."""
    minted = await _mint(client)
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(b"x", captured=captured)

    async with _use_runner(runner):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/{name}")

    assert resp.status_code == 404
    assert captured == []


async def test_dotfile_entry_is_served_as_a_file_token(
    client: httpx.AsyncClient,
) -> None:
    """Opening a dotfile directly (D7) mints a file token and serves it."""
    minted = await _mint(client, path=".env")
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(b"SECRET=1", content_type="text/plain", captured=captured)

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert minted["kind"] == "file"
    assert resp.status_code == 200
    assert resp.content == b"SECRET=1"
    # A file token never sends ``within``: the name filter does not apply to
    # the entry the owner explicitly opened.
    assert captured[0]["relative_path"] == ".env"
    assert captured[0]["within"] is None


async def test_workspace_root_bundle_sends_empty_within(
    client: httpx.AsyncClient,
) -> None:
    """A root-entry bundle passes ``within=""`` for a sub-resource.

    The empty value must arrive as an empty string (the workspace root),
    not be dropped, or the runner would serve unconfined and the server
    would reject the missing confirmation.
    """
    minted = await _mint(client, path=_ROOT_BUNDLE_ENTRY)
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(
        b"body{}",
        content_type="text/css",
        extra_headers={"X-Omnigent-Within": "enforced"},
        captured=captured,
    )

    async with _use_runner(runner):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 200
    assert captured[0]["within"] == ""
    assert captured[0]["relative_path"] == "style.css"


async def test_absolute_bundle_subresource_through_runner_keeps_absolute_target(
    client: httpx.AsyncClient,
    unconfined_browse: None,
) -> None:
    """A runner sub-resource read keeps the absolute target and absolute within.

    The ``%2F``-marked path is what makes the runner resolve the file outside
    its workspace, and ``within`` must name the same absolute parent the mint
    was authorized for.
    """
    minted = await _mint(client, path="/opt/reports/index.html", base="host")
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(
        b"body{}",
        content_type="text/css",
        extra_headers={"X-Omnigent-Within": "enforced"},
        captured=captured,
    )

    async with _use_runner(runner):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 200
    assert captured[0]["relative_path"] == "/opt/reports/style.css"
    assert captured[0]["within"] == "/opt/reports"


# ── Serve: reader gating and fallback ───────────────────────────────


async def test_runner_response_without_within_header_is_bad_gateway(
    client: httpx.AsyncClient,
) -> None:
    """An old runner that ignores ``within`` fails closed with 502."""
    minted = await _mint(client)
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(b"body{}", content_type="text/css", captured=captured)

    async with _use_runner(runner):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 502
    assert "runner needs an update" in resp.text
    assert captured[0]["within"] == "reports"


async def test_bundle_entry_does_not_require_within_header(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """The entry itself is authorized at mint; no containment header applies."""
    body = b"<html><body>plain</body></html>"
    runner = _stub_runner_app(body, content_type="text/html", extra_headers={})

    minted = await _mint(client)
    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.content == body


async def test_runner_offline_host_without_capability_is_bad_gateway(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A connected host that cannot raw-read fails closed with 502."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client)

    async with _online_host(app.state.test_host_registry, capabilities=()):
        resp = await client.get(minted["url"])

    assert resp.status_code == 502
    assert "host needs an update" in resp.text


async def test_runner_offline_host_serves_full_text_with_raw_and_within(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The host fallback returns whole text and carries raw + within."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client)
    text = "".join(f"line {index}\n" for index in range(3000))
    frames: list[Any] = []

    async with _online_host(
        app.state.test_host_registry,
        reply=_host_file_payload(text),
        frames=frames,
    ):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 200, resp.text
    assert resp.text == text
    frame = frames[0]
    assert isinstance(frame, HostFsRequestFrame)
    assert frame.params["raw"] is True
    assert frame.params["within"] == "reports"
    assert frame.params["path"] == "reports/style.css"
    assert frame.workspace == _WORKSPACE


async def test_absolute_bundle_subresource_through_host_roots_at_absolute_parent(
    client: httpx.AsyncClient,
    app: FastAPI,
    unconfined_browse: None,
) -> None:
    """The offline host is rooted at the authorized absolute parent.

    The authorization gets the absolute root the mint stored, the reader gets
    the sub-resource relative to that root, and ``within=""`` names the root
    itself — the same shape as the live absolute browse.
    """
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client, path="/opt/reports/index.html", base="host")
    frames: list[Any] = []

    async with _online_host(
        app.state.test_host_registry,
        reply=_host_file_payload("body{}", path="style.css"),
        frames=frames,
    ):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 200, resp.text
    frame = frames[0]
    assert isinstance(frame, HostFsRequestFrame)
    assert frame.workspace == "/opt/reports"
    assert frame.params["path"] == "style.css"
    assert frame.params["within"] == ""
    assert frame.params["raw"] is True


async def test_host_payload_without_within_enforced_is_bad_gateway(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """An old host that ignores ``within`` fails closed with 502."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client)

    async with _online_host(
        app.state.test_host_registry,
        reply=_host_file_payload("x", within_enforced=False),
    ):
        resp = await client.get(f"{minted['url'].rsplit('/', 1)[0]}/style.css")

    assert resp.status_code == 502
    assert "host needs an update" in resp.text


async def test_host_truncated_payload_is_payload_too_large(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A truncated host read is refused rather than served silently short."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client)

    async with _online_host(
        app.state.test_host_registry,
        reply=_host_file_payload("x", truncated=True),
    ):
        resp = await client.get(minted["url"])

    assert resp.status_code == 413


async def test_host_listing_payload_is_not_found(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A directory target is a 404 — artifact links never list."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    _bind_host(app)
    minted = await _mint(client)
    reply = {
        "status": "ok",
        "payload": {"object": "list", "data": [], "has_more": False},
    }

    async with _online_host(app.state.test_host_registry, reply=reply):
        resp = await client.get(minted["url"])

    assert resp.status_code == 404


async def test_no_runner_and_no_host_is_unavailable(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Neither reader available answers 503 (host offline)."""
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    minted = await _mint(client)

    resp = await client.get(minted["url"])

    assert resp.status_code == 503
    assert "host is offline" in resp.text


async def test_runner_content_length_over_cap_is_payload_too_large(
    client: httpx.AsyncClient,
) -> None:
    """A declared size over the cap is a 413 before any streaming starts."""
    runner = _stub_runner_app(
        b"tiny",
        content_type="text/plain",
        extra_headers={"Content-Length": str(11 * _MIB)},
    )
    minted = await _mint(client, path="notes.txt")

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 413


async def test_runner_body_over_cap_without_content_length_is_payload_too_large(
    client: httpx.AsyncClient,
) -> None:
    """A body without Content-Length is buffered bounded and refused past cap."""
    runner = _stub_runner_app(
        b"x" * (11 * _MIB),
        content_type="application/octet-stream",
        streaming=True,
    )
    minted = await _mint(client, path="notes.txt")

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 413


async def test_runner_read_error_while_buffering_is_bad_gateway_page(
    client: httpx.AsyncClient,
) -> None:
    """A runner body that dies mid-read stays inside the HTML error contract.

    The read is buffered (no Content-Length), so the ``httpx.ReadError``
    surfaces at the server, not in an already-returned stream: it must be
    answered with the 502 page and the artifact security headers, never JSON.
    """
    minted = await _mint(client, path="notes.txt")

    async with _use_runner(_ReadErrorTransport()):
        resp = await client.get(minted["url"])

    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-security-policy"] == _CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "could not be loaded" in resp.text
    assert '"error"' not in resp.text


# ── Serve: headers and injection ────────────────────────────────────


async def test_success_headers_are_complete(client: httpx.AsyncClient) -> None:
    """Every success carries the isolation, CORS and caching headers."""
    runner = _stub_runner_app(b"plain text", content_type="text/plain")
    minted = await _mint(client, path="notes.txt", view="raw")

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"] == "inline"
    assert resp.headers["content-security-policy"] == _CSP
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex"


async def test_error_page_headers_are_complete(client: httpx.AsyncClient) -> None:
    """An error page carries the same security headers as a success."""
    resp = await client.get("/v1/artifacts/a1.bad.token/nothing.html")

    assert resp.status_code == 404
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"] == "inline"
    assert resp.headers["content-security-policy"] == _CSP
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex"


async def test_preflight_options_is_success_with_cors_headers(
    client: httpx.AsyncClient,
) -> None:
    """OPTIONS answers 204 and echoes the requested headers."""
    resp = await client.options(
        "/v1/artifacts/a1.x.y/index.html",
        headers={"Access-Control-Request-Headers": "x-custom, content-type"},
    )

    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["access-control-allow-methods"] == "GET, OPTIONS"
    assert resp.headers["access-control-allow-headers"] == "x-custom, content-type"
    assert resp.headers["access-control-max-age"] == "600"


async def test_preflight_options_defaults_headers_to_wildcard(
    client: httpx.AsyncClient,
) -> None:
    """Without a requested-headers header the preflight allows ``*``."""
    resp = await client.options("/v1/artifacts/a1.x.y/index.html")

    assert resp.status_code == 204
    assert resp.headers["access-control-allow-headers"] == "*"


async def test_panel_html_injects_script_with_nonce(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """Panel-view HTML gets the inline bridge with the mint's nonce."""
    asset = "window.__omniBridge = 1;\n"
    (asset_dir / "omni-html-bridge.js").write_text(asset, encoding="utf-8")
    body = b"<html><body><h1>hi</h1></body></html>"
    runner = _stub_runner_app(body, content_type="text/html")
    minted = await _mint(client)

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    expected = f'<script data-omni-nonce="{minted["nonce"]}">{asset}</script>'
    assert resp.status_code == 200
    assert expected in resp.text
    assert resp.text.index(expected) < resp.text.index("</body>")
    assert int(resp.headers["content-length"]) == len(resp.content)


async def test_raw_view_html_is_byte_identical(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """A raw-view token streams the pristine bytes: no script, no rewrite."""
    (asset_dir / "omni-html-bridge.js").write_text("bridge();", encoding="utf-8")
    body = b"<html><body>raw</body></html>"
    runner = _stub_runner_app(body, content_type="text/html")
    minted = await _mint(client, view="raw")

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.content == body
    assert "data-omni-nonce" not in resp.text


async def test_panel_html_without_asset_is_served_unmodified(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """A missing bridge asset degrades to no injection, never an error."""
    body = b"<html><body>plain</body></html>"
    runner = _stub_runner_app(body, content_type="text/html")
    minted = await _mint(client)

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.content == body


async def test_asset_script_close_is_escaped(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """An embedded ``</script`` cannot close the injected tag early."""
    (asset_dir / "omni-html-bridge.js").write_text(
        'var s = "</script>";\n',
        encoding="utf-8",
    )
    body = b"<html><body>x</body></html>"
    runner = _stub_runner_app(body, content_type="text/html")
    minted = await _mint(client)

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert r"<\/script>" in resp.text
    assert resp.text.count("</script>") == 1


# ── Open ────────────────────────────────────────────────────────────


async def test_open_publishes_event_without_url(client: httpx.AsyncClient) -> None:
    """The open route publishes one event carrying path + base and no URL."""
    stream = session_stream.subscribe(_SESSION_ID, ready_event={"type": "test.ready"})
    try:
        assert await anext(stream) == {"type": "test.ready"}
        resp = await client.post(
            f"/v1/sessions/{_SESSION_ID}/artifacts/open",
            json={"path": "reports/index.html"},
        )
        event = await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"viewers": 1}
    assert event == {
        "type": "artifact.open_request",
        "path": "reports/index.html",
        "base": "workspace",
        "sequence_number": None,
    }
    assert "url" not in event


async def test_open_absolute_path_carries_host_base(client: httpx.AsyncClient) -> None:
    """An absolute/open path keeps its leading slash and names the host base."""
    stream = session_stream.subscribe(_SESSION_ID, ready_event={"type": "test.ready"})
    try:
        assert await anext(stream) == {"type": "test.ready"}
        await client.post(
            f"/v1/sessions/{_SESSION_ID}/artifacts/open",
            json={"path": "Users/x/report.html", "base": "host"},
        )
        event = await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()

    assert event["path"] == "/Users/x/report.html"
    assert event["base"] == "host"


async def test_open_requires_edit_level(client: httpx.AsyncClient, app: FastAPI) -> None:
    """A read-only viewer cannot ask the UI to open files."""
    _set_viewer(app)

    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts/open",
        json={"path": "reports/index.html"},
    )

    assert resp.status_code == 403


# ── Label hygiene ───────────────────────────────────────────────────


async def test_label_key_never_appears_in_responses(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """No response body leaks the artifact-link key label or its value."""
    minted = await _mint(client)
    opened = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts/open",
        json={"path": _ENTRY},
    )
    revoke = await client.post(f"/v1/sessions/{_SESSION_ID}/artifacts/revoke")
    key_value = _conv(app).labels[ARTIFACT_LINK_KEY_LABEL]

    assert key_value
    for text in (json.dumps(minted), opened.text, revoke.text):
        assert ARTIFACT_LINK_KEY_LABEL not in text
        assert key_value not in text
