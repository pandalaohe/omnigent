"""Default-off capture using the runner's existing authenticated server client."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import sqlite3
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import tomllib
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


def routing_attestation(harness: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Fingerprint only public routing configuration, never authentication material."""
    env = os.environ if env is None else env
    claude = harness.startswith("claude")
    kind = "anthropic" if claude else "openai"
    default = "https://api.anthropic.com" if claude else "https://api.openai.com"
    raw = env.get("ANTHROPIC_BASE_URL" if claude else "OPENAI_BASE_URL", default)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid_usage_route_origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    mode = "api" if ("ANTHROPIC_API_KEY" if claude else "OPENAI_API_KEY") in env else "configured"
    material = [kind, origin, parsed.path.rstrip("/"), env.get("OMNIGENT_GATEWAY_ROUTE", ""), mode]
    return {
        "billing_route": "direct_provider" if raw.rstrip("/") == default else "gateway",
        "route_ref": "route-v1:"
        + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest(),
    }


def begin_admitted_turn(session_id: str, harness: str, admission: Mapping[str, Any]) -> None:
    if not capture_enabled():
        return
    harness = "codex-sdk" if harness == "codex" else harness
    outbox().begin_admission(session_id, harness, {**admission, "route_check": None})


def check_live_route(
    session_id: str,
    harness: str,
    env: Mapping[str, str],
    *,
    codex_home: Path | None = None,
    provider: str | None = None,
) -> None:
    if not capture_enabled():
        return
    route_env = dict(env)
    if harness.startswith("codex") and codex_home is not None:
        document = tomllib.loads((codex_home / "config.toml").read_text())
        provider = provider or document.get("model_provider", "openai")
        config = document.get("model_providers", {}).get(provider, {})
        route_env["OPENAI_BASE_URL"] = config.get("base_url", "https://api.openai.com")
        route_env["OMNIGENT_GATEWAY_ROUTE"] = provider
    outbox().record_live_route(session_id, harness, routing_attestation(harness, route_env))


def link_codex_turn(session_id: str, harness: str, physical: str, turn_id: str) -> None:
    if not capture_enabled():
        return
    spool = outbox()
    pending = spool.pending_admission(session_id, harness)
    if pending is not None:
        spool.link_admission(
            session_id,
            harness,
            physical,
            None,
            turn_id,
            pending["admission_id"],
            route_check=pending.get("route_check"),
        )
        spool.finish_admission(session_id, harness)


async def finish_admitted_turn(
    client: httpx.AsyncClient, session_id: str, harness: str, admission_id: str
) -> None:
    harness = "codex-sdk" if harness == "codex" else harness
    if capture_enabled():
        await asyncio.to_thread(outbox().end_admitted_turn, session_id, harness, admission_id)
    response = await client.post(
        "/v1/usage-cutover/admissions",
        json={
            "session_id": session_id,
            "admission_id": admission_id,
            "ended": True,
        },
    )
    response.raise_for_status()


def codex_usage_boundary(message: Mapping[str, Any]) -> bool:
    return message.get("method") in {
        "thread/started",
        "turn/started",
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
        "antigravity-native",
    }:
        feed_harness = "codex-sdk" if harness == "codex" else harness
        await outbox().prepare(client, session_id, feed_harness)
        outbox().mark_capture(session_id, feed_harness)
        route = outbox().session_route(session_id, feed_harness)
        if route is not None:
            response = await client.post("/v1/usage-cutover/route", json=route)
            response.raise_for_status()


def register_pi_native_sessions(directory: Path, session_id: str) -> None:
    if capture_enabled():
        outbox().register_pi_root(directory, session_id)


def capture_antigravity_step(
    session_id: str,
    cascade_id: str,
    step: Mapping[str, Any],
    *,
    thread_kind: Literal["main", "subagent"] = "main",
) -> None:
    """Spool native metering identities; no admission or account association is inferred."""
    if not capture_enabled() or step.get("status") not in {
        "CORTEX_STEP_STATUS_DONE",
        "CORTEX_STEP_STATUS_ERROR",
    }:
        return
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return
    retries = metadata.get("retryInfos")
    if not isinstance(metadata.get("modelUsage"), dict) and not isinstance(retries, list):
        return
    physical = {
        "harness_family": "antigravity",
        "identity_namespace": "antigravity/cascade/v1",
        "physical_session_id": cascade_id,
        "upstream_thread_id": cascade_id,
        "thread_kind": thread_kind,
    }
    spool = outbox()
    spool.append(session_id, "antigravity-native", physical, step)
    if isinstance(retries, list):
        for index, retry in enumerate(retries):
            usage = retry.get("usage") if isinstance(retry, dict) else None
            if isinstance(usage, dict):
                spool.append(
                    session_id,
                    "antigravity-native",
                    physical,
                    {
                        **step,
                        "metadata": {**metadata, "modelUsage": usage},
                        "usage_source": f"retry:{index}",
                    },
                )


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
    registered_scan: bool = False,
) -> None:
    if not registered_scan and not capture_enabled():
        return
    spool = outbox()
    spool.register_root(home, session_id, harness, main_thread)
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
        source = payload.get("source")
        subagent = (
            source.get("subAgent", source.get("subagent")) if isinstance(source, dict) else None
        )
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
        if isinstance(parent, str):
            physical["parent_physical_session_id"] = parent
            physical["thread_kind"] = "subagent"
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
        # The outbox supplies a path only after an SDK hook proves this tuple.
        "physical_transcript_path": None,
    }
    spool = outbox()
    for record in entries:
        if isinstance(record, dict):
            spool.append(session_id, "claude-sdk", physical, record)


def capture_claude_hook_path(
    session_id: str, payload: Mapping[str, Any], *, harness: str = "claude-sdk"
) -> None:
    """Bind the SDK mirror key to the actual path reported by a CLI hook."""
    if not capture_enabled():
        return
    event = payload.get("hook_event_name")
    parent = payload.get("session_id")
    if not isinstance(parent, str) or not parent:
        return
    child: str | None = None
    if event == "SubagentStop":
        child = payload.get("agent_id")
        raw_path = payload.get("agent_transcript_path")
        if not isinstance(child, str) or not child:
            return
    elif event in {"UserPromptSubmit", "Stop"} and payload.get("agent_id") is None:
        raw_path = payload.get("transcript_path")
    else:
        return
    if not isinstance(raw_path, str) or not raw_path:
        return
    try:
        reported_path = Path(raw_path)
        if not reported_path.is_absolute():
            return
        path = reported_path.resolve(strict=True)
        if not path.is_file():
            return
        physical = claude_physical_identity(path)
    except (OSError, ValueError):
        return
    if physical["physical_session_id"] != parent or physical["physical_subagent_id"] != child:
        outbox().bind_claude_path(session_id, parent, child, None)
        return
    spool = outbox()
    if harness == "claude-sdk":
        spool.bind_claude_path(session_id, parent, child, str(path))
        if not spool.has_source(path):
            spool.register_source(
                path, session_id, harness, physical, start_offset=path.stat().st_size
            )
    if event in {"Stop", "SubagentStop"}:
        spool.record_thread_end(
            session_id,
            harness,
            parent,
            child,
            str(payload.get("hook_event_id") or uuid.uuid4().hex),
        )


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
            reported_routes: dict[tuple[str, str], str] = {}
            while True:
                try:
                    for route in spool.live_routes():
                        key = json.dumps(route, sort_keys=True)
                        domain = (route["host_id"], route["harness"])
                        if reported_routes.get(domain) != key:
                            response = await client.post("/v1/usage-cutover/route", json=route)
                            response.raise_for_status()
                            reported_routes[domain] = key
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
