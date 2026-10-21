"""Default-off capture using the runner's existing authenticated server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import sqlite3
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI

from omnigent.process_logging import data_dir
from omnigent.usage_export.outbox import UsageOutbox
from omnigent.usage_export.transcripts import claude_physical_identity


def capture_enabled() -> bool:
    return os.environ.get("OMNIGENT_USAGE_EXPORT_CAPTURE", "").lower() in {"1", "true", "yes"}


@lru_cache(maxsize=8)
def _outbox_at(directory: Path) -> UsageOutbox:
    return UsageOutbox(directory)


def outbox() -> UsageOutbox:
    return _outbox_at(data_dir() / "usage-export")


def codex_usage_boundary(message: Mapping[str, Any]) -> bool:
    return message.get("method") in {
        "thread/started",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/failed",
    }


async def prepare_capture(client: httpx.AsyncClient, session_id: str, harness: str) -> None:
    if capture_enabled() and harness in {
        "codex",
        "codex-sdk",
        "codex-native",
        "claude-sdk",
        "claude-native",
        "pi",
        "pi-native",
    }:
        await outbox().prepare(client, session_id, "codex-sdk" if harness == "codex" else harness)


def register_pi_native_sessions(directory: Path, session_id: str) -> None:
    if capture_enabled():
        outbox().register_pi_root(directory, session_id)


def capture_pi_rpc_message(session_id: str, message: Mapping[str, Any]) -> None:
    """Persist one RPC assistant response; RPC mode has no session transcript."""
    if not capture_enabled() or not session_id or session_id == "__default__":
        return
    response_id = message.get("responseId")
    timestamp = message.get("timestamp")
    unit_id = (
        response_id
        if isinstance(response_id, str) and response_id
        else f"ts:{timestamp}"
        if type(timestamp) is int and timestamp >= 0
        else uuid.uuid4().hex
    )
    metering = {
        "role": message.get("role"),
        "model": message.get("model"),
        "responseId": response_id,
        "usage": message.get("usage"),
    }
    outbox().append(
        session_id,
        "pi",
        {"harness_family": "pi", "identity_namespace": "pi/rpc/v1", "thread_kind": "main"},
        {"type": "message", "id": unit_id, "message": metering},
    )


def capture_codex_home(
    home: Path,
    session_id: str,
    harness: str,
    main_thread: str | None = None,
    *,
    final: bool = False,
    baseline_existing: bool = False,
) -> None:
    if not capture_enabled():
        return
    spool = outbox()
    for path in home.glob("sessions/**/rollout-*.jsonl"):
        physical: dict[str, Any] = {
            "physical_transcript_path": str(path.resolve()),
            "physical_subagent_id": None,
            "thread_kind": "unknown",
            "harness_family": "codex",
            "identity_namespace": "codex/rollout/v1",
        }
        # The scanner reads session_meta as the physical identity, never a
        # filename, Omnigent id, or local JSON-RPC request id.
        with path.open("rb") as transcript:
            first_line = transcript.readline(1024 * 1024)
        if not first_line.endswith(b"\n") and not final:
            continue
        first = json.loads(first_line)
        payload = first.get("payload") if isinstance(first, dict) else None
        if (
            not isinstance(first, dict)
            or first.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
        ):
            raise ValueError("missing_physical_identity")
        physical["physical_session_id"] = payload["id"]
        physical["upstream_thread_id"] = payload["id"]
        if payload["id"] == main_thread:
            physical["thread_kind"] = "main"
        if not spool.has_source(path):
            spool.register_source(
                path,
                session_id,
                harness,
                physical,
                start_offset=path.stat().st_size if baseline_existing else 0,
            )
        spool.scan(path, final=final)


def baseline_claude_transcripts(
    path: Path, session_id: str, *, start_offset: int | None = None
) -> None:
    """Freeze inherited files before launch; later children start at zero."""
    if not capture_enabled():
        return
    spool = outbox()
    if spool.has_source(path):
        return
    children = list((path.parent / path.stem / "subagents").glob("agent-*.jsonl"))
    # Register the parent last: it is the proof that all inherited children
    # were frozen, including after a partial registration failure.
    for source in [*children, path]:
        if not spool.has_source(source):
            spool.register_source(
                source,
                session_id,
                "claude-native",
                claude_physical_identity(source),
                start_offset=(
                    start_offset
                    if source == path and start_offset is not None
                    else source.stat().st_size
                ),
            )


def capture_claude_transcript(
    path: Path, session_id: str, *, start_offset: int | None = None
) -> None:
    if not capture_enabled():
        return
    spool = outbox()
    if start_offset and not spool.has_source(path):
        raise ValueError("usage_inherited_baseline_missing")
    paths = [path, *(path.parent / path.stem / "subagents").glob("agent-*.jsonl")]
    for source in paths:
        if not spool.has_source(source):
            spool.register_source(
                source,
                session_id,
                "claude-native",
                claude_physical_identity(source),
                start_offset=(start_offset or 0) if source == path else 0,
            )
        spool.scan(source)


def capture_claude_mirror(
    session_id: str, key: Mapping[str, Any], entries: list[Any], *, internal: bool
) -> None:
    if not capture_enabled():
        return
    physical_session = key.get("session_id")
    subpath = key.get("subpath")
    if not isinstance(physical_session, str) or not physical_session:
        raise ValueError("missing_physical_identity")
    child = None
    if subpath is not None:
        if (
            not isinstance(subpath, str)
            or not subpath.startswith("subagents/agent-")
            or "/" in subpath.removeprefix("subagents/")
        ):
            raise ValueError("unknown_physical_subagent")
        child = subpath.removeprefix("subagents/agent-")
    physical = {
        "physical_session_id": physical_session,
        "physical_subagent_id": child,
        "upstream_thread_id": child or physical_session,
        "thread_kind": "subagent" if child else "internal" if internal else "main",
        "harness_family": "claude",
        "identity_namespace": "claude-code/transcript/v1",
        # SessionStore keys do not carry the physical path. F1 remains
        # pending until independently captured, rather than guessing it.
        "physical_transcript_path": None,
    }
    spool = outbox()
    for record in entries:
        if isinstance(record, dict):
            spool.append(session_id, "claude-sdk", physical, record)


def retry_delay(attempt: int, retry_after: str = "") -> float:
    """Bound exponential full jitter, honoring a valid server lower bound."""
    cap = min(300, 5 * 2 ** min(attempt, 6))
    minimum = (
        int(retry_after)
        if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 4
        else 0
    )
    if not 1 <= minimum <= 3600:
        minimum = 0
    return max(minimum, random.uniform(1, cap))


def usage_capture_router(client: httpx.AsyncClient) -> APIRouter:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not capture_enabled():
            yield
            return
        spool = outbox()
        app.state.usage_export_status = "pending"

        async def deliver() -> None:
            attempt = 0
            while True:
                try:
                    await asyncio.to_thread(spool.scan_registered_sources)
                    await spool.drain(client)
                    app.state.usage_export_status = "observed-only"
                    attempt = 0
                    delay = 5
                except httpx.HTTPStatusError as exc:
                    app.state.usage_export_status = f"http-{exc.response.status_code}"
                    if exc.response.status_code in {400, 401, 403, 404, 409, 410, 422}:
                        return
                    raw = exc.response.headers.get("Retry-After", "")
                    delay = retry_delay(attempt, raw)
                    attempt += 1
                except (OSError, ValueError, sqlite3.Error, httpx.HTTPError):
                    app.state.usage_export_status = "capture-pending"
                    delay = retry_delay(attempt)
                    attempt += 1
                await asyncio.sleep(delay)

        task = asyncio.create_task(deliver(), name="usage-export-outbox")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return APIRouter(lifespan=lifespan)
