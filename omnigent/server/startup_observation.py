"""Owner-scoped diagnostic projection over the existing runner transport."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

import httpx

from omnigent.errors import ErrorCode, OmnigentError

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.stores import ConversationStore

from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    PERMISSION_LABEL_KEYS,
    PERMISSION_RUNTIME_ID_LABEL_KEY,
    TIER_HARNESSES,
    read_permission_state,
)
from omnigent.startup_observation import (
    OBSERVATION_CAPABILITY,
    PROCESS_OBSERVATION_ID,
    comparands,
    context,
    loaded_build,
    runtime_fingerprint,
)

_LOADED_BUILD = loaded_build()


def _binding(conv: Conversation) -> dict[str, object]:
    return context(conv, session_id=conv.id, agent_id=conv.agent_id)


def _stamp(conv: Conversation) -> tuple[object, ...]:
    # Capture immutable scalars before awaiting; ORM/test rows may be mutable.
    return (_binding(conv), *(conv.labels.get(k) for k in sorted(PERMISSION_LABEL_KEYS)))


def _canonical_snapshot(
    labels: Mapping[str, str], harness: str | None
) -> dict[str, object] | None:
    # Unresolved/default/un-decodable is missing evidence, never synthesized denial.
    if not isinstance(harness, str) or harness not in TIER_HARNESSES:
        return None
    try:
        confirmed = read_permission_state(labels, harness=harness)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return {
        "permission_tier_state": confirmed.status,
        "permission_tier_revision": confirmed.revision,
        "permission_tiers_capability": labels.get(PERMISSION_CAPABILITY_LABEL_KEY)
        == PERMISSION_CAPABILITY_VALUE,
    }


async def read_startup_observation(
    conv: Conversation,
    *,
    harness: str | None,
    router: RunnerRouter | None,
    registry: TunnelRegistry | None,
    store: ConversationStore,
) -> dict[str, object]:
    # Called only AFTER the existing owner/admin permission check.
    from urllib.parse import quote

    binding = _binding(conv)
    before = _stamp(conv)
    snapshot = _canonical_snapshot(conv.labels, harness)
    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "startup_observation",
        "status": "unknown",
        "observed_at": time.time(),
        "binding": binding,
        "server_process_id": PROCESS_OBSERVATION_ID,
        "server_loaded_build": _LOADED_BUILD,
        "comparands": comparands(snapshot, None),
        "runner": None,
        "reason": "unsupported_transport",
        "admission": "not_evaluated",
    }
    if conv.archived:
        result["reason"] = "archived"
        return result
    if not conv.runner_id or router is None or registry is None:
        return result
    connection = registry.get(conv.runner_id)
    if connection is None:
        # Wrong replica and offline are not distinguishable from this read.
        result["reason"] = "bound_runner_not_connected_here"
        return result
    if OBSERVATION_CAPABILITY not in connection.hello.capabilities:
        result["reason"] = "runner_observation_not_advertised"
        return result
    try:
        routed = router.client_for_session_resources(conv.id, conversation=conv)
        if routed.runner_id != binding["runner_id"]:
            result["reason"] = "binding_changed"
            return result
        async with asyncio.timeout(2.0):
            response = await routed.client.get(
                f"/v1/sessions/{quote(conv.id, safe='')}/startup-observation/v1",
                follow_redirects=False,
                extensions={"runner_tunnel_generation": connection.generation},
                timeout=2.0,
            )
            if response.status_code in (401, 403):
                response.raise_for_status()
            payload = response.json() if response.status_code == 200 else None
    except OmnigentError as exc:
        # Exactly the two codes emitted by the real router's absent-runner branch.
        if exc.code not in (ErrorCode.RUNNER_UNAVAILABLE, ErrorCode.WRONG_REPLICA):
            raise
        result["reason"] = "bound_runner_route_unavailable"
        return result
    except (TimeoutError, httpx.TimeoutException):
        result["reason"] = "runner_observation_timeout"
        return result
    except (httpx.RequestError, ConnectionError, ValueError, LookupError):
        result["reason"] = "runner_observation_unavailable"
        return result
    current = await asyncio.to_thread(store.get_conversation, conv.id)
    if (
        current is None
        or _stamp(current) != before
        or registry.get(conv.runner_id) is not connection
    ):
        result["reason"] = "binding_or_connection_changed"
        return result
    raw = payload.get("startup_observation") if isinstance(payload, dict) else None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        result["reason"] = "runner_observation_unsupported"
        return result
    # Rebuild every nested object from an allowlist; never forward an opaque snapshot.
    local_context = raw.get("binding")
    expected = raw.get("expected_runner_revision")
    runtime_id = raw.get("permission_runtime_fingerprint")
    build = raw.get("loaded_build")
    observed_harness = raw.get("harness")
    if (
        local_context != binding
        or raw.get("init_fresh") is not True
        or raw.get("state_context_matches") is not True
        or type(expected) is not int
        or expected < 1
        or not isinstance(observed_harness, str)
        or observed_harness not in TIER_HARNESSES
        or (harness is not None and observed_harness != harness)
        or runtime_id is None
        or runtime_id != runtime_fingerprint(conv.labels.get(PERMISSION_RUNTIME_ID_LABEL_KEY))
    ):
        result["reason"] = "runner_context_incomplete_or_stale"
        return result
    # Resolve no agent bundle/config/cache on this read. The authenticated
    # runner reports the harness that owns this installed permission state.
    snapshot = _canonical_snapshot(conv.labels, observed_harness)
    clean_build = {
        key: build.get(key) if isinstance(build, dict) else None
        for key in ("commit_sha", "build_time_epoch", "version")
    }
    # Types must be bounded even from an authenticated older/buggy runner.
    sha = clean_build["commit_sha"]
    clean_build["commit_sha"] = (
        sha
        if isinstance(sha, str) and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
        else None
    )
    epoch = clean_build["build_time_epoch"]
    clean_build["build_time_epoch"] = epoch if type(epoch) is int and epoch > 0 else None
    version = clean_build["version"]
    clean_build["version"] = version if isinstance(version, str) and len(version) <= 80 else None
    result["runner"] = {
        "binding": binding,
        "connection_generation": connection.generation,
        "harness": observed_harness,
        "permission_runtime_fingerprint": runtime_id,
        "loaded_build": clean_build,
    }
    result["comparands"] = comparands(snapshot, expected)
    build_complete = all(v is not None for v in clean_build.values()) and all(
        _LOADED_BUILD.get(k) is not None for k in ("commit_sha", "build_time_epoch", "version")
    )
    result["status"] = "observed" if build_complete and snapshot is not None else "unknown"
    result["loaded_build_metadata_match"] = (
        clean_build["commit_sha"] == _LOADED_BUILD["commit_sha"] if build_complete else None
    )
    result["reason"] = (
        "server_state_unknown"
        if snapshot is None
        else "comparands_observed"
        if result["status"] == "observed"
        else "loaded_build_unknown"
    )
    result["observed_at"] = time.time()
    return result
