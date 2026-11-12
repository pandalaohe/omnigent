"""Selected analysis presentation shares the saved baseline and actual cohorts."""

import json
from fractions import Fraction

import pytest

from omnigent.usage.analysis_presentation import present_analysis
from omnigent.usage.pricing import PricingBaseline, PricingDefaults
from tests.server.test_usage_pricing_math import card, view


def sample():
    purchases = [
        card(
            id=f"card-{key}",
            target_id=f"account-{key}",
            retail_amount=amount,
            purchase_currency="USD",
            discount_override="1",
        )
        for key, amount in [("a", "100"), ("b", "50"), ("c", "999")]
    ]
    source = view(purchases[0], requests=10, failed=0)
    row = source.rows[0]
    source.rows = [
        row.model_copy(
            update={"account_id": purchase.parent, "parent_account_id": purchase.parent}
        )
        for purchase in purchases
    ]
    source.account_spans = {purchase.parent: source.span for purchase in purchases}
    baseline = PricingBaseline(defaults=PricingDefaults(), cards=purchases)
    return source, baseline


def test_multi_account_payment_and_per_item_are_the_selected_population():
    source, baseline = sample()
    result = present_analysis(
        source,
        None,
        baseline,
        dimension="account",
        keys=["account-a", "account-b"],
        excluded=set(),
    )
    assert result["projection"]["paid"]["low"] == "150"
    assert [item["projection"]["paid"]["low"] for item in result["items"]] == ["100", "50"]
    assert {row.parent_account_id for row in result["rows"]} == {"account-a", "account-b"}
    assert result["paid_series"][-1]["value"]["low"] == "150"
    hidden = present_analysis(
        source,
        None,
        baseline,
        dimension="account",
        keys=["account-a", "account-b"],
        excluded={"account-a"},
    )
    assert hidden["projection"]["paid"]["low"] == "50"
    empty = present_analysis(source, None, baseline, dimension="account", keys=[], excluded=set())
    assert empty["projection"]["paid"]["state"] == "unknown"
    assert empty["rows"] == []


def test_two_card_selection_keeps_unsplittable_usage_unknown_and_uses_actual_resets():
    source, baseline = sample()
    result = present_analysis(
        source, None, baseline, dimension="card", keys=["card-a", "card-b"], excluded=set()
    )
    assert result["projection"]["paid"]["low"] == "150"
    assert result["projection"]["tokens"]["state"] == "unknown"
    assert result["projection"]["payback"]["state"] == "unknown"
    assert result["allowance"]["low"] == "200"
    assert result["paid_per_quota"]["formatted"]["low"] == "0.75"
    assert [item["reset_count"] for item in result["cards"]] == [1, 1]


def test_money_is_rounded_half_even_once_after_summing_raw_purchase_allocations():
    source, baseline = sample()
    for purchase in baseline.cards[:2]:
        purchase.retail_amount = "1.005"
    result = present_analysis(
        source,
        None,
        baseline,
        dimension="account",
        keys=["account-a", "account-b"],
        excluded=set(),
    )
    assert result["projection"]["paid"]["low"] == "2.01"
    assert result["projection"]["paid"]["formatted"]["low"] == "2.01"
    assert [item["projection"]["paid"]["formatted"]["low"] for item in result["items"]] == [
        "1.00",
        "1.00",
    ]


def test_pool_identity_includes_parent_and_models_never_borrow_a_shared_payment():
    source, baseline = sample()
    for row in source.rows:
        row.pool_id = "same-pool"
        row.attributed = True
    for purchase in baseline.cards:
        parent = purchase.target_id
        purchase.target_kind, purchase.target_id, purchase.parent_account_id = (
            "pool",
            "same-pool",
            parent,
        )
    result = present_analysis(
        source,
        source,
        baseline,
        dimension="pool",
        keys=[json.dumps(["account-a", "same-pool"])],
        excluded=set(),
    )
    assert result["projection"]["paid"]["low"] == "100"
    assert {row.parent_account_id for row in result["previous"]["rows"]} == {"account-a"}
    model = present_analysis(
        source, source, baseline, dimension="model", keys=[source.rows[0].model], excluded=set()
    )
    assert model["projection"]["paid"]["state"] == "unknown"
    assert model["comparison"]["reason"] == "missing_or_incompatible_payment_or_usage"
    quota_only = present_analysis(
        source,
        source,
        baseline,
        dimension="pool",
        keys=[json.dumps(["account-a", "unreported"])],
        excluded=set(),
    )
    assert quota_only["rows"] == [] and quota_only["previous"]["rows"] == []


def test_previous_comparisons_and_closing_evidence_follow_the_same_selection():
    source, baseline = sample()
    previous = source.model_copy(deep=True)
    for row in previous.rows:
        row.tokens //= 2
        row.uncached_input_tokens //= 2
        row.output_tokens //= 2
        row.cache_read_input_tokens //= 2
        row.cache_creation_input_tokens //= 2
    source.quota_closings = [
        {
            "account_id": "account-a",
            "window_id": "week",
            "observed_at": 100,
            "resets_at": 110,
            "remaining": {
                "state": "range",
                "low": "0",
                "high": "30",
                "unit": "percent",
                "cohort_id": "reading",
                "as_of": 110,
            },
        }
    ]
    result = present_analysis(
        source, previous, baseline, dimension="account", keys=["account-a"], excluded=set()
    )
    assert Fraction(result["comparison"]["same_usage_difference"]["low"]) == 100
    assert result["comparison"]["same_spend_percent"]["formatted"]["low"] == "100.00"
    assert result["quota_closings"][0]["remaining"]["formatted"]["high"] == "30.00"
    other = present_analysis(
        source, previous, baseline, dimension="account", keys=["account-b"], excluded=set()
    )
    assert other["quota_closings"] == []


@pytest.mark.asyncio
async def test_router_reads_multiple_parents_and_previous_without_migration_bootstrap():
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from omnigent.errors import OmnigentError
    from omnigent.server.routes.agent_quota import create_agent_quota_router

    source, baseline = sample()
    reads = []

    def analysis(owner, **options):
        reads.append((owner, options))
        selected = options.get("account_ids")
        return source.model_copy(
            update={
                "rows": [
                    row
                    for row in source.rows
                    if selected is None or row.parent_account_id in selected
                ]
            }
        )

    service = SimpleNamespace(
        usage_enabled=True,
        analysis=analysis,
        pricing_store=SimpleNamespace(
            read=lambda owner: baseline if owner == "owner" else PricingBaseline()
        ),
        quota_closing_store=None,
        pool_store=None,
    )
    auth = SimpleNamespace(get_user_id=lambda request: request.headers.get("x-owner"))
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=auth, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        params = {"dimension": "account", "selection": json.dumps(["account-a", "account-b"])}
        with pytest.raises(OmnigentError):
            await client.get("/v1/me/usage/analysis", params=params)
        response = await client.get(
            "/v1/me/usage/analysis", params=params, headers={"x-owner": "owner"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["presentation"]["projection"]["paid"]["low"] == "150"
        assert len(reads) == 2 and reads[1][1]["previous"]
        assert all(
            owner == "owner" and options["account_ids"] == {"account-a", "account-b"}
            for owner, options in reads
        )
        params["selection"] = "[]"
        assert (
            await client.get("/v1/me/usage/analysis", params=params, headers={"x-owner": "owner"})
        ).json()["presentation"]["projection"]["paid"]["state"] == "unknown"
        for value in ("{}", "invalid", json.dumps(["x"] * 257)):
            params["selection"] = value
            assert (
                await client.get(
                    "/v1/me/usage/analysis", params=params, headers={"x-owner": "owner"}
                )
            ).status_code == 422


def test_mixed_reported_and_unreported_selection_is_not_a_complete_total():
    source, baseline = sample()
    result = present_analysis(
        source,
        source,
        baseline,
        dimension="account",
        keys=["account-a", "unreported"],
        excluded=set(),
    )
    assert result["partial"]
    assert result["projection"]["tokens"]["state"] == "unknown"
    assert result["projection"]["payback"]["state"] == "unknown"
    assert result["projection"]["paid"]["state"] == "partial"
    assert result["projection"]["paid"]["low"] == "100"


def test_partial_previous_population_is_not_compared_to_the_current_cohort():
    source, baseline = sample()
    previous = source.model_copy(deep=True)
    previous.rows = previous.rows[:1]
    result = present_analysis(
        source,
        previous,
        baseline,
        dimension="account",
        keys=["account-a", "account-b"],
        excluded=set(),
    )
    assert result["previous"]["partial"]
    assert result["comparison"]["reason"] == "missing_or_incompatible_payment_or_usage"
