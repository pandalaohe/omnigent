"""Sanitized Codex account rate-limit collection for connected Hosts.

The Codex app-server owns the subscription-authenticated rate-limit protocol.
This module keeps that login state inside the Host process and returns only the
window percentages, durations, and reset timestamps needed by the web UI.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import suppress
from typing import Any

from omnigent._platform import resolve_cli_binary
from omnigent.inner import _proc
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport
from omnigent.inner.agent_env import clean_agent_env
from omnigent.json_types import JsonObject as _JsonObject

CODEX_RATE_LIMITS_REFRESH_INTERVAL_S = 300.0
CODEX_RATE_LIMITS_HARD_TTL_S = 3600
_CODEX_RATE_LIMITS_PROBE_TIMEOUT_S = 12.0
_CODEX_RATE_LIMITS_MAX_BUCKETS = 16
_CODEX_RATE_LIMITS_MAX_WINDOW_MINS = 5 * 525_600


def _number(value: object) -> float | None:
    """Return a finite JSON number while rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _window(raw: object, *, kind: str) -> _JsonObject | None:
    """Sanitize one Codex primary/secondary rate-limit window."""
    if not isinstance(raw, dict):
        return None
    used_percent = _number(raw.get("usedPercent"))
    duration = raw.get("windowDurationMins")
    resets_at = raw.get("resetsAt")
    if used_percent is None or not 0 <= used_percent <= 100:
        return None
    if isinstance(duration, bool) or not isinstance(duration, int):
        return None
    if not 0 < duration <= _CODEX_RATE_LIMITS_MAX_WINDOW_MINS:
        return None
    window: _JsonObject = {
        "kind": kind,
        "used_percent": used_percent,
        "window_duration_mins": duration,
    }
    if isinstance(resets_at, int) and not isinstance(resets_at, bool) and resets_at > 0:
        window["resets_at"] = resets_at
    return window


def normalize_codex_rate_limits_response(
    response: object,
    *,
    captured_at: int | None = None,
) -> _JsonObject | None:
    """Convert an app-server response into the bounded Host wire snapshot.

    Account identifiers, plan metadata, credit balances, and authentication
    fields are intentionally discarded here before the payload can cross the
    Host-to-Server boundary.
    """
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None

    rows: list[tuple[str, dict[str, Any]]] = []
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        for raw_limit_id, raw_bucket in list(by_limit_id.items())[:_CODEX_RATE_LIMITS_MAX_BUCKETS]:
            if isinstance(raw_limit_id, str) and raw_limit_id and isinstance(raw_bucket, dict):
                rows.append((raw_limit_id, raw_bucket))
    if not rows:
        raw_bucket = result.get("rateLimits")
        if isinstance(raw_bucket, dict):
            raw_limit_id = raw_bucket.get("limitId")
            limit_id = raw_limit_id if isinstance(raw_limit_id, str) and raw_limit_id else "codex"
            rows.append((limit_id, raw_bucket))

    limits: list[_JsonObject] = []
    for limit_id, raw_bucket in rows:
        windows = [
            window
            for kind, key in (("primary", "primary"), ("secondary", "secondary"))
            if (window := _window(raw_bucket.get(key), kind=kind)) is not None
        ]
        if not windows:
            continue
        bucket: _JsonObject = {"limit_id": limit_id, "windows": windows}
        raw_name = raw_bucket.get("limitName")
        if isinstance(raw_name, str) and raw_name.strip():
            bucket["limit_name"] = raw_name.strip()[:128]
        limits.append(bucket)

    if not limits:
        return None
    return {
        "captured_at": int(time.time()) if captured_at is None else captured_at,
        "limits": limits,
    }


def validate_codex_rate_limits_snapshot(snapshot: object) -> _JsonObject | None:
    """Validate a sanitized snapshot received on the Host wire boundary."""
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise ValueError("codex rate limits must be an object or null")
    captured_at = snapshot.get("captured_at")
    raw_limits = snapshot.get("limits")
    if (
        isinstance(captured_at, bool)
        or not isinstance(captured_at, int)
        or captured_at <= 0
        or not isinstance(raw_limits, list)
        or not 0 < len(raw_limits) <= _CODEX_RATE_LIMITS_MAX_BUCKETS
    ):
        raise ValueError("invalid codex rate-limit snapshot")

    limits: list[_JsonObject] = []
    for raw_bucket in raw_limits:
        if not isinstance(raw_bucket, dict):
            raise ValueError("invalid codex rate-limit bucket")
        limit_id = raw_bucket.get("limit_id")
        raw_windows = raw_bucket.get("windows")
        if not isinstance(limit_id, str) or not limit_id or len(limit_id) > 128:
            raise ValueError("invalid codex rate-limit id")
        if not isinstance(raw_windows, list) or not 0 < len(raw_windows) <= 2:
            raise ValueError("invalid codex rate-limit windows")
        windows: list[_JsonObject] = []
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                raise ValueError("invalid codex rate-limit window")
            kind = raw_window.get("kind")
            if kind not in {"primary", "secondary"}:
                raise ValueError("invalid codex rate-limit window kind")
            normalized = _window(
                {
                    "usedPercent": raw_window.get("used_percent"),
                    "windowDurationMins": raw_window.get("window_duration_mins"),
                    "resetsAt": raw_window.get("resets_at"),
                },
                kind=str(kind),
            )
            if normalized is None:
                raise ValueError("invalid codex rate-limit window values")
            windows.append(normalized)
        bucket: _JsonObject = {"limit_id": limit_id, "windows": windows}
        limit_name = raw_bucket.get("limit_name")
        if limit_name is not None:
            if not isinstance(limit_name, str) or not limit_name or len(limit_name) > 128:
                raise ValueError("invalid codex rate-limit name")
            bucket["limit_name"] = limit_name
        limits.append(bucket)
    return {"captured_at": captured_at, "limits": limits}


async def _read_response(
    stdout: asyncio.StreamReader,
    *,
    request_id: int,
) -> _JsonObject:
    """Read JSONL until the matching app-server response arrives."""
    while True:
        line = await stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server closed before replying")
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error") is not None:
            raise RuntimeError("Codex rate-limit RPC is unavailable")
        return message


async def read_codex_rate_limits_snapshot(
    *,
    codex_path: str | None = None,
    timeout_s: float = _CODEX_RATE_LIMITS_PROBE_TIMEOUT_S,
) -> _JsonObject | None:
    """Read the current Host user's Codex subscription rate-limit snapshot."""
    resolved = codex_path or resolve_cli_binary("codex", env_var="OMNIGENT_CODEX_PATH")
    if resolved is None:
        return None
    # Use the shared safe process base rather than inheriting the Host's
    # unrelated credentials. Preserve an explicit CODEX_HOME because it owns
    # the same login/config the interactive TUI uses; never pass an API key,
    # whose billing/rate-limit semantics differ from a subscription.
    env = clean_agent_env(
        allow_exact=("CODEX_HOME",),
        deny_exact=("OPENAI_API_KEY",),
    )
    proc = await asyncio.create_subprocess_exec(
        resolved,
        "app-server",
        "--listen",
        "stdio://",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        **_proc.spawn_kwargs(),
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        async with asyncio.timeout(timeout_s):
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "clientInfo": {
                                    "name": "omnigent-host-rate-limits",
                                    "version": "0.1",
                                },
                                "capabilities": {"experimentalApi": True},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            await _read_response(proc.stdout, request_id=1)
            proc.stdin.write((json.dumps({"method": "initialized", "params": {}}) + "\n").encode())
            proc.stdin.write(
                (
                    json.dumps({"id": 2, "method": "account/rateLimits/read", "params": {}}) + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            response = await _read_response(proc.stdout, request_id=2)
            return normalize_codex_rate_limits_response(response)
    finally:
        if proc.returncode is None:
            _proc.terminate_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                _proc.kill_tree(proc)
                with suppress(Exception):
                    await proc.wait()
        proc.stdin.close()
        with suppress(Exception):
            await proc.stdin.wait_closed()
        close_subprocess_transport(proc)


__all__ = [
    "CODEX_RATE_LIMITS_HARD_TTL_S",
    "CODEX_RATE_LIMITS_REFRESH_INTERVAL_S",
    "normalize_codex_rate_limits_response",
    "read_codex_rate_limits_snapshot",
    "validate_codex_rate_limits_snapshot",
]
