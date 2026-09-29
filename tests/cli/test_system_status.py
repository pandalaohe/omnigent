"""Unit tests for ``omnigent system status``."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.util.server_url import ServerUrl

_BASE = "http://localhost:6767"

_STATUS: dict[str, Any] = {
    "revision": 2,
    "level": "amber",
    "findings": [
        {
            "id": "host_a:cpu",
            "target": "host_a",
            "kind": "cpu",
            "level": "amber",
            "since": 1.0,
            "detail": "cpu above 85% for 10 minutes",
            "top_session": "conv_a",
        }
    ],
    "server": None,
    "hosts": [
        {
            "host_id": "host_a",
            "owner": "alice@example.com",
            "name": "laptop",
            "state": "online",
            "since": 0.0,
            "last_snapshot": {
                "sampled_at": "2026-09-29T09:25:00+00:00",
                "interval_s": 60,
                "machine": {"cpu_pct": 91.0, "mem_used": 8, "mem_total": 16},
                "processes": [],
                "runner_count": 1,
            },
            "last_runner_count": 1,
        }
    ],
    "monitor_overhead": {},
}


def _patch_server(base_url: str = _BASE) -> Any:
    """Patch the CLI so it targets *base_url* without resolving a server."""
    return patch(
        "omnigent.cli._resolve_attach_server_url",
        return_value=ServerUrl(base_url),
    )


@respx.mock
def test_system_status_json_prints_the_response() -> None:
    """``--json`` prints the machine-readable status payload."""
    respx.get(f"{_BASE}/v1/system/status").mock(return_value=httpx.Response(200, json=_STATUS))

    with _patch_server():
        result = CliRunner().invoke(cli, ["system", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] == "amber"
    assert payload["findings"][0]["id"] == "host_a:cpu"


@respx.mock
def test_system_status_text_summarises_level_findings_and_hosts() -> None:
    """The default output shows the level, findings and one host line."""
    respx.get(f"{_BASE}/v1/system/status").mock(return_value=httpx.Response(200, json=_STATUS))

    with _patch_server():
        result = CliRunner().invoke(cli, ["system", "status"])

    assert result.exit_code == 0, result.output
    assert "level: amber" in result.output
    assert "host_a:cpu" in result.output
    assert "host_a" in result.output
    assert "online" in result.output
    assert "cpu 91%" in result.output
    assert "mem 50%" in result.output


def test_system_status_without_a_server_errors() -> None:
    """No resolvable server is an error — the CLI never starts one itself."""
    with patch("omnigent.cli._resolve_attach_server_url", return_value=None):
        result = CliRunner().invoke(cli, ["system", "status"])

    assert result.exit_code != 0
    assert "No Omnigent server found" in result.output


@respx.mock
def test_system_status_404_names_the_missing_route() -> None:
    """A server without the system-status route gets an actionable error."""
    respx.get(f"{_BASE}/v1/system/status").mock(return_value=httpx.Response(404))

    with _patch_server():
        result = CliRunner().invoke(cli, ["system", "status"])

    assert result.exit_code != 0
    assert "/v1/system/status" in result.output
