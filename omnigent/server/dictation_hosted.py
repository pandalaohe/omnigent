"""Whole-take dictation through an OpenAI-compatible endpoint."""

from __future__ import annotations

import base64
import io
import json
import logging
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

from omnigent.server.dictation import DictationUpdate
from omnigent.server.model_connections import (
    ResolvedConnection,
    resolve_connection,
    resolve_reference,
)
from omnigent.server.voice_input import CLEANUP_RULES, TRANSCRIPTION_RULES, VoiceInputSettings

_logger = logging.getLogger(__name__)

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
        resolve_reference(settings.speech_connection, settings.speech_row, settings.speech_model)[
            1
        ],
        settings.speech_method,
        settings.speech_instructions,
        settings.words,
        settings.instructions,
    )


def transcribe_once(engine: HostedTranscriptionEngine, pcm: bytes) -> OneStepOutcome:
    """Run the same bounded provider request used by a live speech segment."""
    with openai.OpenAI(
        base_url=engine.connection.base_url,
        api_key=engine.connection.api_key,
        max_retries=0,
        http_client=httpx.Client(follow_redirects=False, timeout=10),
    ) as client:
        return _transcribe(engine, client, pcm, 10)


def _transcribe(
    engine: HostedTranscriptionEngine, client: openai.OpenAI, pcm: bytes, timeout: float
) -> OneStepOutcome:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    if engine.method == "transcription":
        response = client.audio.transcriptions.create(
            model=engine.model,
            file=("segment.wav", buffer.getvalue(), "audio/wav"),
            prompt=(engine.instructions + "\nWord list:\n" + engine.dictionary)[:800],
            response_format="json",
            timeout=timeout,
        )
        if not response.text.strip():
            raise ValueError("Empty transcription")
        return OneStepOutcome(response.text)
    response = client.chat.completions.create(
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


class _HostedStream:
    def __init__(self, engine: HostedTranscriptionEngine):
        self.engine = engine
        self.timeout = max(
            0.1, float(os.environ.get("OMNIGENT_DICTATION_HOSTED_REQUEST_TIMEOUT_S", "30"))
        )
        self.stop_timeout = max(
            0.1, float(os.environ.get("OMNIGENT_DICTATION_HOSTED_STOP_S", "65"))
        )
        # Leave room for the WAV header and multipart overhead below 25 MB.
        self.max_chunk_bytes = (
            max(
                640,
                min(
                    24_000_000,
                    int(os.environ.get("OMNIGENT_DICTATION_HOSTED_MAX_AUDIO_BYTES", "24000000")),
                )
                - 44,
            )
            // 640
            * 640
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
        self.pending = bytearray()
        self.audio = bytearray()
        self.rms: list[float] = []
        self.connected_at = time.monotonic()
        self.first_frame_delay: float | None = None
        self.frames_received = 0
        self.requests: list[dict[str, str | float]] = []
        self.chunks_sent = 0
        self.retries = 0
        self.transcript_chars = 0
        self.cleanup_outcome = "unknown"
        self.logged = False
        self.request_started: float | None = None
        self.inflight = 0
        self.inflight_abandoned = False
        self.lost_segments = 0
        self.lost_seconds = 0.0
        self.cancelled = False
        self.closed = False
        self.stopping = False
        self.deadline: float | None = None
        self.worker = threading.Thread(target=self._run, daemon=True, name="hosted-dictation")
        self.worker.start()

    def _lost(self, audio: bytes) -> None:
        self.lost_segments += 1
        self.lost_seconds += len(audio) / 32000

    @property
    def stop_budget(self) -> float:
        chunks = max(1, math.ceil((len(self.audio) + len(self.pending)) / self.max_chunk_bytes))
        return self.stop_timeout * chunks

    def _segment(self) -> None:
        data = bytes(self.audio) + bytes(self.pending[: len(self.pending) // 2 * 2])
        self.audio.clear()
        self.pending.clear()
        start = 0
        remaining = math.ceil(len(data) / self.max_chunk_bytes)
        while remaining > 1:
            # Feasible cuts keep the fewest chunks; choose the quietest frame.
            first = max(start + 640, len(data) - (remaining - 1) * self.max_chunk_bytes)
            first = (first + 639) // 640
            last = (start + self.max_chunk_bytes) // 640
            cut = min(range(first, last + 1), key=lambda i: (self.rms[i - 1], -i)) * 640
            self.queue.append(data[start:cut])
            start = cut
            remaining -= 1
        if start < len(data):
            self.queue.append(data[start:])

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
            if self.cancelled or self.stopping:
                return self._update()
            if data and self.first_frame_delay is None:
                self.first_frame_delay = time.monotonic() - self.connected_at
            self.frames_received += bool(data)
            self.pending.extend(data)
            while len(self.pending) >= 640:
                frame = bytes(self.pending[:640])
                del self.pending[:640]
                samples = array("h", frame)
                self.rms.append(
                    math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                )
                self.audio.extend(frame)
            return self._update()

    def _abandon_inflight(self) -> None:
        if self.inflight and not self.inflight_abandoned:
            self.lost_segments += 1
            self.lost_seconds += self.inflight / 32000
            self.inflight_abandoned = True

    def _call(self, pcm: bytes) -> OneStepOutcome:
        timeout = (
            min(self.timeout, max(0.01, self.deadline - time.monotonic()))
            if self.deadline
            else self.timeout
        )
        return _transcribe(self.engine, self.client, pcm, timeout)

    def _run(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.cancelled or self.queue or self.stopping)
                if self.cancelled or not self.queue:
                    return
                pcm = self.queue.popleft()
                self.inflight = len(pcm)
                self.chunks_sent += 1
                self.inflight_abandoned = False
            try:
                result = None
                for attempt in range(2):
                    started = time.monotonic()
                    self.request_started = started
                    request_status = "ok"
                    try:
                        result = self._call(pcm)
                        break
                    except (openai.APIConnectionError, openai.APIStatusError) as exc:
                        request_status = (
                            str(exc.status_code)
                            if isinstance(exc, openai.APIStatusError)
                            else "connection_error"
                        )
                        retryable = (
                            isinstance(exc, openai.APIConnectionError)
                            or exc.status_code == 429
                            or exc.status_code >= 500
                        )
                        if (
                            attempt
                            or not retryable
                            or (self.deadline and self.deadline <= time.monotonic())
                        ):
                            raise
                        self.retries += 1
                        with self.condition:
                            # Stopping uses the remaining request budget without backoff.
                            if self.deadline is None:
                                self.condition.wait(timeout=0.5)
                            if self.cancelled:
                                return
                    except Exception:
                        request_status = "invalid_response"
                        raise
                    finally:
                        self.request_started = None
                        self.requests.append(
                            {
                                "latency_s": round(time.monotonic() - started, 3),
                                "status": request_status,
                            }
                        )
                with self.condition:
                    if not self.cancelled and result is not None:
                        self.results.append(result)
                        self.transcript_chars += len(result.text)
                        if self.engine.method == "chat_audio":
                            self.cleanup_outcome = "cleaned" if result.cleaned else "raw_fallback"
            except Exception:  # noqa: BLE001 - redact provider and credential failures
                with self.condition:
                    if not self.cancelled:
                        self._lost(pcm)
            finally:
                with self.condition:
                    self.inflight = 0
                    self.condition.notify_all()

    def finish_update(self) -> DictationUpdate:
        with self.condition:
            if self.stopping:
                return self._update()
            self.deadline = time.monotonic() + self.stop_budget
            self.take_seconds = (len(self.audio) + len(self.pending) // 2 * 2) / 32000
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
                self._abandon_inflight()
                self.cancelled = True
                self.condition.notify_all()
            self._log_take("stopped")
            return self._update()

    def drain_updates(self) -> list[DictationUpdate]:
        with self.condition:
            updates = []
            while self.results:
                updates.append(self._update())
            return updates

    def finish(self) -> str:
        return self.finish_update().finalized or ""

    def _log_take(self, outcome: str) -> None:
        if self.logged:
            return
        self.logged = True
        levels = sorted(self.rms)
        quantiles = [
            round(levels[min(len(levels) - 1, int((len(levels) - 1) * q))], 2) if levels else 0
            for q in (0.1, 0.5, 0.9)
        ]
        _logger.info(
            "hosted dictation take %s",
            json.dumps(
                {
                    "outcome": outcome,
                    "take_seconds": getattr(
                        self, "take_seconds", (len(self.audio) + len(self.pending)) / 32000
                    ),
                    "first_frame_delay_s": self.first_frame_delay,
                    "frames_received": self.frames_received,
                    "rms_p10_p50_p90": quantiles,
                    "rms_max": round(max(levels, default=0), 2),
                    "chunks_sent": self.chunks_sent,
                    "requests": list(self.requests)
                    + (
                        [
                            {
                                "latency_s": round(time.monotonic() - self.request_started, 3),
                                "status": "deadline_exceeded",
                            }
                        ]
                        if self.request_started is not None
                        else []
                    ),
                    "retries": self.retries,
                    "undelivered_seconds": round(self.lost_seconds, 2),
                    "transcript_chars": self.transcript_chars,
                    "cleanup_outcome": self.cleanup_outcome,
                }
            ),
        )

    def close(self) -> None:
        with self.condition:
            if self.closed:
                return
            if not self.stopping:
                self._log_take("cancelled")
            self.closed = self.cancelled = True
            self.audio.clear()
            self.pending.clear()
            self.queue.clear()
            self.condition.notify_all()
        try:
            self.client.close()
        finally:
            self.worker.join(timeout=min(2, self.stop_timeout))

    def wait_closed(self) -> None:
        """Retain the router's capacity lease until the synchronous request really exits."""
        self.worker.join()
