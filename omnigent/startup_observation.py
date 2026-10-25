"""Allowlisted observations; these values never authorize a turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from uuid import uuid4

import httpx

PROCESS_OBSERVATION_ID = uuid4().hex


def loaded_build() -> dict[str, object]:
    # Do not import/read a replacement build file after a live install.
    build = sys.modules.get("omnigent._build_info")
    version = sys.modules.get("omnigent.version")
    sha = getattr(build, "COMMIT_SHA", None)
    epoch = getattr(build, "BUILD_TIME_EPOCH", None)
    semver = getattr(version, "VERSION", None)
    return {
        "provenance": "already_loaded_module_metadata",
        "commit_sha": sha
        if isinstance(sha, str) and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
        else None,
        "build_time_epoch": epoch if type(epoch) is int and epoch > 0 else None,
        "version": semver if isinstance(semver, str) and len(semver) <= 80 else None,
    }


def runtime_fingerprint(value: object) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) and value else None


def context(source: object, *, session_id: str, agent_id: str | None) -> dict[str, object]:
    # Deliberately exclude labels, launch args, instructions, agents and config.
    result: dict[str, object] = {"session_id": session_id, "agent_id": agent_id}
    for key in (
        "runner_id",
        "host_id",
        "workspace",
        "worktree",
        "model_override",
        "harness_override",
        "reasoning_effort",
        "archived",
        "archive_revision",
    ):
        value = getattr(source, key, None)
        result[key] = value if value is None or type(value) in (str, int, bool) else None
    return result


def comparands(snapshot: object, expected_revision: int | None) -> dict[str, object]:
    data = snapshot if isinstance(snapshot, dict) else {}
    state = data.get("permission_tier_state")
    revision = data.get("permission_tier_revision")
    capability = data.get("permission_tiers_capability")
    return {
        "server_state": state if state in ("ready", "switching", "blocked") else None,
        "server_revision": revision if type(revision) is int else None,
        "expected_runner_revision": expected_revision,
        "capability": capability if type(capability) is bool else None,
        "ready_matches": state == "ready" if isinstance(snapshot, dict) else None,
        # Match the existing gate's Python equality; do not change admission here.
        "revision_matches": revision == expected_revision
        if isinstance(snapshot, dict) and expected_revision is not None
        else None,
        "capability_matches": capability is True if isinstance(snapshot, dict) else None,
    }


def _now() -> float:
    return asyncio.get_running_loop().time()


async def _pause(delay: float) -> None:
    await asyncio.sleep(delay)


async def wait_for_permission_confirmation(
    server_client: httpx.AsyncClient,
    session_path: str,
    expected_revision: int,
    bound_context: dict[str, object],
) -> None:
    # Exact previous polling predicate/order/deadline. A matching response at
    # the deadline still breaks before checking the deadline, as before.
    deadline = _now() + 20.0
    while True:
        response = await server_client.get(
            session_path,
            headers={"X-Omnigent-Permission-Tiers": "1"},
        )
        snapshot = response.json() if response.status_code == 200 else None
        if response.status_code == 200:
            if (
                isinstance(snapshot, dict)
                and snapshot.get("permission_tier_state") == "ready"
                and snapshot.get("permission_tier_revision") == expected_revision
                and snapshot.get("permission_tiers_capability") is True
            ):
                break
        if _now() >= deadline:
            diagnostic = {
                "schema_version": 1,
                "kind": "permission_confirmation_timeout",
                "http_status": response.status_code,
                "comparands": comparands(snapshot, expected_revision),
                "bound_context": bound_context,
                "server_binding": {
                    key: snapshot.get(key)
                    if isinstance(snapshot, dict) and type(snapshot.get(key)) in (str, int, bool)
                    else None
                    for key in ("id", "runner_id", "host_id", "agent_id", "workspace", "worktree")
                },
                "observed_at": time.time(),
            }
            raise RuntimeError(
                "Permission tier was not confirmed before turn; "
                + json.dumps(diagnostic, sort_keys=True)
            )
        await _pause(0.1)


# Additive authenticated hello feature, not semver/HEAD/operator configuration.
OBSERVATION_CAPABILITY = "permission_startup_observation_v1"
OBSERVATION_ROUTE = "/v1/sessions/{session_id}/startup-observation/v1"
OBSERVATION_MARKER = "_omnigent_startup_observation_v1"


def mark_observation_endpoint(endpoint: object) -> None:
    """Mark the exact registered GET implementation that discovery must find."""
    setattr(endpoint, OBSERVATION_MARKER, 1)


def registered_observation_capabilities(app: object) -> list[str]:
    """Advertise only the exact marked GET implementation registered on this app."""
    for route in getattr(app, "routes", ()):
        if (
            getattr(route, "path", None) == OBSERVATION_ROUTE
            and "GET" in getattr(route, "methods", ())
            and getattr(getattr(route, "endpoint", None), OBSERVATION_MARKER, None) == 1
        ):
            return [OBSERVATION_CAPABILITY]
    return []
