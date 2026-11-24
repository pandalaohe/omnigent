"""Tests for the per-request usage proxy and usage.request_source server config."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.server_config import UsageRequestSource, usage_request_source
from omnigent.server.usage_requests import UsageRequestsError
from omnigent.stores.usage_hours_store import _ACTIVITY_MAX_AGE_S, hours_of
from omnigent.usage.contract import UsageReportV2
from tests.server import test_usage_activity
from tests.server.test_usage_activity import HOUR, NOW

OWNER = RESERVED_USER_LOCAL
service = test_usage_activity.service


class _FakeGateway:
    """In-process loopback HTTP upstream mirroring the gateway response list read."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, dict[str, list[str]], str | None]] = []
        self.delay_s: float = 0.0
        self.redirect_to: str | None = None
        self.status_override: tuple[int, dict[str, Any]] | None = None
        self.known_accounts: set[str] | None = None

        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                gateway.requests.append((parsed.path, query, self.headers.get("Authorization")))
                if gateway.delay_s > 0:
                    time.sleep(gateway.delay_s)
                if gateway.redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", gateway.redirect_to)
                    self.end_headers()
                    return
                if gateway.status_override is not None:
                    code, payload = gateway.status_override
                    body = json.dumps(payload).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                hour = int(query["hour"][0])
                limit = int(query.get("limit", ["20"])[0])
                accounts = sorted(set(query.get("account", [])))
                if gateway.known_accounts is not None and (set(accounts) - gateway.known_accounts):
                    self._reply(404, {"error": "unknown-account"})
                    return
                hosts = set(query.get("host", []))
                agents = set(query.get("agent", []))
                models = set(query.get("model", []))
                scope_doc = {
                    "hour": hour,
                    "account": accounts,
                    "host": sorted(hosts),
                    "agent": sorted(agents),
                    "model": sorted(models),
                }
                scope = hashlib.sha256(
                    json.dumps(scope_doc, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()[:16]

                offset = 0
                raw_cursor = query.get("cursor", [None])[0]
                if raw_cursor is not None:
                    try:
                        decoded = json.loads(
                            base64.urlsafe_b64decode(
                                raw_cursor + "=" * (-len(raw_cursor) % 4)
                            ).decode()
                        )
                    except Exception:
                        self._reply(422, {"error": "invalid-cursor"})
                        return
                    if decoded.get("scope") != scope:
                        self._reply(422, {"error": "cursor-scope-mismatch"})
                        return
                    offset = int(decoded["offset"])

                matched = []
                for row in gateway.rows:
                    if row["account"] not in accounts:
                        continue
                    if hosts and (row.get("host") or "") not in hosts:
                        continue
                    if agents:
                        row_agent = row.get("agent")
                        untagged = "" in agents and row_agent in (None, "")
                        if not untagged and row_agent not in agents:
                            continue
                    if models and (row.get("model") or "") not in models:
                        continue
                    matched.append(row)

                page = matched[offset : offset + limit]
                next_cursor = None
                if offset + limit < len(matched):
                    cursor_doc = {"v": 1, "scope": scope, "offset": offset + limit}
                    next_cursor = (
                        base64.urlsafe_b64encode(
                            json.dumps(cursor_doc, separators=(",", ":")).encode()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                self._reply(
                    200,
                    {
                        "schema": "response-page/1",
                        "hour": hour,
                        "retained_from": hour - 86400,
                        "rows": page,
                        "next_cursor": next_cursor,
                        "sections": {
                            "litellm": {"state": "ok", "reason": None},
                            "relay": {"state": "ok", "reason": None},
                        },
                    },
                )

            def _reply(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _sample_rows(count: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "id": f"req-{index:02d}",
            "source": "relay" if index % 2 == 0 else "litellm",
            "at": f"2026-11-05T11:{59 - index:02d}:12.345Z",
            "host": "host-a" if index % 2 == 0 else None,
            "agent": "codex" if index % 3 == 0 else (None if index % 3 == 1 else "claude"),
            "account": "acct-alpha" if index < 10 else "acct-beta",
            "group": "shared",
            "model": "model-alpha" if index % 2 == 0 else "model-beta",
            "model_requested": "model-alias",
            "tokens": {
                "input": 100 + index,
                "cache_read": 20,
                "cache_write": 5,
                "output": 40,
                "reasoning": 0,
            },
            "cost_usd": "0.0125",
            "cost_basis": "litellm",
            "ttft_ms": 180,
            "duration_ms": 950 + index * 10,
            "tps": 51.2,
            "status": "error" if index == 2 else "ok",
            "http_status": 429 if index == 2 else 200,
            "error_class": "rate-limit" if index == 2 else None,
            "fallback": False,
            # Simulated leaky fields that must never reach the browser.
            "prompt": "do-not-leak-prompt-body",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "completion": "secret answer",
        }
        for index in range(count)
    ]


def _make_app(service, request_source: UsageRequestSource | None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(
            service,
            auth_provider=None,
            host_store=None,
            request_source=request_source,
        ),
        prefix="/v1",
    )
    return app


def test_usage_request_source_config_validation() -> None:
    assert usage_request_source({}) is None
    assert usage_request_source({"usage": {}}) is None
    assert usage_request_source({"usage": {"request_source": None}}) is None

    parsed = usage_request_source(
        {"usage": {"request_source": {"base_url": "http://127.0.0.1:18405/"}}}
    )
    assert parsed == UsageRequestSource(base_url="http://127.0.0.1:18405")

    with_env = usage_request_source(
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test/prefix/",
                    "credential_env": "USAGE_REQUEST_TOKEN",
                }
            }
        }
    )
    assert with_env == UsageRequestSource(
        base_url="https://board.example.test/prefix",
        credential_env="USAGE_REQUEST_TOKEN",
    )

    with_file = usage_request_source(
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test",
                    "credential_file": "/opt/work/secrets/request-token",
                }
            }
        }
    )
    assert with_file == UsageRequestSource(
        base_url="https://board.example.test",
        credential_file="/opt/work/secrets/request-token",
    )

    for bad in (
        {"usage": "bad"},
        {"usage": {"request_source": "http://127.0.0.1:18405"}},
        {"usage": {"request_source": {}}},
        {"usage": {"request_source": {"base_url": "ftp://board.example.test"}}},
        {"usage": {"request_source": {"base_url": "https://user:pass@board.example.test"}}},
        {"usage": {"request_source": {"base_url": "https://board.example.test?a=1"}}},
        {"usage": {"request_source": {"base_url": "https://board.example.test#frag"}}},
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test",
                    "token": "literal-secret-must-fail",
                }
            }
        },
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test",
                    "credential_env": "not-an-env-var",
                }
            }
        },
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test",
                    "credential_file": "relative/path",
                }
            }
        },
        {
            "usage": {
                "request_source": {
                    "base_url": "https://board.example.test",
                    "credential_env": "A_TOKEN",
                    "credential_file": "/opt/work/token",
                }
            }
        },
    ):
        with pytest.raises(ValueError) as exc:
            usage_request_source(bad)
        assert "literal-secret-must-fail" not in str(exc.value)
        assert "pass" not in str(exc.value)


@pytest.mark.asyncio
async def test_requests_unset_returns_404_not_configured_and_flags_analysis(
    service, monkeypatch
) -> None:
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    service.report_account(
        OWNER, "acct-alpha", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    app = _make_app(service, request_source=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not_configured"

        analysis = await client.get("/v1/me/usage/analysis?group=account&range=d7")
        assert analysis.status_code == 200
        assert analysis.json()["requests_configured"] is False

        hours = await client.get(
            "/v1/me/usage/analysis/hours?group=account&range=d7&day=2026-11-05"
        )
        assert hours.status_code == 200
        assert hours.json()["requests_configured"] is False


@pytest.mark.asyncio
async def test_requests_proxy_paging_scope_cursor_and_no_prompt_text(service, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    service.report_account(
        OWNER, "acct-alpha", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    service.report_account(
        "other-user", "acct-beta", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    gateway = _FakeGateway(_sample_rows(12))
    try:
        source = UsageRequestSource(base_url=gateway.base_url)
        app = _make_app(service, request_source=source)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Analysis reports requests_configured=True when configured.
            hours = await client.get(
                "/v1/me/usage/analysis/hours?group=account&range=d7&day=2026-11-05"
            )
            assert hours.status_code == 200
            assert hours.json()["requests_configured"] is True

            # Page 1: 8 rows, next_cursor set, server resolved scope to OWNER's acct-alpha only.
            page1 = await client.get(f"/v1/me/usage/requests?hour={HOUR}&limit=8&tz=UTC")
            assert page1.status_code == 200, page1.text
            assert page1.headers.get("cache-control") == "private, no-store"
            body1 = page1.json()
            assert len(body1["rows"]) == 8
            assert body1["next_cursor"] is not None
            assert all(row["account"] == "acct-alpha" for row in body1["rows"])
            assert "prompt" not in page1.text
            assert "secret prompt" not in page1.text
            assert "completion" not in page1.text
            assert gateway.requests[-1][0] == "/api/v2/responses"
            assert gateway.requests[-1][1]["account"] == ["acct-alpha"]

            # Page 2 via cursor: remaining 2 rows of acct-alpha, no next_cursor.
            cursor = body1["next_cursor"]
            page2 = await client.get(
                f"/v1/me/usage/requests?hour={HOUR}&limit=8&tz=UTC&cursor={cursor}"
            )
            assert page2.status_code == 200
            body2 = page2.json()
            assert [row["id"] for row in body2["rows"]] == ["req-08", "req-09"]
            assert body2["next_cursor"] is None
            assert gateway.requests[-1][1]["account"] == ["acct-alpha"]

            # Cursor minted for unfiltered scope cannot be reused on a different filter/hour.
            mismatch = await client.get(
                f"/v1/me/usage/requests?hour={HOUR}&limit=8&host=host-a&cursor={cursor}"
            )
            assert mismatch.status_code == 422
            assert mismatch.json()["detail"] == "cursor-scope-mismatch"

            # Forged cursor for another scope fails closed under server scope.
            forged_doc = {"v": 1, "scope": "0123456789abcdef", "offset": 0}
            forged_cursor = (
                base64.urlsafe_b64encode(json.dumps(forged_doc).encode()).rstrip(b"=").decode()
            )
            forged = await client.get(
                f"/v1/me/usage/requests?hour={HOUR}&limit=8&cursor={forged_cursor}"
            )
            assert forged.status_code == 422
            assert forged.json()["detail"] == "cursor-scope-mismatch"

            # Foreign account is refused with 403 before touching upstream.
            before = len(gateway.requests)
            forbidden = await client.get(
                f"/v1/me/usage/requests?hour={HOUR}&account=acct-beta&limit=8"
            )
            assert forbidden.status_code == 403
            assert len(gateway.requests) == before

            # Empty agent= selects untagged rows and passes through cleanly.
            untagged = await client.get(f"/v1/me/usage/requests?hour={HOUR}&agent=&limit=8")
            assert untagged.status_code == 200
            assert gateway.requests[-1][1]["agent"] == [""]
            assert all(row["agent"] is None for row in untagged.json()["rows"])
            assert len(untagged.json()["rows"]) > 0
    finally:
        gateway.close()


@pytest.mark.asyncio
async def test_requests_validation_422_and_hour_bounds(service, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    service.report_account(
        OWNER, "acct-alpha", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    gateway = _FakeGateway([])
    try:
        app = _make_app(service, request_source=UsageRequestSource(base_url=gateway.base_url))
        future_hour = hours_of(NOW) + 3600
        expired_hour = hours_of(NOW - _ACTIVITY_MAX_AGE_S) - 3600
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for query in (
                "",
                f"hour={HOUR + 1}",
                f"hour={future_hour}",
                f"hour={expired_hour}",
                f"hour={HOUR}&hour={HOUR}",
                f"hour={HOUR}&tz=Not/A_Zone",
                f"hour={HOUR}&limit=0",
                f"hour={HOUR}&limit=51",
                f"hour={HOUR}&agent=INVALID",
                f"hour={HOUR}&account=",
                f"hour={HOUR}&cursor=",
                f"hour={HOUR}&upstream=https://evil.example.test",
            ):
                resp = await client.get(f"/v1/me/usage/requests?{query}")
                assert resp.status_code == 422, (query, resp.status_code, resp.text)
            assert len(gateway.requests) == 0
    finally:
        gateway.close()


@pytest.mark.asyncio
async def test_requests_timeout_504_redirects_and_unknown_account(service, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    monkeypatch.setattr("omnigent.server.usage_requests.REQUEST_SOURCE_TIMEOUT_S", 0.08)
    service.report_account(
        OWNER, "acct-alpha", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    gateway = _FakeGateway(_sample_rows(3))
    try:
        app = _make_app(service, request_source=UsageRequestSource(base_url=gateway.base_url))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Timeout -> 504.
            gateway.delay_s = 0.25
            timed_out = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert timed_out.status_code == 504
            assert timed_out.json()["detail"] == "request_source_timeout"

            # Redirect -> 502 (never followed).
            gateway.delay_s = 0.0
            gateway.redirect_to = "https://other.example.test/secret"
            redirected = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert redirected.status_code == 502
            assert "other.example.test" not in redirected.text

            # Gateway 404 unknown-account -> 200 empty rows (not 404 not_configured).
            gateway.redirect_to = None
            gateway.known_accounts = {"some-other-gateway-account"}
            unknown = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert unknown.status_code == 200
            assert unknown.json()["rows"] == []
            assert unknown.json()["next_cursor"] is None
    finally:
        gateway.close()


@pytest.mark.asyncio
async def test_requests_credential_refs_and_fail_closed(service, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    service.report_account(
        OWNER, "acct-alpha", UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
    )
    gateway = _FakeGateway(_sample_rows(2))
    try:
        # Env var credential ref.
        env_name = "TEST_USAGE_REQUEST_CRED_REF"
        monkeypatch.setenv(env_name, "fake-test-bearer-value")
        app_env = _make_app(
            service,
            request_source=UsageRequestSource(
                base_url=gateway.base_url,
                credential_env=env_name,
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_env), base_url="http://test"
        ) as client:
            resp = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert resp.status_code == 200
            assert gateway.requests[-1][2] == "Bearer fake-test-bearer-value"
            assert "fake-test-bearer-value" not in resp.text

            monkeypatch.delenv(env_name)
            missing = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert missing.status_code == 503

        # File credential ref.
        cred_file = tmp_path / "request-cred"
        cred_file.write_text("fake-file-bearer-value\n", encoding="utf-8")
        app_file = _make_app(
            service,
            request_source=UsageRequestSource(
                base_url=gateway.base_url,
                credential_file=str(cred_file),
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_file), base_url="http://test"
        ) as client:
            resp = await client.get(f"/v1/me/usage/requests?hour={HOUR}")
            assert resp.status_code == 200
            assert gateway.requests[-1][2] == "Bearer fake-file-bearer-value"
            assert "fake-file-bearer-value" not in resp.text
    finally:
        gateway.close()


def test_usage_requests_error_holds_status_and_detail() -> None:
    err = UsageRequestsError(504, "request_source_timeout")
    assert err.status_code == 504
    assert err.detail == "request_source_timeout"
