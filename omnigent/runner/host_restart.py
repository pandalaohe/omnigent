"""REST execution for the approval-gated host restart tool."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

HOST_RESTART_TOOL_HEADER = "X-Omnigent-Host-Restart-Tool"


async def execute_host_restart(
    args: dict[str, Any], client: httpx.AsyncClient | None, session_id: str | None
) -> str:
    if client is None or not session_id:
        return json.dumps({"error": "sys_host_restart requires a bound session and server access"})
    host = args.get("host")
    dry_run, force = args.get("dry_run", True), args.get("force", False)
    preview_id = args.get("preview_id")
    if (
        set(args) - {"host", "dry_run", "force", "preview_id"}
        or not isinstance(host, str)
        or not host.strip()
        or not isinstance(dry_run, bool)
        or not isinstance(force, bool)
        or (preview_id is not None and not isinstance(preview_id, str))
    ):
        return json.dumps({"error": "invalid host restart arguments"})
    try:
        response = await client.get("/v1/hosts", timeout=30)
        response.raise_for_status()
        matches = [
            row
            for row in response.json()["hosts"]
            if host.strip() in (row.get("host_id"), row.get("name"))
        ]
        if len(matches) != 1:
            return json.dumps({"error": "host not found or ambiguous; use its exact id"})
        host_id = matches[0]["host_id"]
        from omnigent.cli_auth import databricks_request_headers

        headers = databricks_request_headers(str(client.base_url), host_id=host_id)
        path = f"/v1/hosts/{quote(host_id, safe='')}/restart"
        body = {
            "dry_run": dry_run,
            "force": force,
            "preview_id": preview_id,
            "requested_by_session_id": session_id,
        }
        if not dry_run:
            headers[HOST_RESTART_TOOL_HEADER] = "1"
            if preview_id is None:
                # Fetch the preview the app's card approved, never silently refresh it.
                response = await client.post(
                    path, json={**body, "dry_run": True}, headers=headers, timeout=30
                )
                response.raise_for_status()
                body["preview_id"] = response.json()["preview_id"]
        response = await client.post(path, json=body, headers=headers, timeout=30)
        response.raise_for_status()
        return json.dumps(response.json())
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return json.dumps({"error": f"sys_host_restart failed: {exc}"})
