"""Artifact capability-URL routes: mint, revoke, open and serve.

The web UI mints a signed, session-bound token for one workspace file
(``POST /v1/sessions/{sid}/artifacts``) and loads the file from
``GET /v1/artifacts/<token>/<relpath>``. The URL is the credential: the
serve route reads no cookie, verifies the token against the session's
artifact-link key label, and pulls the bytes from the session's runner
(streamed) or, when that runner is offline, from the connected host over
its filesystem tunnel. A bundle token (an HTML entry) serves the entry's
folder and descendants; the reader enforces containment with ``within``
and the server refuses a response that does not confirm it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http
import logging
import mimetypes
import os
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from omnigent.artifact_paths import artifact_path_allowed, artifact_segments_valid
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_FS_READ_RAW
from omnigent.server.artifact_links import (
    ArtifactTokenClaims,
    bridge_nonce,
    decode_artifact_token,
    encode_artifact_token,
    get_or_create_artifact_key,
    read_artifact_key,
    rotate_artifact_key,
    verify_artifact_token,
)
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._gzip_route import skip_gzip
from omnigent.server.routes.sessions.routes_resources import _RunnerStreamResponse
from omnigent.server.schemas import ArtifactOpenRequestEvent
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)

# The web UI's environment resource id (see the file-panel routes).
_ENVIRONMENT_ID = "default"

# Per-file ceiling for a served artifact. The runner announces the size in
# Content-Length; a body without one is read into a buffer no larger than this.
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

# In-frame scripts injected into panel-view HTML, in order. RPB03 appends
# its annotation asset here; an asset missing from disk is skipped.
_IN_FRAME_ASSETS: tuple[str, ...] = ("omni-html-bridge.js",)

# Served web-ui directory, resolved the same way as ``omnigent.server.app``
# does (that module imports this one transitively, so the path is rebuilt
# here rather than imported). Env vars are read once at import.
_WEB_UI_DIR = Path(
    os.environ.get("OMNIGENT_WEB_UI_DIST")
    or (Path(__file__).resolve().parent.parent / "static" / "web-ui")
)
# Source-checkout fallback for a server running from the repo without a web
# build. Read only when it exists; nothing is copied or checked at startup.
_SOURCE_ASSET_DIR = Path(__file__).resolve().parents[3] / "web" / "public"

_CSP_SANDBOX = (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox allow-modals allow-top-navigation allow-downloads"
)

# Headers on every artifact response, success or error page. The CSP forces an
# opaque origin in-frame and top-level; ACAO without credentials lets a
# bundle's module scripts and fetches run from that origin.
_SECURITY_HEADERS: dict[str, str] = {
    "Content-Disposition": "inline",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _CSP_SANDBOX,
    "Access-Control-Allow-Origin": "*",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
}

_ERROR_PAGE_TEMPLATE = (
    '<!doctype html>\n<html><head><meta charset="utf-8">'
    "<title>{status} {reason}</title></head>"
    "<body><h1>{status} {reason}</h1><p>{sentence}</p></body></html>\n"
)

_NOT_FOUND_SENTENCE = "Not found."
_REVOKED_SENTENCE = "This link is no longer available."
_TOO_LARGE_SENTENCE = "This file is too large to serve as an artifact."
_HOST_OFFLINE_SENTENCE = "The session host is offline."
_HOST_NEEDS_UPDATE_SENTENCE = "The session host needs an update to serve bundle files."
_RUNNER_NEEDS_UPDATE_SENTENCE = "The session runner needs an update to serve bundle files."
_LOAD_FAILED_SENTENCE = "The artifact could not be loaded."

_PAGE_STATUSES = frozenset({404, 410, 413, 502, 503})

_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r"</html\s*>", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script", re.IGNORECASE)


def _posix_join(root: str, rel: str) -> str:
    """Join a bundle root and a child path with ``/`` separators.

    :param root: Bundle root, ``""`` for the workspace root.
    :param rel: Path relative to *root*.
    :returns: The joined path.
    """
    if not root:
        return rel
    if not rel:
        return root
    return f"{root.rstrip('/')}/{rel}"


def _guess_media_type(relpath: str) -> str:
    """Return the guessed content type for *relpath*, with a binary default."""
    return mimetypes.guess_type(relpath)[0] or "application/octet-stream"


def _is_html(content_type: str) -> bool:
    """Whether *content_type* names HTML, ignoring its parameters."""
    return content_type.split(";", 1)[0].strip().lower() == "text/html"


def _sentence_for(status: int) -> str:
    """The one plain sentence an error page of *status* carries."""
    if status == 404:
        return _NOT_FOUND_SENTENCE
    if status == 410:
        return _REVOKED_SENTENCE
    if status == 413:
        return _TOO_LARGE_SENTENCE
    if status == 503:
        return _HOST_OFFLINE_SENTENCE
    return _LOAD_FAILED_SENTENCE


def _page_status(status: int) -> int:
    """Clamp an error status to one the artifact route serves as a page."""
    return status if status in _PAGE_STATUSES else 502


def _error_page(status: int, sentence: str) -> HTMLResponse:
    """Build a tiny static HTML error page with the artifact security headers.

    The body echoes no request data — not the token, path or query.

    :param status: HTTP status code, e.g. ``410``.
    :param sentence: One plain sentence explaining the outcome.
    :returns: The HTML response.
    """
    reason = http.HTTPStatus(status).phrase
    return HTMLResponse(
        content=_ERROR_PAGE_TEMPLATE.format(status=status, reason=reason, sentence=sentence),
        status_code=status,
        headers=dict(_SECURITY_HEADERS),
    )


def _normalize_artifact_path(path: str, base: object) -> tuple[bool, str]:
    """Resolve a mint/open body path against its declared base.

    The same rule as ``_resolve_browse_path``: ``base == "host"`` or a
    leading ``/`` marks the target absolute, and the absolute form is
    ``"/" + path.lstrip("/")``. Rejects empty, ``.``, ``..``, backslash and
    NUL components before any authorization or filesystem work.

    :param path: Raw ``path`` from the request body.
    :param base: Raw ``base`` from the request body.
    :returns: ``(absolute, normalized)`` — *normalized* carries a leading
        slash iff *absolute*.
    :raises OmnigentError: 400 when the path has an invalid component.
    """
    absolute = base == "host" or path.startswith("/")
    normalized = "/" + path.lstrip("/") if absolute else path
    if not artifact_segments_valid(normalized.lstrip("/").split("/")):
        raise OmnigentError(
            "Artifact path must be a plain file path without '..' or empty components",
            code=ErrorCode.INVALID_INPUT,
        )
    return absolute, normalized


def _content_length(value: str | None) -> int | None:
    """Parse a Content-Length header, or ``None`` when absent or malformed."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _read_stream_bounded(upstream: httpx.Response) -> bytes | None:
    """Read a runner body up to the artifact cap.

    :param upstream: The runner's streamed response.
    :returns: The decoded body, or ``None`` when it exceeds
        :data:`_MAX_ARTIFACT_BYTES`.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in upstream.aiter_bytes():
        total += len(chunk)
        if total > _MAX_ARTIFACT_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_host_content(payload: Mapping[str, Any]) -> bytes | None:
    """Decode a host read payload's ``content`` by its declared ``encoding``.

    :param payload: The host's file-content payload.
    :returns: The file bytes, or ``None`` for an unknown encoding or a
        malformed base64 body.
    """
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    encoding = payload.get("encoding")
    if encoding == "utf-8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content)
        except (binascii.Error, ValueError):
            return None
    return None


def _read_in_frame_asset(name: str) -> str | None:
    """Read one in-frame asset from the served web UI or the source checkout.

    :param name: Asset file name, e.g. ``"omni-html-bridge.js"``.
    :returns: The asset text, or ``None`` when it is absent from both
        locations or is not valid UTF-8.
    """
    for base in (_WEB_UI_DIR, _SOURCE_ASSET_DIR):
        try:
            return (base / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _last_match_start(pattern: re.Pattern[str], text: str) -> int:
    """Return the start index of the last match of *pattern* in *text*, or -1."""
    start = -1
    for match in pattern.finditer(text):
        start = match.start()
    return start


def _inject_in_frame_assets(body: bytes, nonce: str) -> bytes | None:
    """Inline the panel assets as ``<script data-omni-nonce=…>`` blocks.

    Placement mirrors the viewer's own regex approach: before the last
    ``</body>``, else the last ``</html>``, else appended.

    :param body: The raw HTML bytes.
    :param nonce: The bridge nonce for the served token.
    :returns: The injected bytes, or ``None`` when the body is not UTF-8 or
        no asset could be read (the caller then serves the body unmodified).
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    scripts: list[str] = []
    for name in _IN_FRAME_ASSETS:
        asset = _read_in_frame_asset(name)
        if asset is None:
            continue
        # An embedded "</script" would close the tag early; the JS-safe
        # escape keeps the asset inert inside a string while the HTML parser
        # still sees no closing tag.
        escaped = _SCRIPT_CLOSE_RE.sub(lambda _match: r"<\/script", asset)
        scripts.append(f'<script data-omni-nonce="{nonce}">{escaped}</script>')
    if not scripts:
        return None
    index = _last_match_start(_BODY_CLOSE_RE, text)
    if index == -1:
        index = _last_match_start(_HTML_CLOSE_RE, text)
    if index == -1:
        index = len(text)
    return (text[:index] + "".join(scripts) + text[index:]).encode("utf-8")


def register_artifact_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    host_registry: HostRegistry | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    _authorize_browse_read: Callable[[str, Request | None, str], Awaitable[Conversation]],
    _authorize_absolute_browse: Callable[[Conversation, str], Awaitable[str]],
    _stream_download_from_runner: Callable[[Request, str, Conversation, str], Awaitable[Response]],
    _read_workspace_via_host: Callable[..., Awaitable[dict[str, Any] | None]],
    _runner_path_segment: Callable[..., str],
) -> None:
    """Register the artifact capability-URL routes on *router*.

    Called from ``register_resources_routes`` so the mint and open paths
    reuse that function's authorization closures. The serve and preflight
    routes live here too, on the same router, so they are mounted under
    ``/v1`` before the SPA fallback.

    :param router: The sessions router to register on.
    :param conversation_store: Store holding conversations and labels.
    :param host_registry: Live host tunnels, for the runner-offline fallback.
    :param auth_provider: Auth provider for user identity extraction.
    :param permission_store: Permission store for session-level access.
    :param _authorize_browse_read: Closure authorizing a content read.
    :param _authorize_absolute_browse: Closure authorizing an absolute target.
    :param _stream_download_from_runner: Closure streaming a runner download.
    :param _read_workspace_via_host: Closure reading a workspace over the host.
    :param _runner_path_segment: Closure encoding a runner path segment.
    """

    async def _authorize_edit(request: Request, session_id: str) -> None:
        """Require edit level on *session_id*, like the browser bridge routes."""
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )

    def _artifact_runner_url(
        session_id: str,
        claims: ArtifactTokenClaims,
        relpath: str,
        *,
        sub_resource: bool,
    ) -> str:
        """Build the runner download URL for one artifact target.

        :param session_id: Session/conversation identifier.
        :param claims: Verified token claims.
        :param relpath: Requested path relative to the bundle root.
        :param sub_resource: Whether ``within`` containment applies.
        :returns: The runner-relative URL with ``download=true`` and, for a
            bundle sub-resource, ``within``.
        """
        if claims.absolute:
            full = "/" + _posix_join(claims.root.lstrip("/"), relpath)
            runner_rel = _runner_path_segment(full, absolute=True)
        else:
            runner_rel = _runner_path_segment(_posix_join(claims.root, relpath), absolute=False)
        params = {"download": "true"}
        if sub_resource:
            # ``""`` names the workspace root and must arrive as an empty
            # string, not be dropped.
            params["within"] = claims.root
        return (
            f"/v1/sessions/{session_id}/resources/environments/{_ENVIRONMENT_ID}"
            f"/filesystem/{runner_rel}?{urllib.parse.urlencode(params)}"
        )

    def _render_buffered(
        request: Request,
        claims: ArtifactTokenClaims,
        nonce: str,
        content_type: str,
        body: bytes,
    ) -> Response:
        """Build an in-memory artifact response, injecting panel assets if due."""
        if claims.view == "p" and _is_html(content_type):
            injected = _inject_in_frame_assets(body, nonce)
            if injected is not None:
                body = injected
        skip_gzip(request)
        return Response(content=body, media_type=content_type, headers=dict(_SECURITY_HEADERS))

    def _apply_stream_headers(request: Request, response: Response, content_type: str) -> None:
        """Rewrite a streamed runner response's headers for artifact serving."""
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        if not response.headers.get("content-type"):
            response.headers["content-type"] = content_type
        skip_gzip(request)

    async def _serve_from_runner(
        request: Request,
        session_id: str,
        conv: Conversation,
        claims: ArtifactTokenClaims,
        relpath: str,
        *,
        sub_resource: bool,
        nonce: str,
    ) -> Response:
        """Serve one artifact from the session's runner.

        :raises OmnigentError: ``RUNNER_UNAVAILABLE`` when no live runner can
            serve the target (the caller falls back to the host).
        """
        runner_path = _artifact_runner_url(session_id, claims, relpath, sub_resource=sub_resource)
        runner_response = await _stream_download_from_runner(
            request, session_id, conv, runner_path
        )
        if not isinstance(runner_response, _RunnerStreamResponse):
            # The download proxy forwards the runner's own error body. A
            # missing file or directory target is a 404; every other runner
            # failure (or a stale session binding) is a gateway fault.
            if runner_response.status_code in (400, 404):
                return _error_page(404, _NOT_FOUND_SENTENCE)
            return _error_page(502, _LOAD_FAILED_SENTENCE)
        upstream = runner_response.upstream
        if sub_resource and upstream.headers.get("x-omnigent-within") != "enforced":
            # An older runner ignores ``within`` and would serve unconfined.
            await upstream.aclose()
            return _error_page(502, _RUNNER_NEEDS_UPDATE_SENTENCE)
        content_type = upstream.headers.get("content-type") or _guess_media_type(relpath)
        content_length = _content_length(upstream.headers.get("content-length"))
        if content_length is not None and content_length > _MAX_ARTIFACT_BYTES:
            await upstream.aclose()
            return _error_page(413, _TOO_LARGE_SENTENCE)
        if content_length is None or (claims.view == "p" and _is_html(content_type)):
            try:
                body = await _read_stream_bounded(upstream)
            except httpx.HTTPError:
                # A runner body that dies mid-read cannot become a streamed
                # response, so answer the page the route promises.
                logger.warning("artifact runner read failed for session %s", session_id)
                return _error_page(502, _LOAD_FAILED_SENTENCE)
            finally:
                # The caller took over the stream, so it owns closing it —
                # including when the read itself fails.
                await upstream.aclose()
            if body is None:
                return _error_page(413, _TOO_LARGE_SENTENCE)
            return _render_buffered(request, claims, nonce, content_type, body)
        _apply_stream_headers(request, runner_response, content_type)
        return runner_response

    async def _serve_from_host(
        request: Request,
        session_id: str,
        conv: Conversation,
        claims: ArtifactTokenClaims,
        relpath: str,
        *,
        sub_resource: bool,
        nonce: str,
    ) -> Response:
        """Serve one artifact over the session's host tunnel (runner offline)."""
        if host_registry is None or not conv.host_id:
            return _error_page(503, _HOST_OFFLINE_SENTENCE)
        host_conn = host_registry.get(conv.host_id)
        if host_conn is None:
            return _error_page(503, _HOST_OFFLINE_SENTENCE)
        if CAP_FS_READ_RAW not in host_conn.hello.capabilities:
            return _error_page(502, _HOST_NEEDS_UPDATE_SENTENCE)
        workspace_override: str | None = None
        if claims.absolute:
            # The host reads whatever root it is handed; authorize the root
            # here, exactly as the live absolute browse would.
            try:
                workspace_override = await _authorize_absolute_browse(conv, claims.root)
            except HTTPException as exc:
                if exc.status_code >= 500:
                    return _error_page(502, _LOAD_FAILED_SENTENCE)
                return _error_page(404, _NOT_FOUND_SENTENCE)
        params: dict[str, Any] = {
            "path": relpath if claims.absolute else _posix_join(claims.root, relpath),
            "raw": True,
        }
        if sub_resource:
            params["within"] = "" if claims.absolute else claims.root
        try:
            payload = await _read_workspace_via_host(
                session_id,
                conv,
                "list_or_read",
                params,
                workspace_override=workspace_override,
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return _error_page(404, _NOT_FOUND_SENTENCE)
            return _error_page(502, _LOAD_FAILED_SENTENCE)
        except HTTPException as exc:
            if exc.status_code in (400, 404):
                return _error_page(404, _NOT_FOUND_SENTENCE)
            return _error_page(502, _LOAD_FAILED_SENTENCE)
        if payload is None:
            return _error_page(503, _HOST_OFFLINE_SENTENCE)
        if payload.get("object") == "list" or isinstance(payload.get("data"), list):
            # No directory listings through an artifact link.
            return _error_page(404, _NOT_FOUND_SENTENCE)
        if sub_resource and payload.get("within_enforced") is not True:
            # An older host ignores ``within`` and would serve unconfined.
            return _error_page(502, _HOST_NEEDS_UPDATE_SENTENCE)
        if payload.get("truncated"):
            return _error_page(413, _TOO_LARGE_SENTENCE)
        body = _decode_host_content(payload)
        if body is None:
            return _error_page(502, _LOAD_FAILED_SENTENCE)
        content_type = payload.get("content_type") or _guess_media_type(relpath)
        return _render_buffered(request, claims, nonce, content_type, body)

    @router.post(
        "/sessions/{session_id}/artifacts",
        # Internal web-UI flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def mint_artifact_link(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Mint a capability URL for one workspace file.

        Normalizes the target first (``base=host`` or a leading slash marks
        it absolute), then authorizes the final target — an absolute path
        always takes the owner gate — before minting the signed token. An
        HTML entry becomes a bundle token (its folder and descendants); any
        other file becomes a file token (that entry only).

        :param request: The incoming request, for authorization.
        :param session_id: Session/conversation identifier.
        :param body: ``{"path": <str>, "base"?: "host", "view": <"panel"|"raw">}``.
        :returns: ``{"url": <str>, "nonce": <str>, "kind": <"bundle"|"file">}``.
        :raises OmnigentError: 400 invalid path, 403/404 on auth, 409 archived.
        """
        raw_path = body.get("path")
        if not isinstance(raw_path, str):
            raise OmnigentError(
                "artifacts requires a 'path' string",
                code=ErrorCode.INVALID_INPUT,
            )
        view = body.get("view", "panel")
        if view not in ("panel", "raw"):
            raise OmnigentError(
                "artifacts 'view' must be 'panel' or 'raw'",
                code=ErrorCode.INVALID_INPUT,
            )
        absolute, target = _normalize_artifact_path(raw_path, body.get("base"))
        conv = await _authorize_browse_read(session_id, request, target)
        if conv.archived:
            raise OmnigentError(
                "Session is archived. Unarchive it before opening files in the panel.",
                code=ErrorCode.CONFLICT,
            )
        if absolute:
            target = await _authorize_absolute_browse(conv, target)
        segments = target.lstrip("/").split("/")
        entry = segments[-1]
        parent = "/".join(segments[:-1])
        # An absolute target's root stays absolute, "/" for a file directly
        # under the filesystem root. Both readers resolve this root as a host
        # path; stripping the slash would aim them somewhere else.
        root = f"/{parent}" if absolute else parent
        kind: Literal["b", "f"] = "b" if Path(entry).suffix.lower() in (".html", ".htm") else "f"
        key = await asyncio.to_thread(get_or_create_artifact_key, conversation_store, session_id)
        token = encode_artifact_token(
            key,
            session_id=session_id,
            workspace_id=current_workspace_id(),
            root=root,
            absolute=absolute,
            entry=entry,
            kind=kind,
            view="p" if view == "panel" else "r",
        )
        return {
            "url": f"/v1/artifacts/{token}/{urllib.parse.quote(entry)}",
            "nonce": bridge_nonce(key, token),
            "kind": "bundle" if kind == "b" else "file",
        }

    @router.post(
        "/sessions/{session_id}/artifacts/revoke",
        # Internal web-UI flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=204,
        response_model=None,
    )
    async def revoke_artifact_links(request: Request, session_id: str) -> Response:
        """
        Revoke every open artifact link for a session.

        Rotates the session's artifact-link key in the conversation store;
        every previously minted URL then verifies as revoked (410).

        :param request: The incoming request, for authorization.
        :param session_id: Session/conversation identifier.
        :returns: 204 with no body.
        :raises OmnigentError: 401/403/404 on auth failure.
        """
        await _authorize_edit(request, session_id)
        await asyncio.to_thread(rotate_artifact_key, conversation_store, session_id)
        return Response(status_code=204)

    @router.post(
        "/sessions/{session_id}/artifacts/open",
        # Internal web-UI flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def open_artifact_in_panel(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Ask every subscribed web client to open a file in the preview panel.

        Publishes ``artifact.open_request`` on the session stream. The event
        carries only the normalized path and its base — never a URL — so each
        client mints its own artifact link with its own authorization.

        :param request: The incoming request, for authorization.
        :param session_id: Session/conversation identifier.
        :param body: ``{"path": <str>, "base"?: "host"}``.
        :returns: ``{"viewers": <int>}`` — subscriber slots the event reached.
        :raises OmnigentError: 400 invalid path, 401/403/404 on auth failure.
        """
        await _authorize_edit(request, session_id)
        raw_path = body.get("path")
        if not isinstance(raw_path, str):
            raise OmnigentError(
                "artifacts/open requires a 'path' string",
                code=ErrorCode.INVALID_INPUT,
            )
        absolute, target = _normalize_artifact_path(raw_path, body.get("base"))
        event = ArtifactOpenRequestEvent(
            type="artifact.open_request",
            path=target,
            base="host" if absolute else "workspace",
        )
        from omnigent.server.routes import sessions as _sessions_facade

        viewers = _sessions_facade.session_stream.publish(session_id, event.model_dump())
        return {"viewers": viewers}

    @router.options(
        "/artifacts/{token}/{relpath:path}",
        # Internal serve route — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def preflight_artifact(token: str, relpath: str, request: Request) -> Response:
        """
        Answer a CORS preflight for the artifact serve route.

        The serve route is credential-free (the token is the credential), so
        the preflight needs no auth. The requested headers are echoed so a
        bundle's own fetch headers survive the opaque-origin check.

        :param token: The artifact token (unused).
        :param relpath: The bundle-relative path (unused).
        :param request: The incoming preflight request.
        :returns: 204 with the CORS headers.
        """
        del token, relpath
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": (
                    request.headers.get("access-control-request-headers") or "*"
                ),
                "Access-Control-Max-Age": "600",
            },
        )

    @router.get(
        "/artifacts/{token}/{relpath:path}",
        # Internal serve route — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def serve_artifact(request: Request, token: str, relpath: str) -> Response:
        """
        Serve one artifact file (or bundle sub-resource) by capability URL.

        The token is the only credential: no cookie or user auth is read.
        The runner is the primary byte source; a runner that is offline
        falls back to the host tunnel, and either reader must confirm
        bundle containment (``X-Omnigent-Within`` / ``within_enforced``)
        before the bytes are served. Every failure is a static HTML page,
        never JSON and never the SPA.

        :param request: The incoming request, for the gzip opt-out.
        :param token: The signed artifact token.
        :param relpath: Path relative to the bundle root (URL-decoded).
        :returns: The file bytes with the artifact security headers, or an
            HTML error page (404 / 410 / 413 / 502 / 503).
        """
        claims = decode_artifact_token(token)
        if claims is None:
            return _error_page(404, _NOT_FOUND_SENTENCE)
        # The route reads no identity: stores scope every row by
        # ``current_workspace_id()``, so the token's minting workspace must be
        # rebound or a link minted in workspace N finds no conversation.
        with workspace_scope(claims.workspace_id):
            conv = await asyncio.to_thread(conversation_store.get_conversation, claims.session_id)
            if conv is None:
                return _error_page(404, _NOT_FOUND_SENTENCE)
            if conv.archived:
                return _error_page(410, _REVOKED_SENTENCE)
            key = read_artifact_key(conv.labels)
            verdict = verify_artifact_token(claims, token, key)
            if verdict == "revoked":
                return _error_page(410, _REVOKED_SENTENCE)
            if verdict != "ok" or key is None:
                return _error_page(404, _NOT_FOUND_SENTENCE)
            segments = relpath.split("/")
            if not artifact_segments_valid(segments):
                return _error_page(404, _NOT_FOUND_SENTENCE)
            is_entry = relpath == claims.entry
            if claims.kind == "f":
                # A file token serves exactly its entry.
                if not is_entry:
                    return _error_page(404, _NOT_FOUND_SENTENCE)
            elif not is_entry and not artifact_path_allowed(segments):
                return _error_page(404, _NOT_FOUND_SENTENCE)
            # ``within`` applies to bundle sub-resources only: the entry itself
            # is authorized by the mint, and a directly opened dotfile is a file
            # token (served as-is).
            sub_resource = claims.kind == "b" and not is_entry
            nonce = bridge_nonce(key, token)

            async def _dispatch_reader() -> Response:
                """Serve from the runner, falling back to the host when offline."""
                try:
                    return await _serve_from_runner(
                        request,
                        claims.session_id,
                        conv,
                        claims,
                        relpath,
                        sub_resource=sub_resource,
                        nonce=nonce,
                    )
                except OmnigentError as exc:
                    if exc.code != ErrorCode.RUNNER_UNAVAILABLE:
                        status = _page_status(exc.http_status)
                        return _error_page(status, _sentence_for(status))
                    return await _serve_from_host(
                        request,
                        claims.session_id,
                        conv,
                        claims,
                        relpath,
                        sub_resource=sub_resource,
                        nonce=nonce,
                    )
                except HTTPException as exc:
                    status = 404 if exc.status_code in (400, 404) else 502
                    return _error_page(status, _sentence_for(status))

            try:
                return await _dispatch_reader()
            except Exception:
                # Capability URLs never surface JSON or the shell: any reader
                # fault answered as the page contract. The token stays out of
                # the log, so it cannot leak through an aggregator.
                logger.exception("artifact serve failed for session %s", claims.session_id)
                return _error_page(502, _LOAD_FAILED_SENTENCE)
