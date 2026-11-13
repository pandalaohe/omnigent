"""Pricing settings use native auth and revisioned durable saves."""

import time

import httpx
import pytest

from omnigent.server.auth import scope_allows
from tests.server.integration.test_global_instructions_routes import (
    _headers,
    _make_user,
)
from tests.server.integration.test_global_instructions_routes import (
    auth_app as authenticated_app,  # noqa: F401
)
from tests.server.test_usage_pricing_math import card


@pytest.mark.asyncio
async def test_settings_auth_save_read_stale_and_invalid_target(request, db_uri):
    app = request.getfixturevalue("authenticated_app")
    _make_user(db_uri, "owner@example.test", is_admin=False)
    _make_user(db_uri, "other@example.test", is_admin=False)
    headers = _headers("owner@example.test")
    path = "/v1/me/usage/pricing-baseline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        empty = await client.get(path, headers=headers)
        assert empty.status_code == 200
        assert empty.headers["cache-control"] == "private, no-store"
        assert empty.json()["defaults"]["default_discount"] == "0.8"
        push = await client.put(
            "/v1/me/usage/accounts/account-a",
            headers=headers,
            json={"schema_version": 2, "observed_at": time.time()},
        )
        assert push.status_code == 200, push.text
        payload = {"revision": 0, "cards": [card().model_dump(mode="json")]}
        saved = await client.put(path, headers=headers, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["cards"][0]["purchase_currency"] == "HKD"
        assert "preview" in saved.json()
        assert (await client.put(path, headers=headers, json=payload)).status_code == 409
        payload["revision"] = 1
        payload["cards"][0]["retail_amount"] = 12.3
        assert (await client.put(path, headers=headers, json=payload)).status_code == 422
        assert (await client.get(path, headers=headers)).json()["revision"] == 1
        assert (await client.get(path, headers=_headers("other@example.test"))).json()[
            "cards"
        ] == []
        analysis = await client.get("/v1/me/usage/analysis", headers=headers)
        assert analysis.status_code == 200
        assert analysis.json()["pricing"]["revision"] == 1
        assert "quota_closings" in analysis.json()
        payload = saved.json()
        preview_payload = {
            "revision": 1,
            "cards": [card(repeats=True, allowance_usd=None).model_dump(mode="json")],
            "account_classes": {"account-a": "subscription"},
        }
        preview = await client.post(path + "/preview", headers=headers, json=preview_payload)
        assert preview.status_code == 200, preview.text
        assert preview.json()["history"][0]["periods"] >= 1
        assert preview.json()["history"][0]["payment"]["low"] == "80"
        assert (await client.get(path, headers=headers)).json()["revision"] == 1
        saved_repeat = await client.put(path, headers=headers, json=preview_payload)
        assert saved_repeat.status_code == 200, saved_repeat.text
        assert saved_repeat.json()["cards"][0]["repeats"]
        preview_payload["revision"] = 2
        preview_payload["account_classes"] = {"account-a": "free"}
        assert (await client.put(path, headers=headers, json=preview_payload)).status_code == 422


def test_usage_keys_cannot_edit_or_read_pricing_settings():
    for scope in ("usage", "usage:read", "tasks"):
        for method in ("GET", "PUT"):
            assert not scope_allows(scope, method, "/v1/me/usage/pricing-baseline")
        assert not scope_allows(scope, "POST", "/v1/me/usage/pricing-baseline/preview")


@pytest.mark.asyncio
async def test_analysis_multi_pick_is_authenticated_bounded_and_never_all_owner_fallback(
    request, db_uri
):
    import json

    app = request.getfixturevalue("authenticated_app")
    _make_user(db_uri, "analysis-owner@example.test", is_admin=False)
    _make_user(db_uri, "analysis-other@example.test", is_admin=False)
    headers = _headers("analysis-owner@example.test")
    path = "/v1/me/usage/analysis"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for key in ("a", "b", "c"):
            response = await client.put(
                f"/v1/me/usage/accounts/account-{key}",
                headers=headers,
                json={"schema_version": 2, "observed_at": time.time(), "source_type": "relay"},
            )
            assert response.status_code == 200
        purchases = [
            card(
                id=f"card-{key}",
                target_id=f"account-{key}",
                retail_amount=amount,
                purchase_currency="USD",
                discount_override="1",
            ).model_dump(mode="json")
            for key, amount in [("a", "100"), ("b", "50"), ("c", "999")]
        ]
        saved = await client.put(
            "/v1/me/usage/pricing-baseline",
            headers=headers,
            json={"revision": 0, "cards": purchases},
        )
        assert saved.status_code == 200, saved.text
        query = {
            "range": "custom",
            "from": "2026-11-02",
            "to": "2026-11-08",
            "dimension": "account",
            "selection": json.dumps(["account-a", "account-b"]),
        }
        assert (await client.get(path, params=query)).status_code == 401
        response = await client.get(path, params=query, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["presentation"]["projection"]["paid"]["low"] == "150"
        query["selection"] = "[]"
        assert (await client.get(path, params=query, headers=headers)).json()["presentation"][
            "projection"
        ]["paid"]["state"] == "unknown"
        query["selection"] = json.dumps(["account-a", "account-b"])
        other = await client.get(
            path, params=query, headers=_headers("analysis-other@example.test")
        )
        assert other.json()["presentation"]["projection"]["paid"]["state"] == "unknown"
        for invalid in ("{}", "not-json", json.dumps(["x"] * 257)):
            query["selection"] = invalid
            assert (await client.get(path, params=query, headers=headers)).status_code == 422
