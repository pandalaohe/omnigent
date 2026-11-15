"""Bounded pause-segmented dictation through an OpenAI-compatible endpoint."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import threading
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass

import httpx
import openai

from omnigent.server.dictation import DictationTakeError, DictationUpdate
from omnigent.server.model_connections import ResolvedConnection, resolve_connection
from omnigent.server.voice_input import CLEANUP_RULES, TRANSCRIPTION_RULES, VoiceInputSettings

_ONE_STEP_OUTPUT = (
    "Return JSON with exactly three fields: raw (string, the verbatim transcript), "
    "text (string, the edited transcript), complete (boolean). Set complete to true "
    "only when the whole segment was edited; otherwise set complete to false and "
    "copy raw into text. If uncertain about a wording, keep it unchanged."
)


@dataclass(frozen=True)
class OneStepOutcome:
    text: str
    raw: str | None = None
    cleaned: bool = False


def parse_one_step(content: str) -> OneStepOutcome:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(text)
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("raw"), str)
        or not parsed["raw"].strip()
    ):
        raise ValueError("No usable transcript")
    raw = parsed["raw"]
    if (
        set(parsed) != {"raw", "text", "complete"}
        or parsed.get("complete") is not True
        or not isinstance(parsed.get("text"), str)
        or not parsed["text"].strip()
    ):
        return OneStepOutcome(raw)
    return OneStepOutcome(parsed["text"], raw, True)


class HostedTranscriptionEngine:
    def __init__(
        self,
        connection: ResolvedConnection,
        model: str,
        method: str = "transcription",
        instructions: str = "",
        dictionary: str = "",
        cleanup_instructions: str = "",
    ):
        if method == "chat_audio" and connection.wire_api != "chat":
            raise ValueError("Audio chat requires a chat provider")
        if not model.strip():
            raise ValueError("A transcription model is required")
        self.connection = connection
        self.model = model
        self.method = method
        self.instructions = instructions or TRANSCRIPTION_RULES
        self.dictionary = dictionary
        self.cleanup_instructions = cleanup_instructions or CLEANUP_RULES

    def create_stream(self) -> _HostedStream:
        return _HostedStream(self)


def engine_for_settings(settings: VoiceInputSettings) -> HostedTranscriptionEngine:
    return HostedTranscriptionEngine(
        resolve_connection(settings.speech_connection),
        settings.speech_model,
        settings.speech_method,
        settings.speech_instructions,
        settings.words,
        settings.instructions,
    )


class _HostedStream:
    def __init__(self, engine: HostedTranscriptionEngine):
        self.engine = engine
        self.timeout = max(
            0.1, float(os.environ.get("OMNIGENT_DICTATION_HOSTED_REQUEST_TIMEOUT_S", "8"))
        )
        self.stop_timeout = min(
            12.0, max(0.1, float(os.environ.get("OMNIGENT_DICTATION_HOSTED_STOP_S", "12")))
        )
        self.queue_size = max(1, int(os.environ.get("OMNIGENT_DICTATION_HOSTED_QUEUE", "2")))
        self.pause_frames = max(
            1, int(os.environ.get("OMNIGENT_DICTATION_HOSTED_PAUSE_MS", "600")) // 20
        )
        self.max_frames = max(
            15, int(float(os.environ.get("OMNIGENT_DICTATION_HOSTED_MAX_SEGMENT_S", "25")) * 50)
        )
        self.client = openai.OpenAI(
            base_url=engine.connection.base_url,
            api_key=engine.connection.api_key,
            max_retries=0,
            http_client=httpx.Client(follow_redirects=False, timeout=self.timeout),
        )
        self.condition = threading.Condition()
        self.queue: deque[bytes] = deque()
        self.results: deque[OneStepOutcome] = deque()
        self.noise: deque[float] = deque([100.0], maxlen=75)
        self.pending = bytearray()
        self.audio = bytearray()
        self.voiced = 0
        self.silent = 0
        self.inflight = 0
        self.lost_segments = 0
        self.lost_seconds = 0.0
        self.fatal: str | None = None
        self.cancelled = False
        self.closed = False
        self.stopping = False
        self.deadline: float | None = None
        self.worker = threading.Thread(target=self._run, daemon=True, name="hosted-dictation")
        self.worker.start()

    def _lost(self, audio: bytes) -> None:
        self.lost_segments += 1
        self.lost_seconds += len(audio) / 32000

    def _segment(self) -> None:
        if self.voiced >= 15:
            data = bytes(self.audio)
            if len(self.queue) >= self.queue_size:
                self._lost(data)
                self.fatal = "third_party_overloaded"
                while self.queue:
                    self._lost(self.queue.popleft())
            else:
                self.queue.append(data)
                self.condition.notify_all()
        self.audio.clear()
        self.voiced = self.silent = 0

    def _update(self, *, all_results: bool = False) -> DictationUpdate:
        values: list[OneStepOutcome] = []
        while self.results and (
            not values or all_results or self.results[0].cleaned == values[0].cleaned
        ):
            values.append(self.results.popleft())
        cleaned = bool(values) and all(value.cleaned for value in values)
        return DictationUpdate(
            partial="",
            finalized=" ".join(value.text for value in values) or None,
            finalized_raw=" ".join(value.raw or value.text for value in values)
            if cleaned
            else None,
            finalized_cleaned=cleaned,
            undelivered={"segments": self.lost_segments, "seconds": round(self.lost_seconds, 2)},
        )

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        with self.condition:
            if self.fatal or self.cancelled:
                if self.results:
                    return self._update()
                raise DictationTakeError(self.fatal or "third_party_failed", self._undelivered())
            self.pending.extend(data)
            while len(self.pending) >= 640:
                frame = bytes(self.pending[:640])
                del self.pending[:640]
                samples = array("h", frame)
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                voiced = rms >= max(300, 3 * min(self.noise))
                if not voiced:
                    self.noise.append(rms)
                # Keep a quiet floor available when a take begins in speech.
                if not self.audio and rms >= 300:
                    voiced = True
                if voiced or self.audio:
                    self.audio.extend(frame)
                    if voiced:
                        self.voiced += 1
                        self.silent = 0
                    else:
                        self.silent += 1
                    if (
                        self.silent >= self.pause_frames
                        or len(self.audio) // 640 >= self.max_frames
                    ):
                        self._segment()
                        if self.fatal:
                            break
            return self._update()

    def _undelivered(self) -> dict[str, int | float]:
        return {"segments": self.lost_segments, "seconds": round(self.lost_seconds, 2)}

    def _call(self, pcm: bytes) -> OneStepOutcome:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        engine = self.engine
        timeout = (
            min(self.timeout, max(0.01, self.deadline - time.monotonic()))
            if self.deadline
            else self.timeout
        )
        if engine.method == "transcription":
            response = self.client.audio.transcriptions.create(
                model=engine.model,
                file=("segment.wav", buffer.getvalue(), "audio/wav"),
                prompt=(engine.instructions + "\nWord list:\n" + engine.dictionary)[:800],
                response_format="json",
                timeout=timeout,
            )
            if not response.text.strip():
                raise ValueError("Empty transcription")
            return OneStepOutcome(response.text)
        response = self.client.chat.completions.create(
            model=engine.model,
            temperature=0,
            max_tokens=2048,
            timeout=timeout,
            messages=[
                {
                    "role": "system",
                    "content": engine.instructions
                    + (
                        "\nTranscribe the audio verbatim into raw, then edit that transcript "
                        "under the rules below into text.\n"
                    )
                    + engine.cleanup_instructions
                    + "\n"
                    + _ONE_STEP_OUTPUT,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(buffer.getvalue()).decode(),
                                "format": "wav",
                            },
                        },
                        {"type": "text", "text": json.dumps({"word_list": engine.dictionary})},
                    ],
                },
            ],
        )
        return parse_one_step(response.choices[0].message.content or "")

    def _run(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(
                    lambda: self.cancelled or self.queue or self.stopping or self.fatal
                )
                if self.cancelled or not self.queue:
                    return
                pcm = self.queue.popleft()
                self.inflight = len(pcm)
            try:
                result = None
                for attempt in range(2):
                    try:
                        result = self._call(pcm)
                        break
                    except (openai.APIConnectionError, openai.APIStatusError) as exc:
                        retryable = (
                            isinstance(exc, openai.APIConnectionError)
                            or exc.status_code == 429
                            or exc.status_code >= 500
                        )
                        if (
                            attempt
                            or not retryable
                            or (
                                self.deadline
                                and self.deadline - time.monotonic() < self.timeout + 0.5
                            )
                        ):
                            raise
                        with self.condition:
                            self.condition.wait(timeout=0.5)
                            if self.cancelled:
                                return
                with self.condition:
                    if not self.cancelled and result is not None:
                        self.results.append(result)
            except Exception:  # noqa: BLE001 - redact provider and credential failures
                with self.condition:
                    if not self.cancelled:
                        self._lost(pcm)
                        self.fatal = self.fatal or "third_party_failed"
                        while self.queue:
                            self._lost(self.queue.popleft())
                        if self.voiced >= 15:
                            self._lost(bytes(self.audio))
                        self.audio.clear()
            finally:
                with self.condition:
                    self.inflight = 0
                    self.condition.notify_all()

    def finish_update(self) -> DictationUpdate:
        with self.condition:
            self.deadline = time.monotonic() + self.stop_timeout
            if not self.fatal:
                self._segment()
            self.stopping = True
            self.condition.notify_all()
            self.condition.wait_for(
                lambda: not self.queue and not self.inflight,
                timeout=max(0, self.deadline - time.monotonic()),
            )
            if self.queue or self.inflight:
                for pcm in self.queue:
                    self._lost(pcm)
                self.queue.clear()
                if self.inflight:
                    self.lost_segments += 1
                    self.lost_seconds += self.inflight / 32000
                self.cancelled = True
                self.condition.notify_all()
            return self._update()

    def drain_updates(self) -> list[DictationUpdate]:
        with self.condition:
            updates = []
            while self.results:
                updates.append(self._update())
            return updates

    def finish(self) -> str:
        return self.finish_update().finalized or ""

    def close(self) -> None:
        with self.condition:
            if self.closed:
                return
            self.closed = self.cancelled = True
            self.queue.clear()
            self.condition.notify_all()
        try:
            self.client.close()
        finally:
            self.worker.join(timeout=2)
