"""Caller-scoped voice settings and bounded, segment-only LLM cleanup."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.llms import Client
from omnigent.llms.summarize import extract_summary_text
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
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
    store: SqlAlchemyUserPreferencesStore | None,
    auth_provider: AuthProvider | None = None,
    *,
    llm_client: Any | None = None,
) -> APIRouter:
    """Use existing identity and settings storage; provider credentials stay server-side."""
    router = APIRouter()
    slots = asyncio.Semaphore(4)

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
                    model, connection = await asyncio.to_thread(
                        _configured_connection, settings.model.strip()
                    )
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
                                    {"segment": body.text, "word_list": settings.words},
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
                    text = result.text
            except TimeoutError as exc:
                raise HTTPException(504, "Voice cleanup timed out") from exc
            except Exception as exc:
                raise HTTPException(503, "Voice cleanup unavailable") from exc
        return {"text": text}

    return router
