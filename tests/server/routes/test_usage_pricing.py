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


def test_usage_keys_cannot_edit_or_read_pricing_settings():
    for scope in ("usage", "usage:read", "tasks"):
        for method in ("GET", "PUT"):
            assert not scope_allows(scope, method, "/v1/me/usage/pricing-baseline")
