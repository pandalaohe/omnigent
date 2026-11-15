"""Voice cleanup sends only the segment and the caller's vocabulary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes import voice_input
from omnigent.server.voice_input import VoiceInputSettings
from omnigent.stores.user_settings_store import UserSettingsStore


def test_gateway_alias_uses_configured_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runtime import get_caps
    from omnigent.spec.types import LLMConfig

    runtime_caps = get_caps()
    monkeypatch.setattr(
        runtime_caps,
        "llm",
        LLMConfig(
            model="openai/test-default", connection={"base_url": "https://provider.example/v1"}
        ),
    )
    model, connection = voice_input._configured_connection("gateway/test-fast")
    assert model == "openai/gateway/test-fast"
    assert connection == {"base_url": "https://provider.example/v1"}
    with pytest.raises(ValueError, match="configured LLM provider"):
        voice_input._configured_connection("anthropic/test-other")


async def test_default_six_second_timeout_is_forwarded(
    voice_store: UserSettingsStore, llm: SimpleNamespace
) -> None:
    voice_store.save_voice_input(
        VoiceInputSettings(model="openai/test-cleanup").model_dump(), user_id=None
    )
    llm.responses.create.side_effect = TimeoutError()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/cleanup", json={"text": "keep this raw segment"}
        )
    assert response.status_code == 504
    assert llm.responses.create.call_args.kwargs["timeout"] == 6


@pytest.fixture()
def voice_store(db_uri: str) -> UserSettingsStore:
    return UserSettingsStore(db_uri)


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    response = SimpleNamespace(
        output=[SimpleNamespace(content=[SimpleNamespace(text="Do not deploy at 12:30.")])]
    )
    create = AsyncMock(return_value=response)
    monkeypatch.setattr(
        voice_input,
        "_configured_connection",
        lambda model: (model, {"base_url": "https://provider.example/v1"}),
    )
    return SimpleNamespace(responses=SimpleNamespace(create=create))


def app_for(store: UserSettingsStore, llm: SimpleNamespace, *, auth: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(
        voice_input.create_voice_input_router(
            store,
            UnifiedAuthProvider(source="header", local_single_user=False) if auth else None,
            llm_client=llm,
        ),
        prefix="/v1",
    )
    return app


async def test_only_segment_and_saved_list_are_sent(
    voice_store: UserSettingsStore, llm: SimpleNamespace
) -> None:
    voice_store.save_voice_input(
        VoiceInputSettings(
            words="ExampleApp may be heard as example app",
            model="openai/test-cleanup",
            reasoning={"effort": "none"},
            thinking={"type": "disabled"},
            timeout=3,
            reasoning_effort="low",
        ).model_dump(),
        user_id=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/cleanup", json={"text": "don't deploy at 12:30"}
        )
    assert response.status_code == 200
    assert response.json() == {"text": "Do not deploy at 12:30."}
    call = llm.responses.create.call_args.kwargs
    assert json.loads(call["input"][0]["content"]) == {
        "segment": "don't deploy at 12:30",
        "word_list": "ExampleApp may be heard as example app",
    }
    assert len(call["input"]) == 1
    assert call["timeout"] == 3
    assert call["reasoning"] == {"effort": "none"}
    assert call["reasoning_effort"] == "low"
    assert call["thinking"] == {"type": "disabled"}
    assert call["connection_params"] == {"base_url": "https://provider.example/v1"}


@pytest.mark.parametrize("settings", [{}, {"model": "openai/test-cleanup", "mode": "off"}])
async def test_unconfigured_or_off_refuses(
    voice_store: UserSettingsStore, llm: SimpleNamespace, settings: dict
) -> None:
    voice_store.save_voice_input(VoiceInputSettings(**settings).model_dump(), user_id=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "raw"})
    assert response.status_code == 409
    llm.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "failure,status", [(TimeoutError(), 504), (RuntimeError("provider failed"), 503)]
)
async def test_failure_keeps_text_client_side(
    voice_store: UserSettingsStore, llm: SimpleNamespace, failure: Exception, status: int
) -> None:
    voice_store.save_voice_input(
        VoiceInputSettings(model="openai/test-cleanup").model_dump(), user_id=None
    )
    llm.responses.create.side_effect = failure
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "keep original"})
    assert response.status_code == status
    assert "provider failed" not in response.text


async def test_wall_timeout_cancels_the_call(
    voice_store: UserSettingsStore, llm: SimpleNamespace
) -> None:
    voice_store.save_voice_input(
        VoiceInputSettings(model="openai/test-cleanup", timeout=1).model_dump(), user_id=None
    )
    cancelled = asyncio.Event()

    async def blocked(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm.responses.create.side_effect = blocked
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "keep original"})
    assert response.status_code == 504
    assert cancelled.is_set()


async def test_settings_are_authenticated_and_user_scoped(
    voice_store: UserSettingsStore, llm: SimpleNamespace
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm, auth=True)),
        base_url="http://test",
    ) as client:
        with pytest.raises(OmnigentError, match="Authentication required"):
            await client.get("/v1/settings/voice-input")
        with pytest.raises(OmnigentError, match="Authentication required"):
            await client.post("/v1/dictation/cleanup", json={"text": "raw"})
        alice = {"X-Forwarded-Email": "alice@example.com"}
        bob = {"X-Forwarded-Email": "bob@example.com"}
        saved = await client.put(
            "/v1/settings/voice-input", headers=alice, json={"words": "personal term"}
        )
        assert saved.status_code == 200
        assert (await client.get("/v1/settings/voice-input", headers=alice)).json()[
            "words"
        ] == "personal term"
        assert (await client.get("/v1/settings/voice-input", headers=bob)).json()["words"] == ""


async def test_rejects_surrounding_draft_and_bad_settings(
    voice_store: UserSettingsStore, llm: SimpleNamespace
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        assert (
            await client.post("/v1/dictation/cleanup", json={"text": "raw", "draft": "typed"})
        ).status_code == 422
        assert (
            await client.put("/v1/settings/voice-input", json={"words": "x" * 8001})
        ).status_code == 422
        assert (
            await client.put("/v1/settings/voice-input", json={"timeout": 0})
        ).status_code == 422
    llm.responses.create.assert_not_called()
