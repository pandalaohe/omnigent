"""Authenticated, exact-allowlist proxy for operator-trusted extension pages."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from omnigent.extensions import ExtensionPermission, ExtensionPluginState, ExternalPageContribution
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_REQUEST_HEADERS = ("content-type", "accept")
_RESPONSE_HEADERS = (
    "content-type",
    "content-language",
    "content-security-policy",
    "x-frame-options",
)
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def create_extension_proxy_router(
    state: ExtensionPluginState,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Expose only declared upstream routes behind the normal user authentication."""
    router = APIRouter()

    async def proxy_page(request: Request, extension_id: str, route: str, path: str) -> Response:
        require_user(request, auth_provider)
        manifest = state.get(extension_id)
        page = (
            next(
                (
                    page
                    for page in manifest.pages
                    if isinstance(page, ExternalPageContribution) and page.route == route
                ),
                None,
            )
            if manifest
            else None
        )
        if (
            page is None
            or page.proxy is None
            or manifest is None
            or ExtensionPermission.PAGES_PROXY not in manifest.permissions
        ):
            raise HTTPException(404, "Extension proxy not found")
        upstream_path = f"/{path}"
        if (
            b"%" in request.scope.get("raw_path", b"")
            or "\\" in path
            or (request.method, upstream_path) not in page.proxy.allowed_routes
        ):
            raise HTTPException(404, "Extension proxy route not allowed")

        origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.method not in {"GET", "HEAD"}:
            supplied_origin = request.headers.get("origin")
            try:
                same_origin = supplied_origin is not None and httpx.URL(
                    supplied_origin
                ) == httpx.URL(origin)
            except httpx.InvalidURL:
                same_origin = False
            if (
                not same_origin
                or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
            ):
                raise HTTPException(403, "Same-origin request required")

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_REQUEST_BYTES:
                raise HTTPException(413, "Extension proxy request too large")
        upstream = httpx.URL(page.url)
        base_path = getattr(request.app.state, "base_path", "") or request.scope.get(
            "root_path", ""
        )
        prefix = f"{base_path.rstrip('/')}/v1/extensions/{extension_id}/proxy/{route}"
        headers = {
            name: request.headers[name] for name in _REQUEST_HEADERS if name in request.headers
        }
        headers.update(page.proxy.request_headers)
        headers.update(
            {
                "host": upstream.netloc.decode("ascii"),
                "x-forwarded-prefix": prefix,
                "x-forwarded-host": request.url.netloc,
                "x-forwarded-proto": request.url.scheme,
            }
        )
        if request.method not in {"GET", "HEAD"}:
            headers["origin"] = str(upstream.copy_with(path="", query=None, fragment=None)).rstrip(
                "/"
            )
        target = upstream.copy_with(
            path=upstream_path, query=request.url.query.encode("ascii") or None, fragment=None
        )
        try:
            async with (
                httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client,
                client.stream(
                    request.method, target, headers=headers, content=bytes(body)
                ) as result,
            ):
                if result.is_redirect:
                    raise HTTPException(502, "Extension upstream redirect refused")
                content = bytearray()
                async for chunk in result.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise HTTPException(502, "Extension upstream response too large")
                response_headers = {
                    name: result.headers[name]
                    for name in _RESPONSE_HEADERS
                    if name in result.headers
                }
                response_headers.update(
                    {"cache-control": "no-store", "x-content-type-options": "nosniff"}
                )
                return Response(
                    bytes(content), status_code=result.status_code, headers=response_headers
                )
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "Extension upstream timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Extension upstream unavailable") from exc

    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        router.add_api_route(
            "/extensions/{extension_id}/proxy/{route}/{path:path}",
            proxy_page,
            methods=[method],
            operation_id=f"extension_proxy_{method.lower()}",
            response_class=Response,
        )
    return router
