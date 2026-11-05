"""Source selection, restart, failures and the owner-scoped Models join."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from omnigent.llms.context_window import ModelPricing, fetch_model_pricing_with_provider
from omnigent.llms.price_source import PriceMap, install_price_map
from omnigent.onboarding import providers
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.usage_models import usage_models
from omnigent.server.usage_price_source import PriceSourceSelection, UsagePriceSource, fetch_prices
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from tests.server.integration.test_global_instructions_routes import auth_app as auth_app
from tests.server.test_usage_activity import (
    HOUR,
    NOW,
    OWNER,
    _headers,
    _make_user,
    report,
)
from tests.server.test_usage_activity import service as service

URL = "https://prices.example.test/map.json"
PAYLOAD = {"vendor/model": {"input_cost_per_token": 2e-6, "output_cost_per_token": 6e-6}}
CUSTOM = PriceSourceSelection(source="custom", format="litellm", url=URL)


@pytest.fixture
def price_source(service, tmp_path, monkeypatch):
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda _: None)
    install_price_map(None)
    yield UsagePriceSource(service.snapshot_store, service.hours_store, tmp_path)
    install_price_map(None)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/prices",
        "https://name:pass@example.test/prices",
        "https://name@example.test/prices",
        "https:///missing",
        "https://example.test/prices#fragment",
    ],
)
def test_custom_requires_https_without_credentials(url):
    with pytest.raises(ValidationError):
        PriceSourceSelection(source="custom", format="litellm", url=url)


@pytest.mark.parametrize(
    "query",
    [
        "api_key=made-up",
        "API-KEY=made-up",
        "access_token=made-up",
        "password=made-up",
        "X-Amz-Credential=made-up",
        "X-Amz-Signature=made-up",
        "%61pi_key=made-up",
        "jwt=made-up",
        "bearer=made-up",
        "sort_key=name",
        "edition=public",
        "",
    ],
)
def test_custom_rejects_any_query_string(query):
    with pytest.raises(ValidationError, match="cannot contain a query string"):
        PriceSourceSelection(source="custom", format="litellm", url=f"{URL}?{query}")


@pytest.mark.asyncio
async def test_plain_public_url_is_accepted(price_source, respx_mock):
    route = respx_mock.get(URL).respond(200, json=PAYLOAD)
    status = await price_source.select(CUSTOM)
    assert route.call_count == 1
    assert status["url"] == URL
    assert status["model_count"] == 1


@pytest.mark.asyncio
async def test_fetch_does_not_follow_a_redirect_adding_a_query(respx_mock):
    target = respx_mock.get(f"{URL}?edition=made-up").respond(200, json=PAYLOAD)
    source = respx_mock.get(URL).respond(302, headers={"location": f"{URL}?edition=made-up"})
    with pytest.raises(ValueError, match="HTTP 302"):
        await fetch_prices(CUSTOM)
    assert source.call_count == 1
    assert target.call_count == 0


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_empty_invalid_and_oversized_maps(respx_mock, monkeypatch):
    route = respx_mock.get(URL)
    for response in [
        httpx.Response(302, headers={"location": "https://other.example.test/map"}),
        httpx.Response(200, json={}),
        httpx.Response(200, content=b"not json"),
    ]:
        route.return_value = response
        with pytest.raises(ValueError):
            await fetch_prices(CUSTOM)
    monkeypatch.setattr("omnigent.server.usage_price_source._MAX_BYTES", 10)
    route.return_value = httpx.Response(200, content=b" " * 11)
    with pytest.raises(ValueError, match="10 MB"):
        await fetch_prices(CUSTOM)


@pytest.mark.asyncio
async def test_selection_freezes_priced_history_fills_closed_only_and_restarts(
    price_source, service, respx_mock, monkeypatch
):
    monkeypatch.setattr("omnigent.server.usage_price_source.time.time", lambda: NOW)
    hours = service.hours_store
    for hour, priced, cost in [(HOUR - 7200, True, 9), (HOUR - 7200, False, 0), (HOUR, False, 0)]:
        hours.add_turn(
            OWNER, "native:codex", hour, "model", "", "metered", priced, 100, 0, 0, 0, 100, cost
        )
    route = respx_mock.get(URL).respond(200, json=PAYLOAD)
    await price_source.select(CUSTOM)
    rows = hours.read(OWNER, HOUR - 7200, HOUR + 1)
    assert [(r.hour_utc, r.priced, r.cost_usd) for r in rows] == [
        (HOUR - 7200, True, pytest.approx(9.0002)),
        (HOUR, False, 0),
    ]
    await price_source.select(CUSTOM)
    assert hours.read(OWNER, HOUR - 7200, HOUR + 1) == rows
    restored = UsagePriceSource(price_source.snapshots, hours, price_source.cache_path.parent)
    install_price_map(None)
    restored.load()
    assert fetch_model_pricing_with_provider("model") == ModelPricing(2e-6, 6e-6)
    assert route.call_count == 2  # loading the cache needed no request
    await restored.select(PriceSourceSelection())
    assert fetch_model_pricing_with_provider("model") is None
    assert hours.read(OWNER, HOUR - 7200, HOUR + 1) == rows


@pytest.mark.asyncio
async def test_failed_daily_read_keeps_last_map_and_becomes_an_issue(
    price_source, service, respx_mock, monkeypatch
):
    clock = [NOW]
    monkeypatch.setattr("omnigent.server.usage_price_source.time.time", lambda: clock[0])
    route = respx_mock.get(URL).respond(200, json=PAYLOAD)
    await price_source.select(CUSTOM)
    await price_source.refresh_if_due()
    assert route.call_count == 1
    clock[0] += 86400
    route.respond(500)
    await price_source.refresh_if_due()
    assert price_source.error
    clock[0] += 1
    assert fetch_model_pricing_with_provider("model") == ModelPricing(2e-6, 6e-6)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(
            service, auth_provider=None, host_store=None, price_source=price_source
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/me/usage/issues")).json()["price_source"][0]["error"]
        failed = await client.put(
            "/v1/me/usage/price-source", json={"source": "custom", "format": "litellm", "url": URL}
        )
        assert failed.status_code == 422
        assert (await client.get("/v1/me/usage/price-source")).json()["fetched_at"] == NOW


def test_models_join_keeps_stale_owned_hosts_and_scopes_product(service, db_uri, monkeypatch):
    monkeypatch.setattr("omnigent.server.agent_quota.time.time", lambda: NOW)
    monkeypatch.setattr(
        "omnigent.server.usage_models.fetch_model_pricing_with_provider",
        lambda *args, **kwargs: ModelPricing(2e-6, 6e-6),
    )
    service.host_store = HostStore(db_uri)
    catalogs = HostModelCatalogCacheStore(db_uri)
    own, foreign = "a" * 32, "b" * 32
    service.host_store.upsert_on_connect(own, "My laptop", OWNER)
    service.host_store.upsert_on_connect(foreign, "Foreign laptop", "other@example.test")
    catalogs.upsert(
        own, "codex", [{"id": "model", "source": {"label": "CLI config", "name": "Gateway"}}], 100
    )
    catalogs.mark_error(own, "codex", "offline")
    catalogs.upsert(own, "claude-native", [{"id": "wrong-product"}], 100)
    catalogs.upsert(foreign, "codex", [{"id": "secret-model"}], 100)
    report(
        service,
        "gateway",
        product="codex",
        models=[{"id": "reported"}],
        activity=[{"hour": HOUR, "model": "used-only", "tokens": 100, "cost_usd": 1}],
    )
    result = usage_models(service, catalogs, OWNER, "gateway", host_id=own, now=NOW)
    assert [row["id"] for row in result["models"]] == ["model", "reported", "used-only"]
    source = result["models"][0]["sources"][0]
    assert source["label"] == "This host" and source["stale"]
    assert source["configuration"]["name"] == "Gateway"
    assert result["models"][2]["usage"]["cost_usd"] == 1
    with pytest.raises(LookupError):
        usage_models(service, catalogs, "other@example.test", "gateway", now=NOW)


def test_models_read_uses_catalog_caches_without_downloading(service, tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    monkeypatch.setattr(providers, "_catalog_cache_root", lambda: tmp_path / "catalog")
    providers._catalog_cache.clear()
    requests = []

    def download(*args, **kwargs):
        requests.append(args)
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", download)
    report(service, product="codex", models=[{"id": "openai/review-model"}])
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    try:
        result = usage_models(service, None, OWNER, "gateway", now=NOW)
        assert result["models"][0]["pricing"] is None
        assert not requests
        providers._write_disk_catalog(
            providers._DiskCatalogEntry(
                catalog={
                    "schema_version": "1.0",
                    "models": {
                        "review-model": {
                            "pricing": {
                                "input_per_million_tokens": 2,
                                "output_per_million_tokens": 6,
                            }
                        }
                    },
                },
                source_url=providers._catalog_source_url("openai"),
                fetched_at=NOW - 7200,
            ),
            "openai",
        )
        monkeypatch.setattr(providers, "_catalog_now", lambda: NOW)
        result = usage_models(service, None, OWNER, "gateway", now=NOW)
        assert result["models"][0]["pricing"]["input_per_token"] == 2e-6
        assert not requests
    finally:
        providers._catalog_cache.clear()


def test_models_read_honors_configured_provider_prices_before_selected_map(service, monkeypatch):
    config = {
        "providers": {
            "free": {
                "kind": "local",
                "default": True,
                "openai": {
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "test",
                    "pricing": {"input_per_million": 0, "output_per_million": 0},
                },
            }
        }
    }
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    report(service, product="codex", models=[{"id": "paid-table-model"}])
    install_price_map(PriceMap({"paid-table-model": ModelPricing(2e-6, 6e-6)}))
    try:
        result = usage_models(service, None, OWNER, "gateway", now=NOW)
        assert result["models"][0]["pricing"]["input_per_token"] == 0
        assert result["models"][0]["pricing"]["output_per_token"] == 0
    finally:
        install_price_map(None)


@pytest.mark.asyncio
async def test_routes_enforce_admin_and_owner_and_deployment_gate(request, db_uri, monkeypatch):
    app = request.getfixturevalue("auth_app")
    _make_user(db_uri, OWNER, is_admin=False)
    _make_user(db_uri, "admin@example.test", is_admin=True)
    monkeypatch.setattr("omnigent.server.agent_quota.time.time", lambda: NOW)
    report(app.state.agent_quota_service, owner=OWNER, models=[{"id": "private"}])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/price-source"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=_headers(OWNER))).json()["can_edit"] is False
        assert (
            await client.put(path, json={"source": "builtin"}, headers=_headers(OWNER))
        ).status_code == 403
        assert (
            await client.put(
                path, json={"source": "builtin"}, headers=_headers("admin@example.test")
            )
        ).status_code == 200
        assert (
            await client.get("/v1/me/usage/models?account=gateway", headers=_headers(OWNER))
        ).status_code == 200
        assert (
            await client.get(
                "/v1/me/usage/models?account=gateway", headers=_headers("admin@example.test")
            )
        ).status_code == 404
        assert (
            await client.get(
                "/v1/me/usage/models?account=gateway&tz=unknown", headers=_headers(OWNER)
            )
        ).status_code == 422
        app.state.agent_quota_service.usage_enabled = False
        assert (await client.get(path, headers=_headers(OWNER))).status_code == 404
