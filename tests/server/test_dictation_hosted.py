"""Hosted audio uses the production take buffer, SDK and bounded worker with fake HTTP."""

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


def test_whole_take_keeps_quiet_start_pauses_and_tail(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"preferred-term" in request.content
        return httpx.Response(200, json={"text": "recognized"})

    take = stream(monkeypatch, handle, dictionary="preferred-term")
    quiet = array("h", [50, -50] * 160).tobytes()
    audio = quiet * 5 + SPEECH * 20 + SILENCE * 30 + SPEECH * 20 + SPEECH[:320]
    try:
        take.feed_pcm16(audio)
        result = take.finish_update()
        assert result.finalized == "recognized"
        assert result.undelivered == {"segments": 0, "seconds": 0.0}
        assert len(calls) == 1
        assert audio in calls[0].content
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


def test_silence_is_sent_without_a_volume_gate(monkeypatch):
    calls = []
    take = stream(
        monkeypatch,
        lambda request: (
            calls.append(request.content),
            httpx.Response(200, json={"text": "quiet words"}),
        )[1],
    )
    try:
        take.feed_pcm16(SILENCE * 250)
        assert not calls
        assert take.finish_update().finalized == "quiet words"
        assert SILENCE * 250 in calls[0]
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


def test_slow_provider_gets_whole_sixty_second_take_and_redacted_diagnostics(monkeypatch, caplog):
    calls = []

    def handle(request):
        calls.append(request.content)
        time.sleep(5)
        return httpx.Response(200, json={"text": "private transcript"})

    take = stream(monkeypatch, handle)
    audio = SILENCE * 100 + SPEECH * 2800 + SILENCE * 100
    try:
        take.feed_pcm16(audio)
        assert not calls
        with caplog.at_level("INFO", logger=hosted.__name__):
            result = take.finish_update()
        assert result.finalized == "private transcript"
        assert result.undelivered == {"segments": 0, "seconds": 0.0}
        assert len(calls) == 1 and audio in calls[0]
        records = [
            r.getMessage() for r in caplog.records if "hosted dictation take" in r.getMessage()
        ]
        assert len(records) == 1
        assert "private transcript" not in records[0] and "RIFF" not in records[0]
        diagnostic = json.loads(records[0].split("hosted dictation take ", 1)[1])
        assert diagnostic["take_seconds"] == 60
        assert diagnostic["transcript_chars"] == 18
        assert diagnostic["chunks_sent"] == 1
        assert diagnostic["requests"][0]["latency_s"] >= 5
        assert diagnostic["rms_p10_p50_p90"] == [2000, 2000, 2000]
    finally:
        take.close()


@pytest.mark.parametrize("fail_middle", [False, True])
def test_over_limit_uses_fewest_quiet_cuts_and_keeps_other_chunks(monkeypatch, fail_middle):
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_MAX_AUDIO_BYTES", str(640 * 50 + 44))
    calls = []
    payloads = []
    import io
    import wave

    def handle(request):
        body = request.content
        wav_start = body.index(b"RIFF")
        length = int.from_bytes(body[wav_start + 4 : wav_start + 8], "little") + 8
        with wave.open(io.BytesIO(body[wav_start : wav_start + length])) as wav:
            payloads.append(wav.readframes(wav.getnframes()))
        calls.append(request)
        # The middle chunk fails three times, then the final chunk still succeeds.
        if fail_middle and len(calls) in (2, 3, 4):
            return httpx.Response(503, json={"error": {"message": "redacted"}})
        return httpx.Response(200, json={"text": str(len(calls))})

    take = stream(monkeypatch, handle)
    audio = SPEECH * 39 + SILENCE + SPEECH * 59 + SILENCE + SPEECH * 20
    try:
        take.feed_pcm16(audio)
        assert take.timeout >= 60
        result = take.finish_update()
        unique = [payloads[0], payloads[1], payloads[-1]]
        assert b"".join(unique) == audio
        assert len(unique[0]) == 640 * 40
        assert all(len(p) <= 640 * 50 for p in payloads)
        assert result.finalized == ("1 5" if fail_middle else "1 2 3")
        assert result.retry_ranges == ([(640 * 40, 640 * 90)] if fail_middle else None)
        assert result.undelivered == {
            "segments": int(fail_middle),
            "seconds": 1.0 if fail_middle else 0.0,
        }
    finally:
        take.close()


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
                socket.send_json({"type": "stop"})
                assert socket.receive_json()["type"] == "processing"
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


def test_processing_waits_until_user_cancels(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    def handle(request):
        entered.set()
        release.wait(timeout=5)
        return httpx.Response(200, json={"text": "too late"})

    take = stream(monkeypatch, handle)

    def finish():
        take.finish_update()
        finished.set()

    worker = threading.Thread(target=finish)
    try:
        take.feed_pcm16(SPEECH * 20)
        worker.start()
        assert entered.wait(timeout=1)
        assert not finished.is_set()
        take.close()
        assert finished.wait(timeout=1)
    finally:
        release.set()
        take.close()
        take.wait_closed()
        worker.join(timeout=1)


@pytest.mark.parametrize("failure", ["timeout", 429, 503])
def test_stop_retries_transient_failure_with_backoff(monkeypatch, failure):
    monkeypatch.setenv("OMNIGENT_DICTATION_HOSTED_STOP_S", "1")
    calls = []

    def handle(request):
        calls.append(request.content)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("slow provider", request=request)
            return httpx.Response(failure, json={"error": {"message": "try again"}})
        return httpx.Response(200, json={"text": "last words"})

    take = stream(monkeypatch, handle)
    try:
        take.feed_pcm16(SPEECH * 20)
        result = take.finish_update()
        assert result.finalized == "last words"
        assert result.undelivered == {"segments": 0, "seconds": 0.0}
        assert len(calls) == 2
        assert all(SPEECH * 20 in body for body in calls)
    finally:
        take.close()
