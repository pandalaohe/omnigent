"""Caller-scoped voice settings and bounded, segment-only LLM cleanup."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.llms import Client
from omnigent.llms.summarize import extract_summary_text
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.voice_input import CLEANUP_INSTRUCTIONS, VoiceInputSettings
from omnigent.stores.user_settings_store import UserSettingsStore


class CleanupRequest(BaseModel):
    """Only a completed dictated segment, never the surrounding draft."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=12000)


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


def create_voice_input_router(
    store: UserSettingsStore,
    auth_provider: AuthProvider | None = None,
    *,
    llm_client: Any | None = None,
) -> APIRouter:
    """Use existing identity and settings storage; provider credentials stay server-side."""
    router = APIRouter()
    slots = asyncio.Semaphore(4)

    @router.get("/settings/voice-input", response_model=VoiceInputSettings)
    async def read_settings(request: Request) -> VoiceInputSettings:
        owner = require_user(request, auth_provider)
        value = await asyncio.to_thread(store.get_voice_input, user_id=owner)
        return VoiceInputSettings.model_validate(value)

    @router.put("/settings/voice-input", response_model=VoiceInputSettings)
    async def save_settings(request: Request, body: VoiceInputSettings) -> VoiceInputSettings:
        owner = require_user(request, auth_provider)
        try:
            await asyncio.to_thread(store.save_voice_input, body.model_dump(), user_id=owner)
        except ValueError as exc:
            raise HTTPException(422, "Voice input settings are too large") from exc
        return body

    @router.post("/dictation/cleanup")
    async def cleanup(request: Request, body: CleanupRequest) -> dict[str, str]:
        owner = require_user(request, auth_provider)
        settings = VoiceInputSettings.model_validate(
            await asyncio.to_thread(store.get_voice_input, user_id=owner)
        )
        if not settings.model.strip() or settings.mode == "off":
            raise HTTPException(409, "Voice cleanup is off")
        if slots.locked():
            raise HTTPException(429, "Voice cleanup is busy")
        async with slots:
            try:
                async with asyncio.timeout(settings.timeout):
                    model, connection = await asyncio.to_thread(
                        _configured_connection, settings.model.strip()
                    )
                    extra: dict[str, Any] = {}
                    if settings.thinking:
                        extra["thinking"] = settings.thinking
                    if settings.reasoning_effort:
                        extra["reasoning_effort"] = settings.reasoning_effort
                    response = await (llm_client or Client()).responses.create(
                        model=model,
                        instructions=CLEANUP_INSTRUCTIONS,
                        input=[
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"segment": body.text, "word_list": settings.words},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        tools=[],
                        connection_params=connection,
                        timeout=settings.timeout,
                        max_output_tokens=4096,
                        reasoning=settings.reasoning,
                        **extra,
                    )
                    text = extract_summary_text(response).strip()
                    if not text or len(text) > 24000:
                        raise ValueError("Invalid cleanup output")
            except TimeoutError as exc:
                raise HTTPException(504, "Voice cleanup timed out") from exc
            except Exception as exc:
                raise HTTPException(503, "Voice cleanup unavailable") from exc
        return {"text": text}

    return router
