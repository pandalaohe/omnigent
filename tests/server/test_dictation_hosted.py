"""Hosted audio uses the production segmenter, SDK and bounded worker with fake HTTP."""

import json
import threading
import time
from array import array

import httpx
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.server import dictation_hosted as hosted
from omnigent.server import model_connections
from omnigent.server.model_connections import ResolvedConnection
from omnigent.server.routes.dictation import create_dictation_router
from omnigent.server.voice_input import VoiceInputSettings
from tests._helpers.polling import poll_until

SPEECH = array("h", [2000, -2000] * 160).tobytes()
SILENCE = bytes(640)


def test_saved_row_follows_current_model_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(model_connections, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "fake-key")
    model_connections.upsert_connection(
        "example",
        model_connections.ConnectionWrite(
            base_url="https://provider.example/v1",
            models={"row": "current-model"},
            key=model_connections.KeyEnvironment(env="EXAMPLE_PROVIDER_KEY"),
        ),
    )
    settings = VoiceInputSettings(
        speech_connection="example", speech_row="row", speech_model="old"
    )
    assert hosted.engine_for_settings(settings).model == "current-model"
    settings.speech_row = "missing"
    assert hosted.engine_for_settings(settings).model == "old"


@pytest.mark.parametrize(
    "body,text,cleaned",
    [
        ({"raw": "a", "text": "A.", "complete": True}, "A.", True),
        ({"raw": "a", "text": "A.", "complete": False}, "a", False),
        ({"raw": "a", "text": "A."}, "a", False),
        ({"raw": "a", "text": "A.", "complete": True, "extra": 1}, "a", False),
    ],
)
def test_one_step_contract(body, text, cleaned):
    result = hosted.parse_one_step("```json\n" + json.dumps(body) + "\n```")
    assert (result.text, result.cleaned) == (text, cleaned)


@pytest.mark.parametrize("text", ["plain prose", '{"text":"A.","complete":true}', '{"raw":""}'])
def test_one_step_unusable_is_failure(text):
    with pytest.raises(ValueError):
        hosted.parse_one_step(text)


def stream(monkeypatch, handler, **kwargs):
    original = openai.OpenAI

    def create(**options):
        options.pop("http_client").close()
        return original(
            **options,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=False
            ),
        )

    monkeypatch.setattr(hosted.openai, "OpenAI", create)
    return hosted.HostedTranscriptionEngine(
        ResolvedConnection("https://provider.example/v1", "fake-key"), "test-model", **kwargs
    ).create_stream()


def test_pause_segmentation_and_tail_use_wav_prompt(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"RIFF" in request.content
        assert b"preferred-term" in request.content
        return httpx.Response(200, json={"text": "recognized"})

    take = stream(monkeypatch, handle, dictionary="preferred-term")
    try:
        take.feed_pcm16(SILENCE * 5 + SPEECH * 20 + SILENCE * 30)
        take.feed_pcm16(SPEECH * 20)
        result = take.finish_update()
        assert result.finalized == "recognized recognized"
        assert result.undelivered == {"segments": 0, "seconds": 0.0}
        assert len(calls) == 2
    finally:
        take.close()
        take.close()
    assert not take.worker.is_alive()


@pytest.mark.parametrize("frames", [0, 5, 14])
def test_stop_preserves_short_tail_and_partial_frame(monkeypatch, frames):
    calls = []

    def handle(request):
        calls.append(request.content)
        return httpx.Response(200, json={"text": "last word"})

    take = stream(monkeypatch, handle)
    tail = SPEECH * frames + SPEECH[:320]
    try:
        take.feed_pcm16(tail)
        result = take.finish_update()
        assert result.finalized == "last word"
        assert len(calls) == 1
        assert tail in calls[0]
        assert result.undelivered == {"segments": 0, "seconds": 0.0}
    finally:
        take.close()


def test_silence_skips_requests_and_long_speech_is_cut(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"text": "recognized"})

    take = stream(monkeypatch, handle)
    try:
        take.feed_pcm16(SILENCE * 250)
        assert take.finish_update().finalized is None
        assert not calls
    finally:
        take.close()
    # Use the same SDK patch only once; the engine creates a fresh client.
    take = hosted.HostedTranscriptionEngine(
        ResolvedConnection("https://provider.example/v1", "fake-key"), "test-model"
    ).create_stream()
    try:
        take.feed_pcm16(SPEECH * 2000)
        result = take.finish_update()
        assert result.finalized == "recognized recognized"
        assert len(calls) == 2
    finally:
        take.close()


def test_one_step_raw_metadata_and_fixed_output(monkeypatch):
    def handle(request):
        body = json.loads(request.content)
        assert body["messages"][1]["content"][0]["type"] == "input_audio"
        assert "exactly three fields" in body["messages"][0]["content"]
        assert "custom cleanup" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"raw":"a","text":"A.","complete":true}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    take = stream(monkeypatch, handle, method="chat_audio", cleanup_instructions="custom cleanup")
    try:
        take.feed_pcm16(SPEECH * 20)
        result = take.finish_update()
        assert (result.finalized, result.finalized_raw, result.finalized_cleaned) == (
            "A.",
            "a",
            True,
        )
    finally:
        take.close()


def hosted_app(take):
    class Engine:
        def create_stream(self):
            return take

    app = FastAPI()
    app.include_router(
        create_dictation_router(
            settings_loader=lambda owner: VoiceInputSettings(speech_source="third"),
            hosted_factory=lambda settings: Engine(),
        ),
        prefix="/v1",
    )
    return app


@pytest.mark.parametrize("complete_active", [True, False])
def test_router_overload_drains_active_or_counts_it_once(monkeypatch, complete_active):
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_STOP_S", ".2")
    entered = threading.Event()
    release = threading.Event()

    def handle(request):
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"text": "delivered"})

    take = stream(monkeypatch, handle)
    try:
        with TestClient(hosted_app(take)) as client:
            with client.websocket_connect("/v1/dictation/stream") as socket:
                assert socket.receive_json()["type"] == "ready"
                socket.send_bytes(SPEECH * 20 + SILENCE * 30)
                assert entered.wait(timeout=1)
                socket.send_bytes((SPEECH * 20 + SILENCE * 30) * 3)
                with take.condition:
                    assert take.condition.wait_for(lambda: take.fatal is not None, timeout=1)
                if complete_active:
                    release.set()
                    result = socket.receive_json()
                    assert (result["type"], result["text"]) == ("final", "delivered")
                    socket.send_bytes(b"")
                result = socket.receive_json()
                assert result["type"] == "error"
                assert result["code"] == "third_party_overloaded"
                lost = 3 if complete_active else 4
                assert result["undelivered"] == {"segments": lost, "seconds": float(lost)}
                # A second flush must not count the timed-out active request twice.
                assert take.finish_update().undelivered == result["undelivered"]
                release.set()
                take.wait_closed()
    finally:
        release.set()
        take.close()
        take.wait_closed()


def test_disconnect_retains_capacity_until_blocked_transport_exits(monkeypatch):
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_MAX_STREAMS", "1")
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_STOP_S", ".1")
    entered = threading.Event()
    release = threading.Event()
    close_returned = threading.Event()

    def handle(request):
        entered.set()
        release.wait(timeout=10)
        return httpx.Response(200, json={"text": "late"})

    take = stream(monkeypatch, handle)
    original_close = take.close

    def close():
        original_close()
        close_returned.set()

    monkeypatch.setattr(take, "close", close)
    try:
        with TestClient(hosted_app(take)) as client:
            with client.websocket_connect("/v1/dictation/stream") as socket:
                assert socket.receive_json()["type"] == "ready"
                socket.send_bytes(SPEECH * 20 + SILENCE * 30)
                assert entered.wait(timeout=1)
            assert close_returned.wait(timeout=1)
            take.worker.join(timeout=2.1)
            assert take.worker.is_alive()
            with client.websocket_connect("/v1/dictation/stream") as blocked:
                with pytest.raises(WebSocketDisconnect) as raised:
                    blocked.receive_json()
                assert raised.value.code == 1013
            release.set()
            take.wait_closed()

            def admitted():
                with client.websocket_connect("/v1/dictation/stream") as retry:
                    try:
                        return retry.receive_json()["type"] == "ready"
                    except WebSocketDisconnect as exc:
                        assert exc.code == 1013
                        return False

            poll_until(admitted, timeout_s=2, interval_s=0.01)
    finally:
        release.set()
        take.close()
        take.wait_closed()


def test_stop_deadline_counts_inflight(monkeypatch):
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_STOP_S", ".1")
    release = threading.Event()

    def handle(request):
        release.wait(timeout=2)
        return httpx.Response(200, json={"text": "too late"})

    take = stream(monkeypatch, handle)
    try:
        take.feed_pcm16(SPEECH * 20)
        started = time.monotonic()
        result = take.finish_update()
        assert time.monotonic() - started < 1
        assert result.finalized is None
        assert result.undelivered["segments"] == 1
    finally:
        release.set()
        take.close()
