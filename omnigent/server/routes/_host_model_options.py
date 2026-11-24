"""One round-trip helper for ``host.model_options``.

Two callers ask a host which models a harness could launch with: the
``/v1/hosts/{id}/model-options`` route (which turns a failure into an HTTP
error) and the session-create routing path (which degrades to no
candidates). Only the failure handling differs, so the request-id /
future / frame / timeout / cleanup shape lives here once.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import HostModelOptionsFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore


async def request_host_model_options(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    timeout_s: float,
    refresh: str | None = None,
) -> dict[str, Any]:
    """
    Send a ``host.model_options`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to query.
    :param harness: Native harness id, e.g. ``"claude-native"``.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload, e.g. ``{"status": "ok", "models": [...]}``.
    :raises ConnectionError: The host connection dropped before the frame
        could be enqueued.
    :raises asyncio.TimeoutError: The host did not answer within
        *timeout_s*.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_model_options[request_id] = future
    frame = encode_host_frame(
        HostModelOptionsFrame(request_id=request_id, harness=harness, refresh=refresh)
    )
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_model_options.pop(request_id, None)


async def record_host_model_options(
    catalog_store: HostModelCatalogCacheStore,
    host_id: str,
    harness: str,
    result: dict[str, Any],
    *,
    fetched_at: int,
) -> None:
    """Record a host response while keeping the last good ACP catalog on an empty probe."""
    models = result.get("models")
    error = result.get("error")
    if result.get("status") == "ok" and isinstance(models, list):
        payload = [row for row in models if isinstance(row, dict)]
        if payload or not (harness == "acp" or harness.startswith("acp:")):
            await asyncio.to_thread(
                catalog_store.upsert,
                host_id,
                harness,
                payload,
                fetched_at,
                permission_options=result.get("permission_options"),
                injects_prompt=result.get("injects_prompt"),
            )
            return
        error = "ACP probe returned an empty model catalog"
    if not isinstance(error, str) or not error:
        error = "host model-options lookup failed"
    await asyncio.to_thread(
        catalog_store.mark_error,
        host_id,
        harness,
        error,
        injects_prompt=result.get("injects_prompt"),
    )
