"""Artifact capability-URL routes: mint, revoke, open, serve and gate.

The web UI mints a signed, session-bound token for one workspace file
(``POST /v1/sessions/{sid}/artifacts``) and loads the file from
``GET /v1/artifacts/<token>/<relpath>``. The URL is the credential: the
serve route reads no cookie, verifies the token against the session's
artifact-link key label, and pulls the bytes from the session's runner
(streamed) or, when that runner is offline, from the connected host over
its filesystem tunnel. A bundle token (an HTML entry) serves the entry's
folder and descendants; the reader enforces containment with ``within``
and the server refuses a response that does not confirm it.

The session owner's external-access gate runs between the path rules and
the read (``artifact_sharing``): a panel token passes outright while its
expiry lasts, an authorized logged-in visitor and a remembered browser are
redirected onto a short-lived grant, and everyone else meets the share-code
form or the sign-in page. The settings routes (``/v1/artifact-sharing``) and
the unlock POST that issues those credentials live here too.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http
import json
import logging
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator

from omnigent.artifact_paths import artifact_path_allowed, artifact_segments_valid
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.utils import now_epoch
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
from omnigent.server.artifact_sharing import (
    KEEP_SHARE_CODE,
    REMEMBER_COOKIE_MAX_AGE_SECONDS,
    ArtifactSharingStore,
    KeepShareCode,
    SharingRecord,
    UnlockFailureBudget,
    gate_key_id,
    grant_valid,
    issue_remember_cookie,
    mint_grant,
    remember_cookie_name,
    remember_cookie_valid,
    verify_share_code,
)
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id as _get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._gzip_route import skip_gzip
from omnigent.server.routes._oauth import RATE_LIMITER_MAX_KEYS, SlidingWindowRateLimiter
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.sessions.routes_resources import _RunnerStreamResponse
from omnigent.server.schemas import ArtifactOpenRequestEvent
from omnigent.stores import ConversationStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.comment_store.visitor_comments import (
    visitor_author as _visitor_author,
)
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)

# The web UI's environment resource id (see the file-panel routes).
_ENVIRONMENT_ID = "default"

# Per-file ceiling for a served artifact. The runner announces the size in
# Content-Length; a body without one is read into a buffer no larger than this.
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

# Panel tokens live long enough to keep an open panel working without a
# redirect; the viewer re-mints before they expire.
_PANEL_TOKEN_TTL_SECONDS = 12 * 60 * 60

# The owner walk for a sub-agent session: past this depth the chain is
# treated as unresolvable, which fails the gate closed.
_OWNER_CHAIN_MAX_HOPS = 8

# Settings writes per owner and unlock failures per owner; both windows are
# per process, like the other in-memory throttles in the server.
_SHARING_WRITE_RATE_MAX = 20
_SHARING_WRITE_RATE_WINDOW_SECONDS = 60
_UNLOCK_FAILURE_MAX = 10
_UNLOCK_FAILURE_WINDOW_SECONDS = 600
_UNLOCK_FAILURE_MAX_KEYS = 10_000

_GATE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'self'; base-uri 'none'"
)

# Headers on the gate's own pages. The artifact sandbox CSP would force an
# opaque origin, which would post the form with ``Origin: null``; these pages
# carry their own CSP instead and none of the artifact CORS/disposition
# headers.
_GATE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _GATE_CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}

_FORM_PAGE_TEMPLATE = (
    '<!doctype html>\n<html><head><meta charset="utf-8">'
    "<title>{status} {reason}</title></head>"
    "<body><h1>{status} {reason}</h1>"
    '<form method="post"><input type="password" name="code">'
    '<button type="submit">Open</button></form></body></html>\n'
)

_FORBIDDEN_PAGE_TEMPLATE = (
    '<!doctype html>\n<html><head><meta charset="utf-8">'
    "<title>403 Forbidden</title></head>"
    '<body><h1>403 Forbidden</h1><p><a href="{root}/">Home</a></p></body></html>\n'
)

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
_EXPIRED_SENTENCE = "This link has expired."
_TOO_MANY_ATTEMPTS_SENTENCE = "Too many attempts."
_VISIT_UNAVAILABLE_SENTENCE = "The visitor page is not available on this server."

_PAGE_STATUSES = frozenset({404, 410, 413, 502, 503})

_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r"</html\s*>", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script", re.IGNORECASE)
_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_MODULEPRELOAD_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*[\"']modulepreload[\"'])", re.IGNORECASE
)

# Visitor comments per link; in-memory and per process, like the other
# server-side throttles. Keyed by the link's identity, never by visitor.
_VISITOR_COMMENT_RATE_MAX = 20
_VISITOR_COMMENT_RATE_WINDOW_SECONDS = 600


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


def _gate_page(status: int, body: str) -> HTMLResponse:
    """Build a static gate page with the gate's own security headers."""
    return HTMLResponse(content=body, status_code=status, headers=dict(_GATE_HEADERS))


def _form_page(status: int) -> HTMLResponse:
    """The share-code form: one password field, one button, no echo.

    The form has no ``action``, so it posts back to the current URL.
    """
    reason = http.HTTPStatus(status).phrase
    return _gate_page(status, _FORM_PAGE_TEMPLATE.format(status=status, reason=reason))


def _forbidden_page(request: Request) -> HTMLResponse:
    """The sign-in page when the gate is switched off, with a link home."""
    base_path = getattr(request.app.state, "base_path", "") or ""
    return _gate_page(403, _FORBIDDEN_PAGE_TEMPLATE.format(root=base_path))


def _parse_token_segment(token: str) -> tuple[str, str]:
    """Split an artifact path segment into the bare token and the grant.

    The grant rides after the token on base64url output, which never
    contains ``~``, so the first ``~`` is the boundary. Every token use
    (decode, verify, bridge nonce) takes the bare token.

    :param token: The ``<token>`` or ``<token>~<grant>`` path segment.
    :returns: ``(bare_token, grant)``, with ``""`` when no grant is present.
    """
    bare, separator, grant = token.partition("~")
    return bare, grant if separator else ""


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


def _stamp_script_nonce(html: str, nonce: str) -> str:
    """Add *nonce* to every ``<script>`` and ``<link rel="modulepreload">``.

    The shell's CSP admits no other script source, so every executable tag
    the built entry carries must present the nonce the response's CSP names.

    :param html: The shell HTML, already asset-rebased.
    :param nonce: The per-response nonce.
    :returns: The HTML with ``nonce`` stamped on each script / preload tag.
    """
    stamped = _SCRIPT_OPEN_RE.sub(f'<script nonce="{nonce}"', html)
    return _MODULEPRELOAD_LINK_RE.sub(f'<link nonce="{nonce}"', stamped)


def _escape_json_for_html(payload: str) -> str:
    """Escape a JSON document so it cannot close its HTML ``<script>`` block.

    ``<`` / ``>`` / ``&`` and the line separators are rewritten to their
    ``\\u`` escapes; each stays valid JSON while the HTML parser sees no tag
    boundary or character reference.

    :param payload: The compact JSON text to embed.
    :returns: The escaped JSON text.
    """
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _insert_omni_visit_config(html: str, config_json: str) -> str:
    """Insert the ``#omni-visit-config`` data block into the shell HTML.

    Placed before ``</head>`` when present, else before the last ``</body>``
    / ``</html>``, else appended — the same placement ladder the in-frame
    asset injection uses.

    :param html: The shell HTML.
    :param config_json: The already-escaped JSON config text.
    :returns: The HTML carrying the config script tag.
    """
    tag = f'<script type="application/json" id="omni-visit-config">{config_json}</script>'
    index = _last_match_start(_HEAD_CLOSE_RE, html)
    if index == -1:
        index = _last_match_start(_BODY_CLOSE_RE, html)
    if index == -1:
        index = _last_match_start(_HTML_CLOSE_RE, html)
    if index == -1:
        index = len(html)
    return html[:index] + tag + html[index:]


def _visitor_error(
    status_code: int,
    reason: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a visitor-comment refusal, e.g. ``{"reason": "reload"}``.

    :param status_code: HTTP status to answer with.
    :param reason: Short machine-readable reason, e.g. ``"disabled"``.
    :param headers: Extra headers merged over the defaults, e.g.
        ``Retry-After``.
    :returns: The JSON refusal response.
    """
    return JSONResponse(status_code=status_code, content={"reason": reason}, headers=headers)


class VisitorCommentRequest(BaseModel):
    """Request body for ``POST /v1/artifact-comments``.

    :param token: The visitor shell's bare ``g`` token.
    :param grant: The link's grant from the open gate, or ``None`` when the
        gate admitted the request without one.
    :param path: Page path relative to the link's bundle root.
    :param body: The comment text (at most 4000 characters).
    :param anchor_content: Plain-text snapshot of the selected range, when
        the comment anchors text; ``None`` for a whole-page comment.
    :param start_index: 0-based character offset (inclusive) of the anchor.
    :param end_index: 0-based character offset (exclusive) of the anchor.
    :param name: Visitor-supplied display name; optional.
    """

    token: str
    grant: str | None = None
    path: str = Field(max_length=1024)
    body: str = Field(max_length=4000)
    anchor_content: str | None = Field(default=None, max_length=2000)
    start_index: int
    end_index: int
    name: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _validate_range(self) -> VisitorCommentRequest:
        """Reject an inverted or negative anchor range.

        :returns: The validated request unchanged.
        :raises ValueError: When ``0 <= start_index <= end_index`` fails.
        """
        if self.start_index < 0:
            raise ValueError("start_index must be >= 0")
        if self.end_index < self.start_index:
            raise ValueError("end_index must be >= start_index")
        return self


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
    comment_store: CommentStore | None = None,
) -> None:
    """Register the artifact capability-URL routes on *router*.

    Called from ``register_resources_routes`` so the mint and open paths
    reuse that function's authorization closures. The serve and preflight
    routes live here too, on the same router, so they are mounted under
    ``/v1`` before the SPA fallback. The visitor comment POST is registered
    only when *comment_store* is configured — without a store there is
    nowhere for a visitor comment to land, so the route does not exist.

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
    :param comment_store: Store visitor comments are inserted into; ``None``
        leaves the visitor comment route unregistered.
    """

    # The gate's per-app state. The sharing store connects on first use, so
    # an app that never serves an artifact pays nothing; the two throttles are
    # in-memory and per process, like the other server-side limiters.
    sharing_store: ArtifactSharingStore | None = None
    sharing_write_limiter = SlidingWindowRateLimiter(
        _SHARING_WRITE_RATE_MAX, _SHARING_WRITE_RATE_WINDOW_SECONDS, RATE_LIMITER_MAX_KEYS
    )
    unlock_failures = UnlockFailureBudget(
        max_failures=_UNLOCK_FAILURE_MAX,
        window_seconds=_UNLOCK_FAILURE_WINDOW_SECONDS,
        max_keys=_UNLOCK_FAILURE_MAX_KEYS,
    )

    def _get_sharing_store() -> ArtifactSharingStore:
        """Return (creating once per router) the owner-settings store."""
        nonlocal sharing_store
        if sharing_store is None:
            sharing_store = ArtifactSharingStore(conversation_store.storage_location)
        return sharing_store

    async def _authorize_edit(request: Request, session_id: str) -> None:
        """Require edit level on *session_id*, like the browser bridge routes."""
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )

    async def _resolve_link_owner(conv: Conversation) -> str | None:
        """Resolve the user whose sharing settings govern *conv*'s links.

        Sub-agent sessions carry no grant of their own, so the walk follows
        ``parent_conversation_id`` until a grant appears. An unresolved owner
        is not an error the visitor can see: the caller treats it as the
        switch being off, and grants are signed with an empty gate-key id.

        :param conv: The link's session.
        :returns: The owner's user id, ``RESERVED_USER_LOCAL`` when no auth
            provider is configured, or ``None``.
        """
        if auth_provider is None:
            return RESERVED_USER_LOCAL
        current = conv
        for _ in range(_OWNER_CHAIN_MAX_HOPS):
            owner = await asyncio.to_thread(_get_session_owner_id, current.id, permission_store)
            if owner is not None:
                return owner
            parent_id = current.parent_conversation_id
            if parent_id is None:
                return None
            parent = await asyncio.to_thread(conversation_store.get_conversation, parent_id)
            if parent is None:
                return None
            current = parent
        return None

    def _is_login(user_id: str | None) -> bool:
        """Whether *user_id* is a real identity, not a reserved sentinel."""
        return user_id is not None and user_id != RESERVED_USER_LOCAL

    async def _identity_allowed(request: Request, claims: ArtifactTokenClaims) -> bool:
        """Whether the request's identity passes the mint's read rule.

        The rule is the one the mint applied (``_authorize_browse_read`` with
        the joined root+entry; an absolute target always takes the owner
        check). The request carries the identity, so the provider resolves it
        again inside the token's workspace scope — the same identity the
        route read before entering that scope.

        :param request: The incoming request, for the auth provider.
        :param claims: The verified token claims.
        :returns: ``True`` when the read rule admits the caller.
        """
        if claims.absolute:
            target = "/" + _posix_join(claims.root.lstrip("/"), claims.entry)
        else:
            target = _posix_join(claims.root, claims.entry)
        try:
            await _authorize_browse_read(claims.session_id, request, target)
        except OmnigentError:
            return False
        return True

    def _grant_redirect(
        request: Request,
        token: str,
        relpath: str,
        session_key: bytes,
        gate_key_identifier: str,
    ) -> Response:
        """303 the visitor onto the same URL carrying a fresh grant.

        The location is path-only (the host belongs to the client) and keeps
        the original query string.
        """
        grant = mint_grant(session_key, token, gate_key_identifier, now_epoch())
        base_path = getattr(request.app.state, "base_path", "") or ""
        location = f"{base_path}/v1/artifacts/{token}~{grant}/{urllib.parse.quote(relpath)}"
        query = request.url.query
        if query:
            location = f"{location}?{query}"
        return RedirectResponse(location, status_code=303)

    async def _read_gate_record(conv: Conversation) -> tuple[str | None, SharingRecord | None]:
        """Resolve the link owner and read the record governing *conv*.

        :param conv: The link's session.
        :returns: ``(owner, record)`` — the gate's inputs, both unresolved as
            ``None``. The caller treats an unresolved owner as failing closed.
        """
        owner = await _resolve_link_owner(conv)
        record = (
            await asyncio.to_thread(_get_sharing_store().read_sharing, owner)
            if owner is not None
            else None
        )
        return owner, record

    async def _gate_response(
        request: Request,
        user_id: str | None,
        claims: ArtifactTokenClaims,
        token: str,
        grant: str,
        relpath: str,
        conv: Conversation,
        session_key: bytes,
    ) -> tuple[Response | None, SharingRecord | None]:
        """Apply the owner's external-access gate.

        Decision order (design §2.5): an unexpired panel token passes; a
        valid grant passes; an open gate (no record, or no code) passes; an
        authorized logged-in visitor and a remembered browser are redirected
        onto a fresh grant; otherwise the share-code form or the sign-in
        page answers. An owner that cannot be resolved fails closed like a
        switched-off gate.

        :returns: ``(decision, record)`` — a ``None`` decision means serve,
        and the record is the one the decision evaluated (``None`` for a
        panel token or an unresolvable owner). The visitor shell mints its
        grants from that record, never from a second read that a settings
        change could race.
        """
        now = now_epoch()
        if claims.view == "p":
            if claims.expires_at is not None and claims.expires_at > now:
                return None, None
            return _error_page(410, _EXPIRED_SENTENCE), None
        owner, record = await _read_gate_record(conv)
        gate_key_identifier = gate_key_id(record.gate_key) if record is not None else ""
        if grant and grant_valid(session_key, token, grant, gate_key_identifier, now):
            return None, record
        if owner is None:
            # Nothing can be consulted, so the gate fails closed: only a
            # logged-in user passing the mint's read rule gets in.
            if _is_login(user_id) and await _identity_allowed(request, claims):
                return (
                    _grant_redirect(request, token, relpath, session_key, gate_key_identifier),
                    record,
                )
            return _forbidden_page(request), record
        # Gate open: no record (the user default is external on, no code) or
        # external on with no code. A URL whose grant is bad is evaluated
        # here as a bare one.
        if record is None or (record.external and record.code_hash is None):
            return None, record
        if _is_login(user_id) and await _identity_allowed(request, claims):
            return (
                _grant_redirect(request, token, relpath, session_key, gate_key_identifier),
                record,
            )
        if not record.external:
            return _forbidden_page(request), record
        cookie_name = remember_cookie_name(owner, secure=request.url.scheme == "https")
        cookie = request.cookies.get(cookie_name)
        if cookie and remember_cookie_valid(record.gate_key, owner, cookie, now):
            return (
                _grant_redirect(request, token, relpath, session_key, gate_key_identifier),
                record,
            )
        return _form_page(401), record

    def _remember_cookie_path(request: Request) -> str:
        """The cookie's Path: the artifact route family under the base path."""
        base_path = getattr(request.app.state, "base_path", "") or ""
        return f"{base_path}/v1/artifacts/"

    def _attach_remember_cookie(
        response: Response, request: Request, owner: str, gate_key: str
    ) -> None:
        """Set the artifact-scoped remember cookie on a successful unlock."""
        secure = request.url.scheme == "https"
        response.set_cookie(
            remember_cookie_name(owner, secure=secure),
            issue_remember_cookie(gate_key, owner, now_epoch()),
            max_age=REMEMBER_COOKIE_MAX_AGE_SECONDS,
            path=_remember_cookie_path(request),
            httponly=True,
            samesite="lax",
            secure=secure,
        )

    async def _load_verified_target(
        token: str,
        relpath: str,
        claims: ArtifactTokenClaims,
    ) -> tuple[Conversation, bytes] | Response:
        """Load and verify the token target; a :class:`Response` is a refusal.

        Covers the shared head of GET and POST: conversation lookup,
        archive, key verification and the lexical path rules. Must run
        inside the token's workspace scope.

        :param token: The bare token (no grant suffix).
        :param relpath: The URL-decoded request path.
        :param claims: Claims decoded from *token*.
        :returns: ``(conversation, session_key)`` or an error page.
        """
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
        return conv, key

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
        if claims.view in ("p", "h") and _is_html(content_type):
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
        if content_length is None or (claims.view in ("p", "h") and _is_html(content_type)):
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

    async def _dispatch_reader(
        request: Request,
        claims: ArtifactTokenClaims,
        conv: Conversation,
        session_key: bytes,
        token: str,
        relpath: str,
    ) -> Response:
        """Serve from the runner, falling back to the host when offline.

        The bare token is what feeds ``bridge_nonce``; the grant suffix is
        never part of the injected nonce.
        """
        # ``within`` applies to bundle sub-resources only: the entry itself
        # is authorized by the mint, and a directly opened dotfile is a file
        # token (served as-is).
        sub_resource = claims.kind == "b" and relpath != claims.entry
        nonce = bridge_nonce(session_key, token)
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

    def _visit_shell_response(
        request: Request,
        claims: ArtifactTokenClaims,
        session_key: bytes,
        token: str,
        relpath: str,
        record: SharingRecord | None,
    ) -> Response:
        """Render the visitor shell for an admitted ``g`` request.

        Reads the built ``visit.html``, rebases its asset references to the
        deployment's dist URL, stamps a per-response nonce over every script
        and module preload, and embeds the visit config. The ``h`` frame and
        both grants are minted from *record* — the record the gate just
        evaluated — so a settings change cannot race the shell's grants.

        :param request: The incoming request, for the base path.
        :param claims: The verified ``g`` claims.
        :param session_key: The session's artifact-link key.
        :param token: The bare ``g`` token.
        :param relpath: The requested HTML path relative to the bundle root.
        :param record: The sharing record the gate evaluated, or ``None``.
        :returns: The shell response, or a 503 page when no build exists.
        """
        shell = _read_in_frame_asset("visit.html")
        if shell is None:
            # A source checkout without a web build has no visitor entry;
            # answer the link with a clear page instead of a traceback.
            return _error_page(503, _VISIT_UNAVAILABLE_SENTENCE)
        base_path = getattr(request.app.state, "base_path", "") or ""
        # The same rebase the SPA index gets; relative ./assets/ would
        # otherwise resolve under the artifact path and fetch bundle files.
        from omnigent.server.app import _rewrite_web_ui_index

        html = _rewrite_web_ui_index(shell, base_path)
        nonce = secrets.token_urlsafe(16)
        html = _stamp_script_nonce(html, nonce)
        h_token = encode_artifact_token(
            session_key,
            session_id=claims.session_id,
            workspace_id=claims.workspace_id,
            root=claims.root,
            absolute=claims.absolute,
            entry=claims.entry,
            kind=claims.kind,
            view="h",
        )
        # A grant is due exactly when the gate the visitor passed requires
        # one; an open gate carries none, so the frame URL stays bare.
        grant: str | None = None
        frame_grant = ""
        if record is not None and (not record.external or record.code_hash is not None):
            now = now_epoch()
            gate_key_identifier = gate_key_id(record.gate_key)
            grant = mint_grant(session_key, token, gate_key_identifier, now)
            frame_grant = f"~{mint_grant(session_key, h_token, gate_key_identifier, now)}"
        frame_url = (
            f"{base_path}/v1/artifacts/{h_token}{frame_grant}/{urllib.parse.quote(relpath)}"
        )
        config = {
            "frameUrl": frame_url,
            "nonce": bridge_nonce(session_key, h_token),
            "token": token,
            "grant": grant,
            "path": relpath,
            "commentsEnabled": comment_store is not None
            and (record is None or record.allow_comments),
        }
        html = _insert_omni_visit_config(
            html, _escape_json_for_html(json.dumps(config, separators=(",", ":")))
        )
        headers = {
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}' 'strict-dynamic'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-src 'self'; form-action 'none'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex",
        }
        skip_gzip(request)
        return Response(content=html.encode("utf-8"), media_type="text/html", headers=headers)

    async def _serve_admitted(
        request: Request,
        claims: ArtifactTokenClaims,
        conv: Conversation,
        session_key: bytes,
        token: str,
        relpath: str,
        record: SharingRecord | None,
    ) -> Response:
        """Serve an admitted request per its view.

        A ``g`` request whose path names HTML renders the visitor shell;
        every other admitted request (including ``h`` and non-HTML ``g``)
        goes to the byte readers.

        :param request: The incoming request.
        :param claims: The verified token claims.
        :param conv: The link's session.
        :param session_key: The session's artifact-link key.
        :param token: The bare token.
        :param relpath: Requested path relative to the bundle root.
        :param record: The sharing record the gate evaluated, or ``None``.
        :returns: The artifact response.
        """
        if claims.view == "g" and _is_html(_guess_media_type(relpath)):
            return _visit_shell_response(request, claims, session_key, token, relpath, record)
        return await _dispatch_reader(request, claims, conv, session_key, token, relpath)

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
        :param body: ``{"path": <str>, "base"?: "host",
            "view": <"panel"|"raw"|"visit">}``.
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
        if view not in ("panel", "raw", "visit"):
            raise OmnigentError(
                "artifacts 'view' must be 'panel', 'raw' or 'visit'",
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
        # A panel token carries an expiry and passes the owner's gate
        # outright; raw-view and visit tokens stay deterministic and gated.
        # Only an HTML entry can open the visitor shell; any other file keeps
        # the raw view, so a visit request never widens what a link serves.
        token_view: Literal["p", "r", "g"] = "p" if view == "panel" else "r"
        if view == "visit" and kind == "b":
            token_view = "g"
        expires_at = now_epoch() + _PANEL_TOKEN_TTL_SECONDS if view == "panel" else None
        token = encode_artifact_token(
            key,
            session_id=session_id,
            workspace_id=current_workspace_id(),
            root=root,
            absolute=absolute,
            entry=entry,
            kind=kind,
            view=token_view,
            expires_at=expires_at,
        )
        return {
            "url": f"/v1/artifacts/{token}/{urllib.parse.quote(entry)}",
            "nonce": bridge_nonce(key, token),
            "kind": "bundle" if kind == "b" else "file",
            "expires_at": expires_at,
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

    async def _serve_with_gate(
        request: Request,
        user_id: str | None,
        token: str,
        relpath: str,
    ) -> Response:
        """Run the shared head (verify, path rules, gate) then the readers.

        :param request: The incoming request.
        :param user_id: Identity read before the workspace scope, or ``None``.
        :param token: The token segment, possibly carrying a grant suffix.
        :param relpath: Path relative to the bundle root (URL-decoded).
        :returns: The artifact response, refusal page, or redirect.
        """
        bare_token, grant = _parse_token_segment(token)
        claims = decode_artifact_token(bare_token)
        if claims is None:
            return _error_page(404, _NOT_FOUND_SENTENCE)
        # Stores scope every row by ``current_workspace_id()``, so the token's
        # minting workspace must be rebound or a link minted in workspace N
        # finds no conversation.
        with workspace_scope(claims.workspace_id):
            try:
                loaded = await _load_verified_target(bare_token, relpath, claims)
                if isinstance(loaded, Response):
                    return loaded
                conv, session_key = loaded
                gate, record = await _gate_response(
                    request, user_id, claims, bare_token, grant, relpath, conv, session_key
                )
                if gate is not None:
                    return gate
                return await _serve_admitted(
                    request, claims, conv, session_key, bare_token, relpath, record
                )
            except Exception:
                # Capability URLs never surface JSON or the shell: any lookup
                # or reader fault answered as the page contract. The token
                # stays out of the log, so it cannot leak through an
                # aggregator.
                logger.exception("artifact serve failed for session %s", claims.session_id)
                return _error_page(502, _LOAD_FAILED_SENTENCE)

    @router.get(
        "/artifacts/{token}/{relpath:path}",
        # Internal serve route — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def serve_artifact(request: Request, token: str, relpath: str) -> Response:
        """
        Serve one artifact file (or bundle sub-resource) by capability URL.

        The token is the credential; the owner's external-access gate runs
        after the path rules and before any read, answering a redirect, the
        share-code form, or a sign-in page for a closed gate. The runner is
        the primary byte source; a runner that is offline falls back to the
        host tunnel, and either reader must confirm bundle containment
        (``X-Omnigent-Within`` / ``within_enforced``) before the bytes are
        served. Every failure is a static HTML page, never JSON and never
        the SPA.

        :param request: The incoming request.
        :param token: The signed artifact token, optionally ``~`` a grant.
        :param relpath: Path relative to the bundle root (URL-decoded).
        :returns: The file bytes with the artifact security headers, or a
            page (303 / 401 / 403 / 404 / 410 / 413 / 502 / 503).
        """
        # The identity is read before entering the token's workspace scope:
        # the provider's per-connection cache is keyed by the ambient
        # workspace id, and the gate's owner lookup runs under the token's.
        user_id = _get_user_id(request, auth_provider)
        return await _serve_with_gate(request, user_id, token, relpath)

    @router.post(
        "/artifacts/{token}/{relpath:path}",
        # Internal unlock flow — the share-code form posts to the current URL.
        include_in_schema=False,
        response_model=None,
    )
    async def unlock_artifact(request: Request, token: str, relpath: str) -> Response:
        """
        Submit the share code and receive a grant plus a remember cookie.

        The token is checked as the GET route checks it (404 / 410 pages).
        The owner's *current* record must have external access on and a code
        set; otherwise nothing is issued and the GET decision answers (403
        when off, the bytes when open). Each attempt claims a slot of the
        owner's failure budget before the code is verified, and a match
        releases it. A correct code sets the remember cookie and 303s onto a
        fresh grant; a wrong one returns the form again with no hint. A
        spent budget refuses further attempts with a 429 page.

        :param request: The incoming request carrying the form body.
        :param token: The signed artifact token, optionally ``~`` a grant.
        :param relpath: Path relative to the bundle root (URL-decoded).
        :returns: 303 with the cookie, or a refusal page.
        """
        user_id = _get_user_id(request, auth_provider)
        bare_token, _grant = _parse_token_segment(token)
        claims = decode_artifact_token(bare_token)
        if claims is None:
            return _error_page(404, _NOT_FOUND_SENTENCE)
        with workspace_scope(claims.workspace_id):
            try:
                loaded = await _load_verified_target(bare_token, relpath, claims)
                if isinstance(loaded, Response):
                    return loaded
                conv, session_key = loaded
                owner, record = await _read_gate_record(conv)
                # A panel token never enters the unlock flow: it is decided
                # exactly as the GET route decides it (serve while unexpired,
                # 410 past it) and issues nothing. Every other view (raw and
                # the visitor views) unlocks through the share code.
                if (
                    claims.view == "p"
                    or owner is None
                    or record is None
                    or not record.external
                    or record.code_hash is None
                ):
                    gate, evaluated = await _gate_response(
                        request, user_id, claims, bare_token, "", relpath, conv, session_key
                    )
                    if gate is not None:
                        return gate
                    # An open gate still answers with the view it would serve:
                    # the shell for a g HTML request, bytes otherwise.
                    return await _serve_admitted(
                        request, claims, conv, session_key, bare_token, relpath, evaluated
                    )
                now = time.time()
                # The attempt is counted before the form is read and the
                # verify runs; recording only failures afterwards let N
                # parallel wrong codes all pass the spent-budget check.
                if not unlock_failures.reserve(owner, now):
                    return _gate_page(
                        429,
                        _ERROR_PAGE_TEMPLATE.format(
                            status=429,
                            reason=http.HTTPStatus(429).phrase,
                            sentence=_TOO_MANY_ATTEMPTS_SENTENCE,
                        ),
                    )
                form = await request.form()
                code = form.get("code")
                submitted = code.strip() if isinstance(code, str) else ""
                if not await asyncio.to_thread(verify_share_code, submitted, record.code_hash):
                    return _form_page(401)
                unlock_failures.release(owner, now)
                response = _grant_redirect(
                    request, bare_token, relpath, session_key, gate_key_id(record.gate_key)
                )
                _attach_remember_cookie(response, request, owner, record.gate_key)
                return response
            except Exception:
                # The unlock flow answers pages only; a lookup fault must not
                # surface JSON. The token stays out of the log.
                logger.exception("artifact unlock failed for session %s", claims.session_id)
                return _error_page(502, _LOAD_FAILED_SENTENCE)

    @router.get(
        "/artifact-sharing",
        # Internal settings flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def get_artifact_sharing(request: Request) -> dict[str, Any]:
        """
        Return the caller's external-access settings summary.

        No row means the defaults: external access on, no share code, visitor
        comments on. The stored code hash and gate key never leave the server.

        :param request: The incoming request, for the caller identity.
        :returns: ``{"external": <bool>, "share_code_set": <bool>,
            "allow_comments": <bool>}``.
        :raises OmnigentError: 401 when authentication is required and absent.
        """
        owner = _require_user(request, auth_provider) or RESERVED_USER_LOCAL
        record = await asyncio.to_thread(_get_sharing_store().read_sharing, owner)
        return {
            "external": record.external if record is not None else True,
            "share_code_set": record is not None and record.code_hash is not None,
            "allow_comments": record.allow_comments if record is not None else True,
        }

    @router.put(
        "/artifact-sharing",
        # Internal settings flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def put_artifact_sharing(
        request: Request,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Update the caller's external-access switch, share code and comments
        switch.

        An absent field keeps its value; ``share_code: null`` clears the
        code; a string is stripped and must be 4–64 characters. Every
        successful write regenerates the gate key — except one that changes
        only ``allow_comments``, which keeps it so the switch does not sign
        open visitors out.

        :param request: The incoming request, for the caller identity.
        :param body: ``{"external"?: <bool>, "share_code"?: <str | null>,
            "allow_comments"?: <bool>}``.
        :returns: The same shape as the GET.
        :raises OmnigentError: 400 invalid input, 401 unauthenticated.
        :raises HTTPException: 429 when the per-owner write rate is exceeded.
        """
        owner = _require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if not sharing_write_limiter.allow(owner, time.time()):
            raise HTTPException(
                status_code=429,
                detail="Too many sharing settings updates",
                headers={"Retry-After": "60"},
            )
        external = body.get("external")
        if "external" in body and not isinstance(external, bool):
            raise OmnigentError(
                "artifact-sharing 'external' must be a boolean",
                code=ErrorCode.INVALID_INPUT,
            )
        allow_comments = body.get("allow_comments")
        if "allow_comments" in body and not isinstance(allow_comments, bool):
            raise OmnigentError(
                "artifact-sharing 'allow_comments' must be a boolean",
                code=ErrorCode.INVALID_INPUT,
            )
        share_code: str | None | KeepShareCode = KEEP_SHARE_CODE
        if "share_code" in body:
            raw_code = body["share_code"]
            if raw_code is None:
                share_code = None
            elif isinstance(raw_code, str):
                stripped = raw_code.strip()
                if not 4 <= len(stripped) <= 64:
                    raise OmnigentError(
                        "artifact-sharing 'share_code' must be 4–64 characters",
                        code=ErrorCode.INVALID_INPUT,
                    )
                share_code = stripped
            else:
                raise OmnigentError(
                    "artifact-sharing 'share_code' must be a string or null",
                    code=ErrorCode.INVALID_INPUT,
                )
        record = await asyncio.to_thread(
            _get_sharing_store().write_sharing,
            owner,
            external=external if isinstance(external, bool) else None,
            share_code=share_code,
            allow_comments=allow_comments if isinstance(allow_comments, bool) else None,
        )
        return {
            "external": record.external,
            "share_code_set": record.code_hash is not None,
            "allow_comments": record.allow_comments,
        }

    if comment_store is not None:
        # Visitor comments per link, in-memory and per process. Keyed by the
        # link's identity (session, key, root, entry), never by the visitor,
        # so one link's flood cannot spend another link's budget.
        visitor_comment_limiter = SlidingWindowRateLimiter(
            _VISITOR_COMMENT_RATE_MAX,
            _VISITOR_COMMENT_RATE_WINDOW_SECONDS,
            RATE_LIMITER_MAX_KEYS,
        )

        @router.post(
            "/artifact-comments",
            # Internal visitor flow — hidden from the public API reference.
            include_in_schema=False,
            response_model=None,
            dependencies=[
                Depends(require_trusted_origin),
                Depends(require_json_content_type),
            ],
        )
        async def add_visitor_comment(
            request: Request,
            body: VisitorCommentRequest,
        ) -> Response:
            """
            Record one comment left by a visitor behind a share link.

            The body's token is the authority and its grant the gate
            credential; no ambient identity is consulted. The target is
            verified exactly as the serve route verifies it, the owner's
            current gate must admit the request, the comments switch must be
            on, and the per-link window must have room. The row is stored on
            the workspace path the owner's own comments use, authored as
            ``visitor:<name>``; nothing about the comment is returned.

            :param request: The incoming request, for the Origin check and
                the unresolvable-owner fallback.
            :param body: The visitor comment payload.
            :returns: 201 ``{"ok": true}``, or a JSON refusal (403
                ``{"reason": "reload"|"disabled"}``, 404, 410, 413/422, 429).
            """
            claims = decode_artifact_token(body.token)
            if claims is None or claims.view != "g":
                return _visitor_error(404, "not_found")
            with workspace_scope(claims.workspace_id):
                loaded = await _load_verified_target(body.token, body.path, claims)
                if isinstance(loaded, Response):
                    if loaded.status_code == 410:
                        return _visitor_error(410, "gone")
                    return _visitor_error(404, "not_found")
                conv, session_key = loaded
                # Only an HTML page can host comment anchors; a non-HTML
                # path under a bundle stays unserved here, as it is served
                # as raw bytes there.
                if not _is_html(_guess_media_type(body.path)):
                    return _visitor_error(404, "not_found")
                owner, record = await _read_gate_record(conv)
                now = now_epoch()
                gate_key_identifier = gate_key_id(record.gate_key) if record is not None else ""
                admitted = bool(body.grant) and grant_valid(
                    session_key,
                    body.token,
                    body.grant or "",
                    gate_key_identifier,
                    now,
                )
                if owner is None:
                    # Mirror the serve gate: with no resolvable owner only the
                    # mint's read rule admits a caller, grants included.
                    if not admitted:
                        user_id = _get_user_id(request, auth_provider)
                        admitted = _is_login(user_id) and await _identity_allowed(request, claims)
                elif record is None or (record.external and record.code_hash is None):
                    # Open gate: admitted without a grant.
                    admitted = True
                if not admitted:
                    return _visitor_error(403, "reload")
                if record is not None and not record.allow_comments:
                    return _visitor_error(403, "disabled")
                window_key = f"{claims.session_id}:{claims.key_id}:{claims.root}:{claims.entry}"
                if not visitor_comment_limiter.allow(window_key, time.time()):
                    return _visitor_error(
                        429,
                        "too_many",
                        headers={"Retry-After": str(_VISITOR_COMMENT_RATE_WINDOW_SECONDS)},
                    )
                # The workspace path the owner's own comments carry, so the
                # row lands in the owner's per-file thread.
                stored_path = _posix_join(claims.root, body.path)
                if claims.absolute:
                    stored_path = "/" + stored_path.lstrip("/")
                await asyncio.to_thread(
                    comment_store.add,
                    conversation_id=claims.session_id,
                    path=stored_path,
                    body=body.body,
                    start_index=body.start_index,
                    end_index=body.end_index,
                    anchor_content=body.anchor_content,
                    created_by=_visitor_author(body.name),
                )
                return JSONResponse(status_code=201, content={"ok": True})
