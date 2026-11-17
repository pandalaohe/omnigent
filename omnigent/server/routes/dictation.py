"""Streaming dictation route: the transcription WebSocket.

This module hosts the server-side speech-to-text surface behind the
composer mic button (``designs/server-dictation.md``):

- ``WS /v1/dictation/stream`` — one connection per dictation take.
- ``POST /v1/dictation/punctuation`` — restore punctuation in one completed
  browser-speech transcript on the main server only.

Availability is advertised as ``dictation_available`` on ``GET /v1/info``
(the web UI's boot-time capability probe); there is no separate probe
endpoint.

Wire protocol on the WebSocket
------------------------------

- **Client → server, binary frames**: raw 16 kHz mono s16le PCM. The
  browser worklet downsamples from the capture rate before sending.
- **Client → server, text frames**: JSON control messages.
  ``{"type": "stop"}`` asks the server to flush trailing audio and
  finish the take. Unknown shapes are ignored so future control
  messages don't break older servers.
- **Server → client, text frames**: JSON events.
    - ``{"type": "processing"}`` — hosted transcription is running; the client
      waits for completion or explicit cancellation.
    - ``{"type": "ready"}`` — sent once after the engine is ready;
      the client may start streaming audio.
    - ``{"type": "partial", "text": ...}`` — revisable in-progress
      utterance, throttled server-side.
    - ``{"type": "final", "text": ...}`` — an utterance completed by
      endpoint detection (a pause). The client appends it and clears
      its partial region.
    - ``{"type": "stopped", "text": ...}`` — reply to ``stop``: the
      flushed tail utterance (possibly empty). The server closes the
      socket after sending it.
    - ``{"type": "error", "message": ...}`` — fatal; the server closes.

Auth
----

Dictation is not session-scoped — the new-chat composer dictates before
any session exists — so the check is identity-level only, matching
``GET /v1/harnesses``: when an auth provider is configured the caller
must be authenticated (the WebSocket handshake carries identity via the
ingress/dev proxy exactly like the terminal-attach socket); in
single-user/dev mode the route is open.

Capacity
--------

Decoding is CPU-bound, so concurrent takes are capped (default 2,
``OMNIGENT_DICTATION_MAX_STREAMS``). Over-cap connections are accepted
and immediately closed with code 1013 (try again later) so the client
can distinguish "busy" from "broken".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any, Final

import anyio
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from pydantic import BaseModel, Field
from starlette import status

from omnigent.debug_logging import debug_event
from omnigent.server.auth import AuthProvider
from omnigent.server.dictation import (
    DictationEngine,
    DictationStreamHandle,
    DictationTakeError,
    DictationUpdate,
    PunctuationRestorer,
    get_engine,
    get_punctuation_restorer,
    max_streams,
)
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

_WS_CLOSE_TRY_AGAIN_LATER: Final[int] = 1013
_WS_CLOSE_INTERNAL_ERROR: Final[int] = 1011

#: Minimum interval between partial-transcript pushes. Keeps the socket
#: chatty enough for live text without a frame per audio chunk.
_PARTIAL_INTERVAL_S: Final[float] = 0.15
_MAX_PUNCTUATION_CHARS: Final[int] = 500


class DictationPunctuationRequest(BaseModel):
    """One completed transcript that needs punctuation restoration."""

    text: str = Field(min_length=1, max_length=_MAX_PUNCTUATION_CHARS)


class DictationPunctuationResponse(BaseModel):
    """Display-ready transcript returned to the composer."""

    text: str


def create_dictation_router(
    *,
    auth_provider: AuthProvider | None = None,
    engine_provider: Callable[[], DictationEngine] | None = None,
    punctuation_provider: Callable[[], PunctuationRestorer] | None = None,
    include_punctuation: bool = True,
    settings_loader=None,
    hosted_factory=None,
) -> APIRouter:
    """Build the router carrying the dictation stream route.

    Wired into the FastAPI app under the ``/v1`` prefix in
    :func:`omnigent.server.app.create_app`.

    :param auth_provider: Optional provider used to authenticate the
        WebSocket handshake. ``None`` preserves single-user/dev
        behavior (open).
    :param engine_provider: Engine factory override for tests. Defaults
        to :func:`omnigent.server.dictation.get_engine`, which resolves
        the configured engine and loads models on first use.
    :param punctuation_provider: Final-text punctuation factory override
        for tests. Defaults to the lazy process-wide restorer.
    :param include_punctuation: Whether to mount the browser-facing HTTP
        punctuation route. The standalone audio worker disables it.
    :returns: An :class:`APIRouter` carrying the stream route.
    """
    router = APIRouter()
    resolve_engine = engine_provider or get_engine
    resolve_punctuation = punctuation_provider or get_punctuation_restorer
    # Router-scoped so each app (and each test app) gets its own cap.
    slots = asyncio.Semaphore(max_streams())
    hosted_slots = asyncio.Semaphore(
        max(1, int(os.environ.get("OMNIGENT_DICTATION_HOSTED_MAX_STREAMS", "4")))
    )
    punctuation_slots = asyncio.Semaphore(1)
    closing_tasks: set[asyncio.Task[None]] = set()

    async def release_after_worker(
        wait_closed: Callable[[], None], take_slots: asyncio.Semaphore
    ) -> None:
        await asyncio.to_thread(wait_closed)
        take_slots.release()

    async def restore_dictation_punctuation(
        request: Request,
        body: DictationPunctuationRequest,
    ) -> DictationPunctuationResponse:
        """Restore Chinese/English punctuation in a completed transcript."""
        require_user(request, auth_provider)
        if punctuation_slots.locked():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="dictation punctuation busy",
            )
        await punctuation_slots.acquire()

        async def _load_and_restore() -> str | None:
            try:
                restorer = await asyncio.to_thread(resolve_punctuation)
                return await asyncio.to_thread(restorer.restore, body.text)
            except Exception as exc:  # noqa: BLE001 - contain initialization failures  # noqa: BLE001 - contain model failures as None
                _logger.error("dictation punctuation failed (%s)", type(exc).__name__)
                return None
            finally:
                punctuation_slots.release()

        work = asyncio.create_task(_load_and_restore())
        try:
            restored = await asyncio.shield(work)
        except asyncio.CancelledError:
            # Worker keeps the slot until inference finishes and contains its own failures.
            raise
        if restored is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dictation punctuation unavailable",
            )
        return DictationPunctuationResponse(text=restored)

    if include_punctuation:
        router.add_api_route(
            "/dictation/punctuation",
            restore_dictation_punctuation,
            methods=["POST"],
            response_model=DictationPunctuationResponse,
            responses={
                429: {"description": "Punctuation model busy"},
                503: {"description": "Punctuation model unavailable"},
            },
        )

    @router.websocket("/dictation/stream")
    async def dictation_stream(websocket: WebSocket) -> None:
        """Transcribe one dictation take (see module docstring)."""
        if auth_provider is not None and auth_provider.get_user_id(websocket) is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="authentication required",
            )
        await websocket.accept()
        _logger.info(
            "dictation stream connected",
            extra=debug_event("dictation_stream", phase="connected"),
        )

        from omnigent.server.auth import RESERVED_USER_LOCAL
        from omnigent.server.dictation_hosted import engine_for_settings
        from omnigent.server.voice_input import VoiceInputSettings

        owner = auth_provider.get_user_id(websocket) if auth_provider else RESERVED_USER_LOCAL
        try:
            settings = await asyncio.to_thread(settings_loader, owner) if settings_loader else None
            settings = settings or VoiceInputSettings()
        except Exception:  # noqa: BLE001 - redact provider and credential failures
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Voice settings unavailable",
                    "code": "third_party_unavailable",
                }
            )
            await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
            return
        third = settings.speech_source == "third"
        take_slots = hosted_slots if third else slots
        if take_slots.locked():
            await websocket.close(
                code=_WS_CLOSE_TRY_AGAIN_LATER,
                reason="dictation is at capacity; try again shortly",
            )
            return

        await take_slots.acquire()
        handle: DictationStreamHandle | None = None
        try:
            # Engine construction loads model weights — seconds on first
            # use. Run it off-loop; later takes reuse the shared engine.
            try:
                engine = (
                    await asyncio.to_thread(hosted_factory or engine_for_settings, settings)
                    if third
                    else await asyncio.to_thread(resolve_engine)
                )
                handle = await asyncio.to_thread(engine.create_stream)
            except Exception as exc:  # noqa: BLE001 - contain initialization failures
                _logger.error(
                    "dictation engine failed to initialize (%s)",
                    type(exc).__name__,
                    extra=debug_event("dictation_stream", phase="error", stage="engine_init"),
                )
                with contextlib.suppress(RuntimeError):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "dictation engine unavailable",
                                "code": "third_party_unavailable" if third else "unavailable",
                            }
                        )
                    )
                    await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
                return
            # Release the take on every exit — normal stop, abrupt browser
            # disconnect, or a crash mid-send. For the in-process engines
            # close() just frees the recognizer stream, so a best-effort
            # close on the way out is enough.
            try:
                await websocket.send_text(
                    json.dumps({"type": "ready", **({"whole_take": True} if third else {})})
                )
                await _pump_dictation(websocket, handle)
            finally:
                _logger.info(
                    "dictation stream disconnected",
                    extra=debug_event("dictation_stream", phase="disconnected"),
                )
                # An abrupt disconnect tears the ASGI task down via
                # cancellation, which would cancel this close mid-await and
                # leak the take (the remote engine holds a worker slot until
                # close). Shield it so cleanup always completes.
                with anyio.CancelScope(shield=True):
                    try:
                        await asyncio.to_thread(handle.close)
                    except Exception:  # noqa: BLE001 - preserve resource cleanup during teardown
                        # During teardown the loop's thread-pool executor may
                        # already be shutting down, so offloading raises rather
                        # than running close() — which would leak the take.
                        # close() is a quick, non-blocking free for every
                        # engine, so fall back to a direct call on the loop.
                        with contextlib.suppress(Exception):
                            handle.close()
        finally:
            wait_closed = getattr(handle, "wait_closed", None)
            if wait_closed is None:
                take_slots.release()
            else:
                # Socket cleanup stays bounded; a blocked transport retains its slot.
                task = asyncio.create_task(release_after_worker(wait_closed, take_slots))
                closing_tasks.add(task)
                task.add_done_callback(closing_tasks.discard)

    return router


async def _pump_dictation(websocket: WebSocket, handle: DictationStreamHandle) -> None:
    """Shuttle audio in and transcript events out until stop/disconnect.

    :param websocket: The accepted browser-facing WebSocket.
    :param handle: The per-connection recognizer stream.
    """
    last_partial_sent = ""
    last_partial_at = 0.0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

            data = message.get("bytes")
            if data is not None:
                update = await asyncio.to_thread(handle.feed_pcm16, data)
                if update.finalized:
                    await websocket.send_text(json.dumps(_update_event("final", update)))
                    last_partial_sent = ""
                    last_partial_at = 0.0
                now = time.monotonic()
                if (
                    update.partial != last_partial_sent
                    and now - last_partial_at >= _PARTIAL_INTERVAL_S
                ):
                    await websocket.send_text(
                        json.dumps({"type": "partial", "text": update.partial})
                    )
                    last_partial_sent = update.partial
                    last_partial_at = now
                continue

            text_frame = message.get("text")
            if text_frame is None:
                continue
            try:
                control = json.loads(text_frame)
            except ValueError:
                continue
            if isinstance(control, dict) and control.get("type") == "stop":
                finish_update = getattr(handle, "finish_update", None)
                if finish_update is not None:
                    if getattr(handle, "whole_take", False):
                        await websocket.send_json({"type": "processing"})
                        update = await _finish_hosted(websocket, handle, finish_update)
                        if update is None:
                            return
                    else:
                        update = await asyncio.to_thread(finish_update)
                    drain = (
                        None
                        if getattr(handle, "whole_take", False)
                        else getattr(handle, "drain_updates", None)
                    )
                    updates = [update] + (await asyncio.to_thread(drain) if drain else [])
                    for completed in updates[:-1]:
                        if completed.finalized:
                            await websocket.send_json(_update_event("final", completed))
                    event = _update_event("stopped", updates[-1])
                else:
                    event = {"type": "stopped", "text": await asyncio.to_thread(handle.finish)}
                await websocket.send_text(json.dumps(event))
                await websocket.close()
                return
            # Unknown control messages are ignored for forward compat.
    except DictationTakeError as exc:
        with contextlib.suppress(RuntimeError):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Third-party recognition failed",
                    "code": exc.code,
                    "undelivered": exc.undelivered,
                }
            )
            await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
    except WebSocketDisconnect:
        return
    except Exception:
        _logger.exception(
            "dictation stream failed",
            extra=debug_event("dictation_stream", phase="error", stage="pump"),
        )
        with contextlib.suppress(RuntimeError):
            await websocket.send_text(json.dumps({"type": "error", "message": "dictation failed"}))
            await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)


def _update_event(kind: str, update: DictationUpdate) -> dict:
    event: dict[str, Any] = {"type": kind, "text": update.finalized or ""}
    if update.finalized_raw is not None:
        event.update(raw=update.finalized_raw, cleaned=update.finalized_cleaned)
    if update.retry_ranges is not None:
        event["retry_ranges"] = update.retry_ranges
    if update.undelivered is not None:
        event["undelivered"] = update.undelivered
    return event


async def _finish_hosted(
    websocket: WebSocket,
    handle: DictationStreamHandle,
    finish_update: Callable[[], DictationUpdate],
) -> DictationUpdate | None:
    work = asyncio.create_task(asyncio.to_thread(finish_update))
    receive = asyncio.create_task(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait((work, receive), return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            message = receive.result()
            if message["type"] == "websocket.disconnect":
                await asyncio.to_thread(handle.close)
                await work
                return None
            receive = asyncio.create_task(websocket.receive())
    finally:
        receive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receive
        if not work.done():
            await asyncio.to_thread(handle.close)
            await work
