"""Hosted audio uses the production segmenter, SDK and bounded worker with fake HTTP."""

import json
import threading
import time
from array import array

import httpx
import openai
import pytest

from omnigent.server import dictation_hosted as hosted
from omnigent.server.dictation import DictationTakeError
from omnigent.server.model_connections import ResolvedConnection

SPEECH = array("h", [2000, -2000] * 160).tobytes()
SILENCE = bytes(640)


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


def test_bounded_queue_reports_lost_audio(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def handle(request):
        entered.set()
        release.wait(timeout=2)
        return httpx.Response(200, json={"text": "delivered"})

    take = stream(monkeypatch, handle)
    try:
        take.feed_pcm16(SPEECH * 20 + SILENCE * 30)
        assert entered.wait(timeout=1)
        take.feed_pcm16((SPEECH * 20 + SILENCE * 30) * 3)
        with pytest.raises(DictationTakeError) as raised:
            take.feed_pcm16(b"")
        assert raised.value.code == "third_party_overloaded"
        assert raised.value.undelivered["segments"] == 3
        release.set()
        assert take.finish_update().finalized == "delivered"
    finally:
        release.set()
        take.close()


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
