"""Host CLI: domain parsing, status output and a full drain against a stub server."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import cli

_HOST = "a" * 32


def _runner() -> CliRunner:
    return CliRunner()


def _http_date() -> str:
    from email.utils import formatdate

    return formatdate(usegmt=True)


@pytest.mark.parametrize(
    "domain",
    ["", "host-only", ",claude-sdk", "host-1,nope", "h" * 257 + ",claude-sdk"],
)
def test_domain_parsing_rejects_bad_values(domain: str) -> None:
    result = _runner().invoke(cli, ["usage-export", "cutover", "status", "--domain", domain])
    assert result.exit_code == 2, result.output
    assert "Invalid value" in result.output


def test_status_prints_the_hold_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.usage_export.cli as usage_cli

    monkeypatch.setattr(usage_cli, "_server_base", lambda explicit: "https://omni.test")
    snapshot = {
        "domain": {"host_id": _HOST, "harness": "claude-sdk"},
        "state": "held",
        "active": True,
        "generation": 2,
        "hold_created_at": "2026-01-01T00:00:00Z",
        "released_at": None,
        "state_changed_at": "2026-01-01T00:00:00Z",
        "refused_count": 3,
        "row_version": 1,
        "latest_receipt": {
            "receipt_id": "b" * 32,
            "receipt_seq": 1,
            "kind": "drain",
            "digest": "c" * 64,
        },
        "open_admissions": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v1/usage-cutover/holds/{_HOST}/claude-sdk"
        return httpx.Response(200, json=snapshot)

    class _Client(httpx.AsyncClient):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        usage_cli,
        "_client_factory",
        lambda base: _Client(),
    )
    result = _runner().invoke(
        cli, ["usage-export", "cutover", "status", "--domain", f"{_HOST},claude-sdk"]
    )
    assert result.exit_code == 0, result.output
    assert "state:    held (generation 2)" in result.output
    assert "open admissions: 0" in result.output
    assert "receipt:  seq 1 drain" in result.output
    as_json = _runner().invoke(
        cli,
        [
            "usage-export",
            "cutover",
            "status",
            "--domain",
            f"{_HOST},claude-sdk",
            "--json",
        ],
    )
    assert json.loads(as_json.output) == snapshot


def test_drain_holds_waits_scans_flushes_and_stores_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.usage_export.cli as usage_cli

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(usage_cli, "_server_base", lambda explicit: "https://omni.test")
    monkeypatch.setattr(usage_cli, "_local_host_id", lambda: _HOST)
    calls: list[str] = []
    receipt = {
        "receipt_schema_version": 1,
        "receipt_id": "d" * 32,
        "receipt_seq": 1,
        "kind": "drain",
        "result": "drained",
        "failure_codes": [],
        "last_committed_sequence": None,
        "domain": {"host_id": _HOST, "harness": "codex-native"},
        "generation": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/usage-cutover/holds":
            assert json.loads(request.content)["generation"] == 1
            return httpx.Response(
                200,
                json={
                    "domain": {"host_id": _HOST, "harness": "codex-native"},
                    "state": "held",
                    "active": True,
                    "generation": 1,
                    "open_admissions": 0,
                },
            )
        if request.url.path == "/v1/usage-events":
            return httpx.Response(200, json={"events": []}, headers={"date": _http_date()})
        if request.url.path == f"/v1/usage-cutover/holds/{_HOST}/codex-native":
            assert request.method == "GET"
            return httpx.Response(200, json={"open_admissions": 0})
        if request.url.path.endswith("/receipts"):
            body = json.loads(request.content)
            assert body["detection"]["checkpoint_digest"]
            assert body["calls"] == []
            return httpx.Response(200, json=receipt)
        raise AssertionError(request.url.path)

    class _Client(httpx.AsyncClient):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(usage_cli, "_client_factory", lambda base: _Client())
    out = tmp_path / "receipt.json"
    result = _runner().invoke(
        cli,
        [
            "usage-export",
            "cutover",
            "drain",
            "--domain",
            f"{_HOST},codex-native",
            "--generation",
            "1",
            "--timeout",
            "5",
            "--receipt-out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["result"] == "drained"
    assert calls[0] == "POST /v1/usage-cutover/holds"
    assert calls[-1] == f"POST /v1/usage-cutover/holds/{_HOST}/codex-native/1/receipts"


def test_drain_refuses_a_clock_skew_and_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.usage_export.cli as usage_cli

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(usage_cli, "_server_base", lambda explicit: "https://omni.test")
    monkeypatch.setattr(usage_cli, "_local_host_id", lambda: _HOST)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/usage-cutover/holds":
            return httpx.Response(200, json={"state": "held", "generation": 1})
        if request.url.path == "/v1/usage-events":
            # A server clock a full minute in the past: |delta| > 30 s.
            return httpx.Response(
                200, json={"events": []}, headers={"date": "Wed, 21 Oct 2026 00:00:00 GMT"}
            )
        if request.url.path.endswith("/receipts"):
            body = json.loads(request.content)
            assert body["failure_codes"] == ["clock_skew"]
            return httpx.Response(
                200,
                json={
                    "result": "failed",
                    "receipt_seq": 1,
                    "failure_codes": ["clock_skew"],
                },
            )
        raise AssertionError(request.url.path)

    class _Client(httpx.AsyncClient):
        def __init__(self) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(usage_cli, "_client_factory", lambda base: _Client())
    monkeypatch.setattr(usage_cli, "_clock_offset_ms", _skewed_offset)
    result = _runner().invoke(
        cli,
        [
            "usage-export",
            "cutover",
            "drain",
            "--domain",
            f"{_HOST},codex-native",
            "--generation",
            "1",
        ],
    )
    assert result.exit_code == 5, result.output
    assert "clock_skew" in result.output


async def _skewed_offset(client: object, base: str) -> int:
    del client, base
    return 31_000
