"""Real authenticated source-evidence doors and reads."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from omnigent.db.utils import now_epoch
from omnigent.server.auth import scope_allows
from omnigent.server.server_config import usage_evidence_retention_days
from tests.server.integration.test_global_instructions_routes import _headers, _make_user
from tests.server.integration.test_global_instructions_routes import auth_app as authenticated_app  # noqa: F401


def event(id="one", **changes):
    value = {
        "specversion": "1.0",
        "id": id,
        "source": "/reader",
        "type": "usage.source.status_changed",
        "time": datetime.fromtimestamp(now_epoch(), UTC).isoformat(),
        "data": {"state": "ok"},
    }
    return dict(value, **changes)


@pytest.mark.asyncio
async def test_doors_replay_sources_reads_auth_and_atomic_envelopes(request, db_uri):
    app = request.getfixturevalue("authenticated_app")
    _make_user(db_uri, "owner@example.test", is_admin=False)
    _make_user(db_uri, "other@example.test", is_admin=False)
    headers = _headers("owner@example.test")
    batch_headers = dict(headers, **{"content-type": "application/cloudevents-batch+json"})
    path = "/v1/me/usage/"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(path + "events")).status_code == 401
        entries = [event("one"), event("two", subject="a"), event("three")]
        response = await client.post(path + "events", headers=batch_headers, json=entries)
        assert response.status_code == 200, response.text
        assert response.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
        response = await client.post(
            path + "events", headers=batch_headers, json=[*entries, event("four")]
        )
        assert response.json() == {"accepted": 1, "duplicates": 3, "rejected": []}
        page = await client.get(path + "events?limit=2", headers=headers)
        assert page.headers["cache-control"] == "private, no-store"
        assert page.json()["stored"] == 4
        assert page.json()["history_complete"] is False
        second = await client.get(
            path + "events",
            params={"limit": 2, "before": page.json()["next_before"]},
            headers=headers,
        )
        assert len({row["id"] for row in page.json()["events"] + second.json()["events"]}) == 4
        assert (await client.get(path + "events?account=a", headers=headers)).json()["stored"] == 1
        assert (await client.get(path + "events", headers=_headers("other@example.test"))).json()[
            "events"
        ] == []
        for invalid in ({}, [event()] * 501):
            assert (
                await client.post(path + "events", headers=batch_headers, json=invalid)
            ).status_code == 422
        assert (await client.get(path + "events", headers=headers)).json()["stored"] == 4
        assert (await client.get(path + "events?limit=201", headers=headers)).status_code == 422
        assert (await client.get(path + "events?before=bad", headers=headers)).status_code == 422
        sources = {
            "schema_version": 1,
            "sources": [
                {"source": "reader", "state": "ok", "fresh_until": now_epoch() - 1},
                {"account": "a", "source": "reader", "state": "credential_missing"},
            ],
        }
        status = await client.put(path + "evidence", headers=headers, json=sources)
        assert status.status_code == 200, status.text
        assert status.json()["applied"]["sources"] == 2
        assert (await client.get(path + "sources", headers=headers)).json()["sources"][0][
            "current"
        ] is False
        assert (
            len((await client.get(path + "sources?account=a", headers=headers)).json()["sources"])
            == 1
        )
        assert (
            await client.put(path + "evidence", headers=headers, json={"schema_version": 1})
        ).json()["applied"]["sources"] == 0
        assert len((await client.get(path + "sources", headers=headers)).json()["sources"]) == 2
        assert (
            await client.put(
                path + "evidence", headers=headers, json={"schema_version": 2, "sources": []}
            )
        ).status_code == 422
        assert (
            await client.post(
                path + "events", headers=batch_headers, content=b"[" + b" " * (1024 * 1024) + b"]"
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_indexed_event_rejections_and_single_mode(request, db_uri):
    app = request.getfixturevalue("authenticated_app")
    _make_user(db_uri, "owner@example.test", is_admin=False)
    headers = dict(
        _headers("owner@example.test"), **{"content-type": "application/cloudevents-batch+json"}
    )
    missing_time = event()
    del missing_time["time"]
    entries = [
        missing_time,
        event(subject="bad/account"),
        event(data={"text": "x" * 5000}),
        event(time="2000-01-01T00:00:00Z"),
        event(time="2099-01-01T00:00:00Z"),
        event("valid"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/v1/me/usage/events", headers=headers, json=entries)
        assert result.status_code == 200, result.text
        assert result.json() == {
            "accepted": 1,
            "duplicates": 0,
            "rejected": [
                {"index": index, "reason": reason}
                for index, reason in enumerate(
                    [
                        "invalid_attribute",
                        "invalid_attribute",
                        "data_too_large",
                        "out_of_range",
                        "future_time",
                    ]
                )
            ],
        }
        headers["content-type"] = "application/cloudevents+json"
        response = await client.post(
            "/v1/me/usage/events", headers=headers, content=json.dumps(event("single"))
        )
        assert response.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
        response = await client.put(
            "/v1/me/usage/evidence",
            headers=_headers("owner@example.test"),
            json={
                "schema_version": 1,
                "sources": [
                    {"source": "x", "state": "bad"},
                    {"source": "x", "state": "ok", "observed_at": 1},
                ],
            },
        )
        assert response.json()["skipped"] == [
            {"section": "sources", "index": 0, "reason": "invalid_value"},
            {"section": "sources", "index": 1, "reason": "out_of_range"},
        ]


@pytest.mark.parametrize(
    "method,path", [("GET", "events"), ("GET", "sources"), ("POST", "events"), ("PUT", "evidence")]
)
def test_usage_scope_only(method, path):
    path = "/v1/me/usage/" + path
    assert scope_allows("usage", method, path)
    assert not scope_allows("tasks", method, path)
    assert not scope_allows("usage:read", method, path)


def test_retention_configuration():
    assert usage_evidence_retention_days({}) == 400
    assert usage_evidence_retention_days({"usage": {"evidence": {"retention_days": 30}}}) == 30
    for value in (0, -1, True, "30"):
        with pytest.raises(ValueError):
            usage_evidence_retention_days({"usage": {"evidence": {"retention_days": value}}})


@pytest.mark.parametrize(
    "change",
    [
        {"source": "has space"},
        {"source": "1:relative"},
        {"source": "/invalid%GG"},
        {"time": "2026-11-12"},
        {"severity": "change"},
        {"datacontenttype": "text/plain"},
        {"subject": None},
        {"data": None},
        {"data": []},
        {"data_base64": "e30="},
    ],
)
def test_cloudevent_attribute_validation(change):
    from omnigent.usage.evidence import Rejected, parse_cloudevents

    parsed = parse_cloudevents(
        json.dumps(event(**change)).encode(), "application/cloudevents+json"
    )
    assert parsed == [Rejected(index=0, reason="invalid_attribute")]


def test_json_depth_and_schema_version_bounds():
    from omnigent.usage.evidence import EvidenceBody, Rejected, parse_cloudevents

    data = {}
    for _ in range(18):
        data = {"nested": data}
    assert parse_cloudevents(
        json.dumps(event(data=data)).encode(), "application/cloudevents+json"
    ) == [Rejected(index=0, reason="invalid_attribute")]
    for value in (True, "1", 1.0):
        with pytest.raises(ValueError):
            EvidenceBody.model_validate({"schema_version": value})


@pytest.mark.asyncio
async def test_real_key_scope_and_disabled_gate(db_uri):
    import hashlib

    from fastapi import FastAPI

    from omnigent.server.routes.usage_evidence import create_usage_evidence_router
    from omnigent.server.server_config import load_api_keys
    from omnigent.server.usage_evidence import UsageEvidenceService
    from omnigent.stores.usage_evidence_store import UsageEvidenceStore
    from tests.server.routes.test_usage_read_auth import _provider

    token = "synthetic_usage_token_12345678901234567890"
    provider = _provider()
    provider.set_usage_read_account_lookup(lambda _: "generation")
    service = UsageEvidenceService(UsageEvidenceStore(db_uri))
    app = FastAPI()
    app.include_router(create_usage_evidence_router(service, provider), prefix="/v1")
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for scope, expected in (("usage", 200), ("tasks", 403)):
            provider.set_api_keys(
                load_api_keys(
                    {
                        "api_keys": [
                            {
                                "name": "test",
                                "user": "owner",
                                "scopes": [scope],
                                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                            }
                        ]
                    },
                    {},
                )
            )
            for method, suffix, kwargs in [
                ("GET", "events", {}),
                ("GET", "sources", {}),
                ("PUT", "evidence", {"json": {"schema_version": 1}}),
                (
                    "POST",
                    "events",
                    {
                        "content": "[]",
                        "headers": dict(
                            headers, **{"Content-Type": "application/cloudevents-batch+json"}
                        ),
                    },
                ),
            ]:
                response = await client.request(
                    method, "/v1/me/usage/" + suffix, **dict({"headers": headers}, **kwargs)
                )
                assert response.status_code == expected, response.text
    disabled = FastAPI()
    disabled.include_router(
        create_usage_evidence_router(service, provider, enabled=False), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disabled), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/me/usage/events", headers=headers)).status_code == 404
