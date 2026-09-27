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
import base64
import contextlib
import json
import re
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation, ResolvedAccess, SessionPermission
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
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.artifact_links import (
    bridge_nonce,
    decode_artifact_token,
    encode_artifact_token,
    generate_session_key,
    session_key_bytes,
)
from omnigent.server.artifact_sharing import (
    KEEP_SHARE_CODE,
    ArtifactSharingStore,
    KeepShareCode,
    gate_key_id,
    grant_valid,
    remember_cookie_name,
)
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider, UnifiedAuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import artifacts as artifacts_module
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import ARTIFACT_LINK_KEY_LABEL

pytestmark = pytest.mark.asyncio

# The real comment store encodes ``conversation_id`` as a 32-char hex uuid,
# so the fixture session uses that shape.
_SESSION_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
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
_GATE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'self'; base-uri 'none'"
)
_MIB = 1024 * 1024


# ── In-memory collaborators ─────────────────────────────────────────


class _ConversationStore:
    """Minimal in-memory conversation store with the label methods mint uses."""

    def __init__(self, conversations: dict[str, Conversation], storage_location: str = "") -> None:
        """Store the canned conversations, a lock, and observed workspace ids.

        ``storage_location`` backs the gate's owner-settings store, which the
        route builds from the conversation store just like the real one.
        """
        self._conversations = conversations
        self.storage_location = storage_location
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

    def list_for_session(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        after_user_id: str | None = None,
    ) -> tuple[list[SessionPermission], str | None]:
        """Return the canned grants, ordered by user id like the store."""
        grants = [
            SessionPermission(user_id=user_id, conversation_id=cid, level=level)
            for (user_id, cid), level in sorted(self.levels.items())
            if cid == conversation_id and (after_user_id is None or user_id > after_user_id)
        ]
        return grants[:limit], None

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


def _build_app(
    store: _ConversationStore,
    *,
    auth_provider: AuthProvider | None,
    permission_store: _PermissionStore | None,
    host_registry: HostRegistry | None = None,
    comment_store: SqlAlchemyCommentStore | None = None,
) -> FastAPI:
    """Build the sessions router app with the artifact routes wired."""
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
            comment_store=comment_store,
        ),
        prefix="/v1",
    )
    app.state.test_store = store
    app.state.test_comment_store = comment_store
    app.state.test_host_registry = host_registry
    app.state.test_auth_provider = auth_provider
    app.state.test_permission_store = permission_store
    return app


@pytest.fixture
def app(runner_globals_reset: None, db_uri: str) -> FastAPI:
    """Build the sessions router app with the artifact routes wired."""
    del runner_globals_reset
    store = _ConversationStore({_SESSION_ID: _conversation()}, storage_location=db_uri)
    host_registry = HostRegistry()
    auth_provider = _FixedAuthProvider()
    permission_store = _PermissionStore()
    permission_store.grant(_OWNER, _SESSION_ID, LEVEL_OWNER)
    app = _build_app(
        store,
        auth_provider=auth_provider,
        permission_store=permission_store,
        host_registry=host_registry,
        comment_store=SqlAlchemyCommentStore(db_uri),
    )
    app.state.test_db_uri = db_uri
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


_VISIT_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<script type="module" crossorigin src="./assets/visit-abc.js"></script>
<link rel="modulepreload" crossorigin href="./assets/visit-vendor.js">
<link rel="stylesheet" crossorigin href="./assets/visit-abc.css">
</head><body><div id="app"></div></body></html>
"""

_VISIT_CONFIG_RE = re.compile(
    r'<script type="application/json" id="omni-visit-config">(.*?)</script>',
    re.DOTALL,
)


def _write_visit_shell(asset_dir: Path) -> None:
    """Write a minimal built visit entry into the fake dist."""
    (asset_dir / "visit.html").write_text(_VISIT_SHELL, encoding="utf-8")


def _visit_config(html: str) -> dict[str, Any]:
    """Extract and parse the served visit config."""
    match = _VISIT_CONFIG_RE.search(html)
    assert match is not None, html
    return json.loads(match.group(1))  # type: ignore[no-any-return]


def _comments(app: FastAPI) -> list[Any]:
    """All comment rows stored for the test session."""
    store: SqlAlchemyCommentStore = app.state.test_comment_store
    return store.list_for_conversation(_SESSION_ID)


# ── Mint ────────────────────────────────────────────────────────────


async def test_mint_workspace_html_is_deterministic_bundle(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A raw-view HTML entry mints a bundle token; the same target mints the same URL.

    Determinism is what makes a copied link survive until revoke/archive:
    the key and descriptor must not change between mints. Panel tokens
    carry an expiry claim, so they vary by mint second; raw-view tokens
    carry none.
    """
    first = await _mint(client, view="raw")
    second = await _mint(client, view="raw")

    assert first["kind"] == "bundle"
    assert first["url"].startswith("/v1/artifacts/a1.")
    assert first["url"].endswith("/index.html")
    assert first["expires_at"] is None
    assert first["nonce"] == second["nonce"]
    assert first["url"] == second["url"]
    assert ARTIFACT_LINK_KEY_LABEL in _conv(app).labels


async def test_mint_non_html_is_a_file_token(client: httpx.AsyncClient) -> None:
    """A non-HTML entry mints a file token ending at the entry name."""
    minted = await _mint(client, path="notes.txt")

    assert minted["kind"] == "file"
    assert minted["url"].endswith("/notes.txt")


async def test_mint_visit_html_is_a_deterministic_shell_token(
    client: httpx.AsyncClient,
) -> None:
    """A visit mint for an HTML entry is a ``g`` bundle token without expiry."""
    first = await _mint(client, view="visit")
    second = await _mint(client, view="visit")
    claims = decode_artifact_token(_token_from_url(first["url"]))

    assert first["kind"] == "bundle"
    assert first["expires_at"] is None
    assert first["url"] == second["url"]
    assert claims is not None
    assert claims.view == "g"
    assert claims.expires_at is None


async def test_mint_visit_non_html_falls_back_to_the_raw_view(
    client: httpx.AsyncClient,
) -> None:
    """A visit mint never widens a non-HTML target past the raw view."""
    minted = await _mint(client, path="notes.txt", view="visit")
    claims = decode_artifact_token(_token_from_url(minted["url"]))

    assert minted["kind"] == "file"
    assert claims is not None
    assert claims.view == "r"


async def test_mint_unknown_view_is_rejected(client: httpx.AsyncClient) -> None:
    """Only ``panel``, ``raw`` and ``visit`` are accepted views."""
    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/artifacts",
        json={"path": _ENTRY, "view": "sideways"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


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
        expires_at=now_epoch() + 3600,
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


# ── Serve: visitor shell (g) and frame (h) ──────────────────────────


async def test_visit_shell_rebases_assets_and_stamps_a_nonce(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """The g shell serves visit.html with dist assets, nonce CSP and config."""
    _write_visit_shell(asset_dir)
    minted = await _mint(client, view="visit")
    g_token = _token_from_url(minted["url"])

    resp = await client.get(minted["url"])

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    # Relative URLs would resolve under /v1/artifacts/<g>/ and fetch bundle
    # files; the dist form is absolute.
    assert 'src="/assets/visit-abc.js"' in resp.text
    assert 'href="/assets/visit-vendor.js"' in resp.text
    assert "./assets/" not in resp.text
    nonce_match = re.search(r'<script nonce="([^"]+)" type="module"', resp.text)
    assert nonce_match is not None, resp.text
    nonce = nonce_match.group(1)
    assert f'<link nonce="{nonce}" rel="modulepreload"' in resp.text
    assert resp.headers["content-security-policy"] == (
        f"default-src 'none'; script-src 'nonce-{nonce}' 'strict-dynamic'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-src 'self'; form-action 'none'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    config = _visit_config(resp.text)
    assert config["token"] == g_token
    assert config["grant"] is None
    assert config["path"] == "index.html"
    assert config["commentsEnabled"] is True
    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    h_token = config["frameUrl"].split("/")[3]
    h_claims = decode_artifact_token(h_token)
    assert h_claims is not None
    assert h_claims.view == "h"
    assert h_claims.entry == "index.html"
    assert config["frameUrl"] == f"/v1/artifacts/{h_token}/index.html"
    assert config["nonce"] == bridge_nonce(key, h_token)


async def test_visit_shell_config_json_is_html_escaped(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """A crafted path cannot close the config script or break out of it."""
    _write_visit_shell(asset_dir)
    minted = await _mint(client, view="visit")
    prefix = minted["url"].rsplit("/", 1)[0]

    resp = await client.get(f"{prefix}/a%3Cb.html")

    assert resp.status_code == 200, resp.text
    assert '"path":"a\\u003cb.html"' in resp.text
    assert "<b.html" not in resp.text


async def test_visit_shell_rebases_for_the_configured_base_path(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """Assets and the frame URL carry the deployment base path."""
    _write_visit_shell(asset_dir)
    app.state.base_path = "/proxy/6767"
    minted = await _mint(client, view="visit")

    resp = await client.get(minted["url"])

    assert 'src="/proxy/6767/assets/visit-abc.js"' in resp.text
    # The base-path script the index rewrite injects is executable, so the
    # stamping must cover it too or the shell cannot learn its prefix.
    assert re.search(r'<script nonce="[^"]+">window\.__OMNIGENT_BASE_PATH__', resp.text)
    config = _visit_config(resp.text)
    assert config["frameUrl"].startswith("/proxy/6767/v1/artifacts/")


async def test_visit_shell_gated_carries_grants_for_g_and_h(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """A share-code gate yields a grant for the shell and one for the frame.

    The grants must bind the gate key the unlock actually passed; the frame
    grant travels on the h URL, the shell grant in the config.
    """
    _write_visit_shell(asset_dir)
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None
    g_token = _token_from_url(minted["url"])

    unlock = await client.post(minted["url"], data={"code": "open-sesame"})
    resp = await client.get(unlock.headers["location"])

    assert unlock.status_code == 303
    assert resp.status_code == 200, resp.text
    config = _visit_config(resp.text)
    assert config["token"] == g_token
    record = _sharing_store(app).read_sharing(_OWNER)
    assert record is not None
    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    identifier = gate_key_id(record.gate_key)
    assert config["grant"] is not None
    assert grant_valid(key, g_token, config["grant"], identifier, now_epoch())
    h_segment = config["frameUrl"].split("/")[3]
    h_token, separator, h_grant = h_segment.partition("~")
    assert separator and h_grant
    assert grant_valid(key, h_token, h_grant, identifier, now_epoch())


async def test_visit_shell_absent_dist_entry_is_unavailable(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """No built visit.html answers the g request with a clear 503 page."""
    assert not (asset_dir / "visit.html").exists()
    minted = await _mint(client, view="visit")

    resp = await client.get(minted["url"])

    assert resp.status_code == 503
    assert "visitor page is not available" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


async def test_visit_shell_gated_request_meets_the_form(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """A bare g URL under a share-code gate gets the form, never the shell."""
    _write_visit_shell(asset_dir)
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None

    resp = await client.get(minted["url"])

    assert resp.status_code == 401
    assert '<form method="post">' in resp.text


async def test_visit_shell_archived_is_gone(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """An archived session's shell is a 410 page."""
    _write_visit_shell(asset_dir)
    minted = await _mint(client, view="visit")
    _conv(app).archived = True

    resp = await client.get(minted["url"])

    assert resp.status_code == 410


@pytest.mark.parametrize("view", ["g", "h"])
async def test_gate_closed_visitor_views_meet_the_form(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
    view: Literal["g", "h"],
) -> None:
    """A bare visitor-view URL under a share-code gate gets the form."""
    _write_visit_shell(asset_dir)
    _set_sharing(app, share_code="open-sesame")
    await _mint(client)  # materialize the session key for the crafted token
    app.state.test_auth_provider.user_id = None

    resp = await client.get(_raw_token_url(app, view=view))

    assert resp.status_code == 401
    assert '<form method="post">' in resp.text


async def test_share_code_change_cuts_an_h_frame(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """A code change voids the h frame's grant; the frame meets the form."""
    (asset_dir / "omni-html-bridge.js").write_text("bridge();", encoding="utf-8")
    _write_visit_shell(asset_dir)
    _set_sharing(app, share_code="old-code")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None
    unlock = await client.post(minted["url"], data={"code": "old-code"})
    shell = await client.get(unlock.headers["location"])
    frame_url = _visit_config(shell.text)["frameUrl"]
    runner = _stub_runner_app(b"<html><body>x</body></html>", content_type="text/html")

    async with _use_runner(runner):
        before = await client.get(frame_url)
    _set_sharing(app, share_code="new-code")
    after = await client.get(frame_url)

    assert unlock.status_code == 303
    assert before.status_code == 200, before.text
    assert after.status_code == 401
    assert '<form method="post">' in after.text


async def test_g_view_non_html_serves_bytes(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """A g request for a non-HTML path stays raw bytes, not the shell."""
    _write_visit_shell(asset_dir)
    await _mint(client)  # materialize the session key for the crafted token
    url = _raw_token_url(app, entry="notes.txt", view="g")
    runner = _stub_runner_app(b"plain", content_type="text/plain")

    async with _use_runner(runner):
        resp = await client.get(url)

    assert resp.status_code == 200
    assert resp.content == b"plain"
    assert "omni-visit-config" not in resp.text


async def test_h_view_html_injects_the_bridge_with_its_own_nonce(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """The h frame injects the bridge under the h token's nonce, as p does."""
    asset = "window.__omniBridge = 1;\n"
    (asset_dir / "omni-html-bridge.js").write_text(asset, encoding="utf-8")
    await _mint(client)
    url = _raw_token_url(app, view="h")
    h_token = url.split("/")[3]
    body = b"<html><body><h1>hi</h1></body></html>"
    runner = _stub_runner_app(body, content_type="text/html")

    async with _use_runner(runner):
        resp = await client.get(url)

    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    expected = f'<script data-omni-nonce="{bridge_nonce(key, h_token)}">{asset}</script>'
    assert resp.status_code == 200
    assert expected in resp.text
    assert "omni-visit-config" not in resp.text


async def test_unlock_for_g_issues_a_grant_that_serves_the_shell(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """The unlock POST for a g token 303s onto a grant; the shell follows."""
    _write_visit_shell(asset_dir)
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None

    unlock = await client.post(minted["url"], data={"code": "open-sesame"})
    served = await client.get(unlock.headers["location"])

    assert unlock.status_code == 303
    assert "~" in unlock.headers["location"]
    assert served.status_code == 200
    assert "omni-visit-config" in served.text


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


# ── Gate: settings ──────────────────────────────────────────────────


def _sharing_store(app: FastAPI) -> ArtifactSharingStore:
    """The gate store bound to the app's test database."""
    return ArtifactSharingStore(app.state.test_db_uri)


def _b64url(data: bytes) -> str:
    """base64url without padding, as the grant wire form uses."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _set_sharing(
    app: FastAPI,
    *,
    owner: str = _OWNER,
    external: bool | None = None,
    share_code: str | None | KeepShareCode = KEEP_SHARE_CODE,
    allow_comments: bool | None = None,
) -> None:
    """Write an owner's sharing record through the real store."""
    _sharing_store(app).write_sharing(
        owner,
        external=external,
        share_code=share_code,
        allow_comments=allow_comments,
    )


def _raw_token_url(
    app: FastAPI,
    *,
    entry: str = "reports/index.html",
    view: Literal["r", "g", "h"] = "r",
) -> str:
    """Craft a token for *entry* without going through the mint."""
    key = session_key_bytes(_conv(app).labels[ARTIFACT_LINK_KEY_LABEL])
    token = encode_artifact_token(
        key,
        session_id=_SESSION_ID,
        workspace_id=0,
        root=entry.rsplit("/", 1)[0] if "/" in entry else "",
        absolute=False,
        entry=entry.rsplit("/", 1)[-1],
        kind="b" if entry.endswith((".html", ".htm")) else "f",
        view=view,
    )
    return f"/v1/artifacts/{token}/{urllib.parse.quote(entry.rsplit('/', 1)[-1])}"


async def test_artifact_sharing_get_defaults_to_open(client: httpx.AsyncClient) -> None:
    """No row means external on, no code and visitor comments on."""
    resp = await client.get("/v1/artifact-sharing")

    assert resp.status_code == 200
    assert resp.json() == {
        "external": True,
        "share_code_set": False,
        "allow_comments": True,
    }


async def test_artifact_sharing_put_round_trip(client: httpx.AsyncClient) -> None:
    """A PUT merges the fields it carries and the GET reflects them."""
    put = await client.put(
        "/v1/artifact-sharing",
        json={"external": False, "share_code": "open-sesame", "allow_comments": False},
    )

    assert put.status_code == 200
    assert put.json() == {
        "external": False,
        "share_code_set": True,
        "allow_comments": False,
    }
    assert (await client.get("/v1/artifact-sharing")).json() == put.json()


async def test_artifact_sharing_put_absent_field_keeps_its_value(
    client: httpx.AsyncClient,
) -> None:
    """A body without ``share_code`` leaves the stored code alone."""
    await client.put("/v1/artifact-sharing", json={"share_code": "open-sesame"})

    put = await client.put("/v1/artifact-sharing", json={"external": False})

    assert put.json() == {
        "external": False,
        "share_code_set": True,
        "allow_comments": True,
    }


async def test_artifact_sharing_allow_comments_only_write_keeps_the_gate_key(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Flipping visitor comments does not sign open visitors out."""
    await client.put("/v1/artifact-sharing", json={"share_code": "open-sesame"})
    before = _sharing_store(app).read_sharing(_OWNER)
    assert before is not None

    put = await client.put("/v1/artifact-sharing", json={"allow_comments": False})

    after = _sharing_store(app).read_sharing(_OWNER)
    assert put.json()["allow_comments"] is False
    assert after is not None
    assert after.gate_key == before.gate_key
    assert after.allow_comments is False


async def test_artifact_sharing_put_rejects_a_non_bool_allow_comments(
    client: httpx.AsyncClient,
) -> None:
    """``allow_comments`` must be a boolean when present."""
    resp = await client.put("/v1/artifact-sharing", json={"allow_comments": "yes"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("code", ["abc", "x" * 65])
async def test_artifact_sharing_put_rejects_bad_code_length(
    client: httpx.AsyncClient, code: str
) -> None:
    """A code shorter than 4 or longer than 64 characters is a 400."""
    resp = await client.put("/v1/artifact-sharing", json={"share_code": code})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


async def test_artifact_sharing_put_rejects_bad_types(client: httpx.AsyncClient) -> None:
    """``external`` must be a bool and ``share_code`` a string or null."""
    for body in ({"external": "yes"}, {"share_code": 7}):
        resp = await client.put("/v1/artifact-sharing", json=body)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == ErrorCode.INVALID_INPUT


async def test_artifact_sharing_put_requires_json_content_type(
    client: httpx.AsyncClient,
) -> None:
    """A non-JSON content type is refused before the body is parsed."""
    resp = await client.put(
        "/v1/artifact-sharing",
        content='{"external": false}',
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 415


async def test_artifact_sharing_put_null_clears_the_code(client: httpx.AsyncClient) -> None:
    """An explicit null clears the stored code and keeps the switch."""
    await client.put("/v1/artifact-sharing", json={"external": False, "share_code": "a-code"})

    put = await client.put("/v1/artifact-sharing", json={"share_code": None})

    assert put.json() == {
        "external": False,
        "share_code_set": False,
        "allow_comments": True,
    }


async def test_artifact_sharing_responses_never_carry_the_secrets(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The stored hash and gate key appear in no settings response."""
    put = await client.put(
        "/v1/artifact-sharing",
        json={"external": False, "share_code": "open-sesame"},
    )
    get = await client.get("/v1/artifact-sharing")
    record = _sharing_store(app).read_sharing(_OWNER)

    assert record is not None
    assert record.code_hash is not None
    for response in (put, get):
        assert set(response.json()) == {"external", "share_code_set", "allow_comments"}
        assert record.gate_key not in response.text
        assert record.code_hash not in response.text


# ── Gate: mint expiry ───────────────────────────────────────────────


async def test_mint_panel_token_carries_expiry_and_raw_does_not(
    client: httpx.AsyncClient,
) -> None:
    """Panel mints an expiry claim; raw mints stay deterministic."""
    panel = await _mint(client)
    raw = await _mint(client, view="raw")
    claims = decode_artifact_token(_token_from_url(panel["url"]))

    assert isinstance(panel["expires_at"], int)
    assert panel["expires_at"] > now_epoch()
    assert claims is not None
    assert claims.expires_at == panel["expires_at"]
    assert raw["expires_at"] is None


async def test_expired_panel_token_is_gone(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A panel token past its expiry is a 410 page even with the gate open."""
    stored_key = generate_session_key()
    _conv(app).labels[ARTIFACT_LINK_KEY_LABEL] = stored_key
    token = encode_artifact_token(
        session_key_bytes(stored_key),
        session_id=_SESSION_ID,
        workspace_id=0,
        root="reports",
        absolute=False,
        entry="index.html",
        kind="b",
        view="p",
        expires_at=now_epoch() - 1,
    )

    resp = await client.get(f"/v1/artifacts/{token}/index.html")

    assert resp.status_code == 410
    assert "expired" in resp.text


async def test_panel_token_passes_a_closed_gate_without_a_redirect(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The panel is never redirected: its own token carries the permission."""
    _set_sharing(app, external=False, share_code="open-sesame")
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client)  # panel view
    app.state.test_auth_provider.user_id = None

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.content == b"plain"


# ── Gate: decision table ────────────────────────────────────────────


async def test_gate_open_serves_a_bare_raw_token(
    client: httpx.AsyncClient,
    asset_dir: Path,
) -> None:
    """No settings row: the bare raw token serves with no redirect."""
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")

    async with _use_runner(runner):
        resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert resp.content == b"plain"


async def test_gate_switch_off_denies_logged_out_visitors(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """External off: a visitor with no identity meets the 403 page.

    The ``"local"`` sentinel is not an identity either, and the page carries
    the gate headers, one line and a link home.
    """
    _set_sharing(app, external=False)
    minted = await _mint(client, view="raw")

    for identity in (None, "local"):
        app.state.test_auth_provider.user_id = identity
        resp = await client.get(minted["url"])
        assert resp.status_code == 403, identity
        assert resp.headers["content-security-policy"] == _GATE_CSP
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["x-robots-tag"] == "noindex"
        assert '<a href="/">Home</a>' in resp.text
        assert '"error"' not in resp.text


async def test_gate_switch_off_redirects_an_authorized_login_onto_a_grant(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """An authorized logged-in user is 303d onto a grant that serves."""
    _set_sharing(app, external=False)
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")

    async with _use_runner(runner):
        redirect = await client.get(minted["url"])
        assert redirect.status_code == 303
        location = redirect.headers["location"]
        assert location.startswith("/v1/artifacts/")
        assert "://" not in location
        assert "~" in location
        served = await client.get(location)

    assert served.status_code == 200
    assert served.content == b"plain"


async def test_gate_grant_redirect_keeps_base_path_and_query(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The Location is path-only under the base path and keeps the query."""
    _set_sharing(app, external=False)
    app.state.base_path = "/proxy/6767"
    minted = await _mint(client, view="raw")

    resp = await client.get(f"{minted['url']}?v=1")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/proxy/6767/v1/artifacts/")
    assert location.endswith("?v=1")
    assert "://" not in location


async def test_gate_unauthorized_login_meets_the_form(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A logged-in viewer without workspace sharing is no login at the gate."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    _set_viewer(app)
    _conv(app).share_workspace_files = False

    resp = await client.get(minted["url"])

    assert resp.status_code == 401
    assert '<form method="post">' in resp.text


# ── Gate: share-code flow ───────────────────────────────────────────


async def test_gate_share_code_form_unlock_and_revisit(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """The full flow: form, wrong code, right code, cookie, grant, bytes."""
    _set_sharing(app, share_code="open-sesame")
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None
    cookie_name = remember_cookie_name(_OWNER, secure=False)

    async with _use_runner(runner):
        form = await client.get(f"{minted['url']}?v=1")
        wrong = await client.post(minted["url"], data={"code": "not-the-code"})
        unlock = await client.post(minted["url"], data={"code": "open-sesame"})
        cookie = unlock.cookies.get(cookie_name)
        revisit = await client.get(minted["url"], cookies={cookie_name: cookie})
        served = await client.get(revisit.headers["location"])

    assert form.status_code == 401
    assert form.headers["content-security-policy"] == _GATE_CSP
    assert '<input type="password" name="code">' in form.text
    assert '<button type="submit">Open</button>' in form.text
    assert "action=" not in form.text
    # No request data is echoed: neither the token, the relpath nor the query.
    assert _token_from_url(minted["url"]) not in form.text
    assert "index.html" not in form.text
    assert "?v=1" not in form.text
    assert wrong.status_code == 401
    assert unlock.status_code == 303
    assert cookie_name in unlock.headers["set-cookie"]
    assert "Path=/v1/artifacts/" in unlock.headers["set-cookie"]
    assert "HttpOnly" in unlock.headers["set-cookie"]
    assert "SameSite=lax" in unlock.headers["set-cookie"]
    assert revisit.status_code == 303
    assert "~" in revisit.headers["location"]
    assert served.status_code == 200
    assert served.content == b"plain"


async def test_gate_unlock_strips_the_submitted_code(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A code typed with surrounding spaces still unlocks.

    The PUT stores the stripped code, so the unlock must compare the same
    form or a visitor's harmless whitespace is refused.
    """
    _set_sharing(app, share_code="abcd")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None

    unlock = await client.post(minted["url"], data={"code": " abcd "})

    assert unlock.status_code == 303
    assert "set-cookie" in unlock.headers


async def test_gate_bare_sub_resource_meets_the_form_not_the_file(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Under a closed gate a sub-resource never reaches the reader."""
    _set_sharing(app, share_code="open-sesame")
    captured: list[dict[str, Any]] = []
    runner = _stub_runner_app(b"body{}", content_type="text/css", captured=captured)
    minted = await _mint(client, view="raw")  # reports/index.html bundle
    app.state.test_auth_provider.user_id = None
    prefix = minted["url"].rsplit("/", 1)[0]

    async with _use_runner(runner):
        resp = await client.get(f"{prefix}/style.css")

    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("text/html")
    assert captured == []


async def test_gate_remember_cookie_is_forgotten_on_a_code_change(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A code change regenerates the gate key and invalidates the cookie."""
    _set_sharing(app, share_code="old-code")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None
    cookie_name = remember_cookie_name(_OWNER, secure=False)
    unlock = await client.post(minted["url"], data={"code": "old-code"})
    cookie = unlock.cookies.get(cookie_name)

    _set_sharing(app, share_code="new-code")
    stale = await client.get(minted["url"], cookies={cookie_name: cookie})
    fresh = await client.post(minted["url"], data={"code": "new-code"})

    assert unlock.status_code == 303
    assert stale.status_code == 401
    assert fresh.status_code == 303


async def test_gate_unlock_refused_while_switched_off(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A POST with the right code issues nothing while external is off."""
    _set_sharing(app, external=False, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None

    resp = await client.post(minted["url"], data={"code": "open-sesame"})

    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers


async def test_gate_unlock_while_open_answers_the_get_decision(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """With no code set, an unlock POST issues nothing and serves the bytes."""
    _set_sharing(app, external=True, share_code="a-code")
    _set_sharing(app, share_code=None)
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None

    async with _use_runner(runner):
        resp = await client.post(minted["url"], data={"code": "whatever"})

    assert resp.status_code == 200
    assert resp.content == b"plain"
    assert "set-cookie" not in resp.headers


async def test_gate_unlock_never_issues_credentials_for_a_panel_token(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """A panel token is decided as GET decides it: serve or 410, never unlock.

    The panel holds an expiry the mint issued to an authorized user, so the
    unlock flow must not mint a grant or a remember cookie for it.
    """
    _set_sharing(app, share_code="open-sesame")
    stored_key = generate_session_key()
    _conv(app).labels[ARTIFACT_LINK_KEY_LABEL] = stored_key
    app.state.test_auth_provider.user_id = None
    runner = _stub_runner_app(b"plain", content_type="text/html")

    def _panel_url(expires_at: int) -> str:
        token = encode_artifact_token(
            session_key_bytes(stored_key),
            session_id=_SESSION_ID,
            workspace_id=0,
            root="reports",
            absolute=False,
            entry="index.html",
            kind="b",
            view="p",
            expires_at=expires_at,
        )
        return f"/v1/artifacts/{token}/index.html"

    async with _use_runner(runner):
        unexpired_url = _panel_url(now_epoch() + 3600)
        unexpired_get = await client.get(unexpired_url)
        unexpired_post = await client.post(unexpired_url, data={"code": "open-sesame"})
    expired_url = _panel_url(now_epoch() - 1)
    expired_get = await client.get(expired_url)
    expired_post = await client.post(expired_url, data={"code": "open-sesame"})

    assert unexpired_get.status_code == 200
    assert unexpired_post.status_code == 200
    assert unexpired_post.content == b"plain"
    assert "set-cookie" not in unexpired_post.headers
    assert expired_get.status_code == 410
    assert expired_post.status_code == 410
    assert "set-cookie" not in expired_post.headers


# ── Gate: grants ────────────────────────────────────────────────────


async def test_gate_invalid_grant_is_evaluated_as_bare(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A forged or expired grant falls back to the bare-token decision."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None
    prefix, entry = minted["url"].rsplit("/", 1)

    forged = await client.get(f"{prefix}~bm90LWEtZ3JhbnQ./{entry}")
    past_expiry = (now_epoch() - 1).to_bytes(4, "big")
    stale_mac = b"\x00" * 16
    expired = await client.get(f"{prefix}~{_b64url(past_expiry)}.{_b64url(stale_mac)}/{entry}")

    assert forged.status_code == 401
    assert expired.status_code == 401


async def test_gate_grant_dies_on_revoke(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """Revoking the session key kills the grant with the token."""
    _set_sharing(app, external=False)
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")

    async with _use_runner(runner):
        grant_url = (await client.get(minted["url"])).headers["location"]
        before = await client.get(grant_url)
    revoke = await client.post(f"/v1/sessions/{_SESSION_ID}/artifacts/revoke")
    after = await client.get(grant_url)

    assert before.status_code == 200
    assert revoke.status_code == 204
    assert after.status_code == 410


async def test_gate_grant_dies_on_a_settings_change(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """Any settings write regenerates the gate key, killing every grant."""
    _set_sharing(app, share_code="open-sesame")
    runner = _stub_runner_app(b"plain", content_type="text/plain")
    minted = await _mint(client, view="raw")

    async with _use_runner(runner):
        grant_url = (await client.get(minted["url"])).headers["location"]
        before = await client.get(grant_url)
    _set_sharing(app, share_code="new-code")
    app.state.test_auth_provider.user_id = None
    stale = await client.get(grant_url)

    assert before.status_code == 200
    assert stale.status_code == 401


async def test_gate_owner_unresolvable_fails_closed_with_empty_gate_id(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """No owner grant: logged out is refused, an authorized login still passes.

    The grant is signed with the empty gate-key id, and it validates because
    that is exactly what an unresolvable owner signs with.
    """
    _conv(app).labels[ARTIFACT_LINK_KEY_LABEL] = generate_session_key()
    app.state.test_permission_store.levels.clear()
    url = _raw_token_url(app)
    runner = _stub_runner_app(b"plain", content_type="text/plain")

    app.state.test_auth_provider.user_id = None
    refused = await client.get(url)

    app.state.test_auth_provider.user_id = _VIEWER
    app.state.test_permission_store.grant(_VIEWER, _SESSION_ID, LEVEL_READ)
    _conv(app).share_workspace_files = True

    async with _use_runner(runner):
        redirect = await client.get(url)
        served = await client.get(redirect.headers["location"])

    assert refused.status_code == 403
    assert redirect.status_code == 303
    assert "~" in redirect.headers["location"]
    assert served.status_code == 200


async def test_gate_without_an_auth_provider_governs_the_local_record(
    runner_globals_reset: None,
    db_uri: str,
    asset_dir: Path,
) -> None:
    """No provider: the owner is the local sentinel and no request is a login."""
    del runner_globals_reset
    store = _ConversationStore({_SESSION_ID: _conversation()}, storage_location=db_uri)
    app = _build_app(store, auth_provider=None, permission_store=None)
    ArtifactSharingStore(db_uri).write_sharing("local", external=False)
    transport = httpx.ASGITransport(app=app)
    runner = _stub_runner_app(b"plain", content_type="text/plain")

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        minted = await _mint(client, view="raw")
        async with _use_runner(runner):
            resp = await client.get(minted["url"])

    assert resp.status_code == 403


# ── Gate: unlock throttle ───────────────────────────────────────────


async def test_gate_unlock_throttle_answers_429_after_ten_failures(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The eleventh wrong code inside the window is a 429 page."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None

    statuses = [
        (await client.post(minted["url"], data={"code": "wrong"})).status_code for _ in range(10)
    ]
    throttled = await client.post(minted["url"], data={"code": "wrong"})

    assert statuses == [401] * 10
    assert throttled.status_code == 429
    assert throttled.headers["content-security-policy"] == _GATE_CSP
    assert "Too many attempts" in throttled.text


async def test_gate_unlock_success_does_not_count_toward_the_budget(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A correct code consumes no budget: nine failures, a success, one more."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None

    for _ in range(9):
        assert (await client.post(minted["url"], data={"code": "wrong"})).status_code == 401
    assert (await client.post(minted["url"], data={"code": "open-sesame"})).status_code == 303
    tenth = await client.post(minted["url"], data={"code": "wrong"})
    throttled = await client.post(minted["url"], data={"code": "wrong"})

    assert tenth.status_code == 401
    assert throttled.status_code == 429


async def test_gate_concurrent_unlocks_reserve_and_release_the_budget(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent attempts reserve slots at admission; a success returns its slot.

    The correct request holds its reservation while blocked in verification,
    so the parallel wrong probes reach the limit instead of all passing the
    spent-budget check; releasing it afterwards shows the success consumed
    no budget.
    """
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="raw")
    app.state.test_auth_provider.user_id = None
    real_verify = artifacts_module.verify_share_code
    in_verify = threading.Event()
    release_verify = threading.Event()

    def gated_verify(code: str, code_hash: str) -> bool:
        if code == "open-sesame":
            in_verify.set()
            release_verify.wait(timeout=5)
            return real_verify(code, code_hash)
        time.sleep(0.05)
        return real_verify(code, code_hash)

    monkeypatch.setattr(artifacts_module, "verify_share_code", gated_verify)
    winner = asyncio.create_task(client.post(minted["url"], data={"code": "open-sesame"}))
    assert await asyncio.to_thread(in_verify.wait, 5)
    wrong = await asyncio.gather(
        *(client.post(minted["url"], data={"code": f"wrong-{index}"}) for index in range(11))
    )
    release_verify.set()
    unlock = await winner

    statuses = [response.status_code for response in wrong]
    assert statuses.count(401) == 9
    assert statuses.count(429) == 2
    assert unlock.status_code == 303
    assert "set-cookie" in unlock.headers

    # The success released its slot: nine failures remain, so one more wrong
    # attempt is admitted and only the one after it is refused.
    tenth = await client.post(minted["url"], data={"code": "wrong"})
    throttled = await client.post(minted["url"], data={"code": "wrong"})

    assert tenth.status_code == 401
    assert throttled.status_code == 429


# ── Gate: cookie is not a login ─────────────────────────────────────


# ── Visitor comments POST ───────────────────────────────────────────


async def _post_visitor_comment(
    client: httpx.AsyncClient,
    token: str,
    *,
    path: str = "index.html",
    body: str = "Fix this",
    grant: str | None = None,
    name: str | None = "Alice",
    anchor_content: str | None = None,
    start_index: int = 0,
    end_index: int = 0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST one visitor comment through the real route."""
    payload: dict[str, Any] = {
        "token": token,
        "path": path,
        "body": body,
        "start_index": start_index,
        "end_index": end_index,
    }
    if grant is not None:
        payload["grant"] = grant
    if name is not None:
        payload["name"] = name
    if anchor_content is not None:
        payload["anchor_content"] = anchor_content
    return await client.post("/v1/artifact-comments", json=payload, headers=headers)


def _grant_from_url(url: str) -> str:
    """Extract the grant suffix from a ``<token>~<grant>`` artifact URL."""
    _bare, separator, grant = url.split("/")[3].partition("~")
    assert separator and grant
    return grant


async def test_visitor_comment_open_gate_stores_a_visitor_row(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """An open gate admits a grant-less visitor comment on the joined path."""
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        body="Please rewrite this",
        anchor_content="old text",
        start_index=3,
        end_index=11,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"ok": True}
    rows = _comments(app)
    assert len(rows) == 1
    row = rows[0]
    assert row.conversation_id == _SESSION_ID
    assert row.path == "reports/index.html"
    assert row.body == "Please rewrite this"
    assert row.anchor_content == "old text"
    assert row.start_index == 3
    assert row.end_index == 11
    assert row.status == "draft"
    assert row.created_by == "visitor:Alice"


async def test_visitor_comment_nested_root_path_is_joined(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The stored path is the bundle root joined with the requested page."""
    minted = await _mint(client, path="reports/page2.html", view="visit")

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        path="page2.html",
    )

    assert resp.status_code == 201, resp.text
    assert _comments(app)[0].path == "reports/page2.html"


async def test_visitor_comment_absolute_root_path_keeps_the_leading_slash(
    client: httpx.AsyncClient,
    app: FastAPI,
    unconfined_browse: None,
) -> None:
    """An absolute link stores the same host path its serve route would."""
    minted = await _mint(client, path="/opt/reports/index.html", base="host", view="visit")

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert resp.status_code == 201, resp.text
    assert _comments(app)[0].path == "/opt/reports/index.html"


async def test_visitor_comment_gated_accepts_a_valid_grant(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A share-code link admits a comment carrying its unlock grant."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None
    unlock = await client.post(minted["url"], data={"code": "open-sesame"})

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        grant=_grant_from_url(unlock.headers["location"]),
    )

    assert unlock.status_code == 303
    assert resp.status_code == 201, resp.text
    assert len(_comments(app)) == 1


async def test_visitor_comment_gated_without_a_grant_reloads(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A share-code link refuses a grant-less comment with 403 reload."""
    _set_sharing(app, share_code="open-sesame")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert resp.status_code == 403
    assert resp.json() == {"reason": "reload"}
    assert _comments(app) == []


async def test_visitor_comment_stale_grant_after_a_code_change_reloads(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A new share code rotates the gate key and voids the old grant."""
    _set_sharing(app, share_code="old-code")
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None
    unlock = await client.post(minted["url"], data={"code": "old-code"})
    grant = _grant_from_url(unlock.headers["location"])

    _set_sharing(app, share_code="new-code")
    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]), grant=grant)

    assert resp.status_code == 403
    assert resp.json() == {"reason": "reload"}
    assert _comments(app) == []


async def test_visitor_comment_external_off_reloads(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A switched-off gate refuses the comment and points at a reload."""
    _set_sharing(app, external=False)
    minted = await _mint(client, view="visit")
    app.state.test_auth_provider.user_id = None

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert resp.status_code == 403
    assert resp.json() == {"reason": "reload"}
    assert _comments(app) == []


async def test_visitor_comment_allow_comments_off_is_disabled(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The owner's comments switch refuses with the disabled reason."""
    _set_sharing(app, allow_comments=False)
    minted = await _mint(client, view="visit")

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert resp.status_code == 403
    assert resp.json() == {"reason": "disabled"}
    assert _comments(app) == []


async def test_visitor_comment_archived_session_is_gone(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """An archived session answers 410 and stores nothing."""
    minted = await _mint(client, view="visit")
    _conv(app).archived = True

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert resp.status_code == 410
    assert _comments(app) == []


async def test_visitor_comment_revoked_link_is_gone(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Revoking the session key kills the token with every other link."""
    minted = await _mint(client, view="visit")
    revoke = await client.post(f"/v1/sessions/{_SESSION_ID}/artifacts/revoke")

    resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert revoke.status_code == 204
    assert resp.status_code == 410
    assert _comments(app) == []


async def test_visitor_comment_cross_origin_is_refused(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an allowlist configured, an untrusted Origin is a 403."""
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "https://app.example.com")
    minted = await _mint(client, view="visit")

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    assert _comments(app) == []


async def test_visitor_comment_requires_json_content_type(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The shared JSON guard refuses a non-JSON body before it is parsed."""
    minted = await _mint(client, view="visit")

    resp = await client.post(
        "/v1/artifact-comments",
        content=json.dumps({"token": _token_from_url(minted["url"])}),
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 415
    assert _comments(app) == []


async def test_visitor_comment_window_is_per_link(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """The 21st comment on one link is 429 while another link still posts."""
    first = await _mint(client, view="visit")
    second = await _mint(client, path="reports/page2.html", view="visit")
    first_token = _token_from_url(first["url"])
    second_token = _token_from_url(second["url"])

    for _ in range(20):
        assert (await _post_visitor_comment(client, first_token)).status_code == 201

    throttled = await _post_visitor_comment(client, first_token)
    other = await _post_visitor_comment(client, second_token, path="page2.html")

    assert throttled.status_code == 429
    assert throttled.headers["retry-after"] == "600"
    assert other.status_code == 201
    assert len(_comments(app)) == 21


async def test_visitor_comment_oversized_body_is_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A body past the cap is refused whole, never truncated into a row."""
    minted = await _mint(client, view="visit")

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        body="x" * 4001,
    )

    assert resp.status_code in (413, 422)
    assert _comments(app) == []


async def test_visitor_comment_path_outside_the_bundle_is_not_found(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """A traversal path never reaches the comment store."""
    minted = await _mint(client, view="visit")

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        path="../secret.html",
    )

    assert resp.status_code == 404
    assert _comments(app) == []


async def test_visitor_comment_non_html_path_is_not_found(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Comments belong to HTML pages; a bundle's other bytes stay unserved."""
    minted = await _mint(client, view="visit")

    resp = await _post_visitor_comment(
        client,
        _token_from_url(minted["url"]),
        path="notes.txt",
    )

    assert resp.status_code == 404
    assert _comments(app) == []


async def test_visitor_comment_frame_token_is_not_found(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Only the shell's ``g`` token is a comment authority, never ``h``."""
    await _mint(client)
    h_token = _raw_token_url(app, view="h").split("/")[3]

    resp = await _post_visitor_comment(client, h_token)

    assert resp.status_code == 404
    assert _comments(app) == []


async def test_visit_shell_without_a_comment_store_disables_comments(
    runner_globals_reset: None,
    db_uri: str,
    asset_dir: Path,
) -> None:
    """A server without a comment store serves the shell but no comment route."""
    del runner_globals_reset
    _write_visit_shell(asset_dir)
    store = _ConversationStore({_SESSION_ID: _conversation()}, storage_location=db_uri)
    permission_store = _PermissionStore()
    permission_store.grant(_OWNER, _SESSION_ID, LEVEL_OWNER)
    app = _build_app(
        store,
        auth_provider=_FixedAuthProvider(),
        permission_store=permission_store,
        comment_store=None,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        minted = await _mint(client, view="visit")
        shell = await client.get(minted["url"])
        resp = await _post_visitor_comment(client, _token_from_url(minted["url"]))

    assert shell.status_code == 200
    assert _visit_config(shell.text)["commentsEnabled"] is False
    assert resp.status_code == 404


async def test_visit_shell_allow_comments_off_disables_the_affordance(
    client: httpx.AsyncClient,
    app: FastAPI,
    asset_dir: Path,
) -> None:
    """The owner's switch turns the shell's comment affordance off."""
    _write_visit_shell(asset_dir)
    _set_sharing(app, allow_comments=False)
    minted = await _mint(client, view="visit")

    resp = await client.get(minted["url"])

    assert resp.status_code == 200
    assert _visit_config(resp.text)["commentsEnabled"] is False


async def test_gate_remember_cookie_is_not_an_api_identity(db_uri: str) -> None:
    """The cookie-source provider never reads the remember cookie as a login.

    An accounts-mode provider resolves identity through ``_check_cookie``, so
    this pins the boundary on the real path: a real unlock issues the cookie,
    and presenting only that cookie to a user-required API route is a 401.
    """
    store = _ConversationStore({_SESSION_ID: _conversation()}, storage_location=db_uri)
    permission_store = _PermissionStore()
    permission_store.grant(_OWNER, _SESSION_ID, LEVEL_OWNER)
    app = _build_app(
        store,
        auth_provider=UnifiedAuthProvider(
            source="accounts",
            accounts_config=AccountsConfig(
                cookie_secret=b"\x51" * 32,
                session_ttl_hours=8,
                base_url="http://server",
                init_admin_password=None,
                invite_ttl_seconds=3600,
                magic_ttl_seconds=600,
            ),
        ),
        permission_store=permission_store,
    )
    ArtifactSharingStore(db_uri).write_sharing(_OWNER, share_code="open-sesame")
    _conv(app).labels[ARTIFACT_LINK_KEY_LABEL] = generate_session_key()
    url = _raw_token_url(app)
    cookie_name = remember_cookie_name(_OWNER, secure=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        unlock = await client.post(url, data={"code": "open-sesame"})
        cookie = unlock.cookies.get(cookie_name)
        assert cookie is not None
        resp = await client.get("/v1/artifact-sharing", cookies={cookie_name: cookie})

    assert unlock.status_code == 303
    assert resp.status_code == 401
