"""Caller-scoped voice settings and bounded, segment-only LLM cleanup."""

from __future__ import annotations

import asyncio
import json
import time
import wave
from pathlib import Path
from typing import Any, Literal

import httpx
import openai
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.llms import Client
from omnigent.llms.errors import PermanentLLMError, RetryableLLMError
from omnigent.llms.summarize import extract_summary_text
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.model_connections import (
    ConnectionUnavailable,
    load_connections,
    resolve_connection,
    resolve_reference,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.server.voice_input import CLEANUP_OUTPUT_FORMAT, CLEANUP_RULES, VoiceInputSettings


class CleanupRequest(BaseModel):
    """Only a completed dictated segment, never the surrounding draft."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=12000)


class CleanupResult(BaseModel):
    """The model must explicitly attest that it edited the whole segment."""

    model_config = ConfigDict(extra="forbid", strict=True)
    complete: bool
    text: str = Field(min_length=1, max_length=24000)


class VoiceTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: Literal["speech", "cleanup"]
    connection: str = Field(min_length=1, max_length=48)
    row: str = Field(min_length=1, max_length=200)
    method: Literal["transcription", "chat_audio"] = "transcription"


def _test_error(exc: Exception, status: str) -> str:
    if isinstance(exc, (PermanentLLMError, RetryableLLMError)):
        if exc.code == "timeout":
            return "timeout"
        if exc.code == "connection_error":
            return "unreachable"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, openai.APITimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, openai.APIConnectionError)):
        return "unreachable"
    if isinstance(exc, ConnectionUnavailable):
        return "gone"
    code = (
        exc.status_code
        if isinstance(exc, openai.APIStatusError)
        else exc.response.status_code
        if isinstance(exc, httpx.HTTPStatusError)
        else None
    )
    if isinstance(exc, (PermanentLLMError, RetryableLLMError)) and exc.detail is not None:
        code = exc.detail.status_code
    if code in (401, 403):
        return "key_rejected"
    if code == 404 and status in ("gone", "not_listed"):
        return "gone"
    if code in (400, 404, 422):
        return "wrong_type"
    return "rejected"


def _configured_connection(model: str) -> tuple[str, dict[str, str] | None]:
    from omnigent.llms.routing import PROVIDER_CONFIGS, parse_model_string
    from omnigent.runtime import get_caps
    from omnigent.runtime.policies.builder import _resolve_server_llm_connection

    caps = get_caps()
    if caps.llm is None:
        raise ValueError("Server LLM is not configured")
    provider = parse_model_string(caps.llm.model).provider
    # A gateway alias may contain slashes without naming one of our adapters.
    routed_model = model if model.split("/", 1)[0] in PROVIDER_CONFIGS else f"{provider}/{model}"
    if parse_model_string(routed_model).provider != provider:
        raise ValueError("Cleanup must use the configured LLM provider")
    if caps.policy_llm_connection_factory is not None:
        return routed_model, caps.policy_llm_connection_factory()
    return routed_model, _resolve_server_llm_connection(caps.llm)


def _cleanup_target(settings: VoiceInputSettings) -> tuple[str, dict[str, str] | None]:
    _, model, _ = resolve_reference(
        settings.cleanup_connection, settings.cleanup_row, settings.model.strip()
    )
    if settings.cleanup_connection == "server-llm":
        return _configured_connection(model)
    connection = resolve_connection(settings.cleanup_connection)
    provider = "openai" if connection.wire_api == "responses" else "openai-compatible"
    return f"{provider}/{model}", {
        "base_url": connection.base_url,
        "api_key": connection.api_key,
    }


async def _run_cleanup(settings: VoiceInputSettings, text: str, llm_client: Any | None) -> str:
    model, connection = await asyncio.to_thread(_cleanup_target, settings)
    extra: dict[str, Any] = {}
    if settings.thinking:
        extra["thinking"] = settings.thinking
    reasoning = settings.reasoning
    if settings.reasoning_effort:
        reasoning = {**(reasoning or {}), "effort": settings.reasoning_effort}
    response = await (llm_client or Client()).responses.create(
        model=model,
        instructions=(settings.instructions.strip() or CLEANUP_RULES)
        + "\n"
        + CLEANUP_OUTPUT_FORMAT,
        input=[
            {
                "role": "user",
                "content": json.dumps(
                    {"segment": text, "word_list": settings.words},
                    ensure_ascii=False,
                ),
            }
        ],
        tools=[],
        connection_params=connection,
        timeout=settings.timeout,
        max_output_tokens=4096,
        reasoning=reasoning,
        **extra,
    )
    if getattr(response, "status", None) != "completed":
        raise ValueError("Cleanup response did not complete")
    result = CleanupResult.model_validate_json(extract_summary_text(response))
    if not result.complete or not result.text.strip():
        raise ValueError("Cleanup model reported an incomplete edit")
    return result.text


def create_voice_input_router(
    store: SqlAlchemyUserPreferencesStore | None,
    auth_provider: AuthProvider | None = None,
    *,
    llm_client: Any | None = None,
) -> APIRouter:
    """Use existing identity and settings storage; provider credentials stay server-side."""
    router = APIRouter()
    slots = asyncio.Semaphore(4)

    @router.post("/dictation/test")
    async def test_model(request: Request, body: VoiceTestRequest) -> dict:
        require_user(request, auth_provider)
        if slots.locked():
            raise HTTPException(429, "Voice testing is busy")
        started = time.monotonic()
        kind = "ok"
        status = "unchecked"
        async with slots:
            try:
                async with asyncio.timeout(12):
                    providers = await asyncio.to_thread(load_connections)
                    provider = next(
                        (item for item in providers if item["name"] == body.connection), None
                    )
                    row = next(
                        (
                            item
                            for item in (provider or {}).get("models", [])
                            if item["key"] == body.row
                        ),
                        None,
                    )
                    if row is None:
                        raise ConnectionUnavailable("Model unavailable")
                    status = row["status"]
                    if body.slot == "cleanup":
                        settings = VoiceInputSettings(
                            cleanup_connection=body.connection,
                            cleanup_row=body.row,
                            model=row["model_id"],
                            timeout=10,
                        )
                        await _run_cleanup(
                            settings, "please check this short sentence", llm_client
                        )
                    else:
                        from omnigent.server.dictation_hosted import (
                            HostedTranscriptionEngine,
                            transcribe_once,
                        )

                        connection = await asyncio.to_thread(resolve_connection, body.connection)
                        engine = HostedTranscriptionEngine(
                            connection, row["model_id"], body.method
                        )
                        with wave.open(
                            str(Path(__file__).parents[1] / "assets" / "voice-test.wav"), "rb"
                        ) as audio:
                            pcm = audio.readframes(audio.getnframes())
                        await asyncio.to_thread(transcribe_once, engine, pcm)
            except Exception as exc:  # noqa: BLE001 - only static messages leave this boundary
                kind = _test_error(exc, status)
        purpose = "transcribe audio" if body.slot == "speech" else "clean up dictated text"
        messages = {
            "ok": f"This model can {purpose}.",
            "wrong_type": f"This model cannot {purpose}. Try a different model type.",
            "key_rejected": "The provider rejected its key. Ask an administrator to check it.",
            "unreachable": "The provider could not be reached. Check the endpoint.",
            "gone": "The selected model is unavailable. Pick it again or ask an administrator.",
            "timeout": "The test timed out. Try again or choose another model.",
            "rejected": f"The provider did not return a usable result to {purpose}.",
        }
        return {
            "ok": kind == "ok",
            "kind": kind,
            "text": messages[kind],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    @router.post("/dictation/cleanup")
    async def cleanup(request: Request, body: CleanupRequest) -> dict[str, str]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if store is None:
            raise HTTPException(503, "Voice preferences unavailable")
        envelope = await asyncio.to_thread(store.get, owner)
        settings = VoiceInputSettings.model_validate(
            (envelope or {}).get("settings", {}).get("voice_input", {})
        )
        if not settings.model.strip() or settings.mode == "off":
            raise HTTPException(409, "Voice cleanup is off")
        if slots.locked():
            raise HTTPException(429, "Voice cleanup is busy")
        async with slots:
            try:
                async with asyncio.timeout(settings.timeout):
                    text = await _run_cleanup(settings, body.text, llm_client)
            except ConnectionUnavailable as exc:
                raise HTTPException(409, "Selected cleanup provider unavailable") from exc
            except TimeoutError as exc:
                raise HTTPException(504, "Voice cleanup timed out") from exc
            except Exception as exc:
                raise HTTPException(503, "Voice cleanup unavailable") from exc
        return {"text": text}

    return router
