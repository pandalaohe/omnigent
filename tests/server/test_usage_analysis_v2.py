"""Complete-window and local-calendar analysis through the real reader."""

from datetime import UTC, datetime
from fractions import Fraction
from time import perf_counter
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert
from starlette.datastructures import QueryParams

from omnigent.db.db_models import SqlUsageHour
from omnigent.db.utils import run_write_transaction
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.usage.analysis_filters import AnalysisFilter, local_slot
from omnigent.usage.analysis_v2 import read_analysis_v2
from omnigent.usage.contract import ActivityEntryV2, UsageReportV2
from tests.server import test_usage_activity
from tests.server.test_usage_activity import HOUR, NOW, OWNER

service = test_usage_activity.service


def test_complete_large_windows_equal_brute_force_and_four_chunkings(service, db_uri, monkeypatch):
    filters = AnalysisFilter.parse(
        QueryParams("group=account&range=d90&include=previous&include=rows"), NOW
    )
    current, previous = filters.spans(NOW)
    hours = range(previous.counted_from, int(current.until), 3600)
    for account in ("account-a", "account-b"):
        service.report_account(
            OWNER, account, UsageReportV2(schema_version=2, observed_at=NOW), now=NOW
        )
    rows = [
        {
            "workspace_id": 0,
            "owner_id": OWNER,
            "account_id": account,
            "hour_utc": hour,
            "model": f"model-{model}",
            "host_id": f"host-{model % 2}",
            "basis": "list",
            "priced": 1,
            "input_tokens": 100,
            "cached_tokens": 10,
            "cache_write_tokens": 0,
            "output_tokens": 20,
            "total_tokens": 130,
            "requests": 3,
            "failed": 1,
            "cost_usd": 0.0013,
            "coverage": "complete",
            "updated_at": hour + 1,
        }
        for hour in hours
        for account in ("account-a", "account-b")
        for model in range(4)
    ]
    run_write_transaction(
        service.hours_store._write_session(),
        "test_bulk_hours",
        lambda session: session.execute(insert(SqlUsageHour), rows),
    )
    from omnigent.db.db_models import SqlUsageHourAgent
    from omnigent.server.usage_evidence import UsageEvidenceService
    from omnigent.stores.usage_evidence_store import UsageEvidenceStore
    from omnigent.usage.prices import PriceEstimate, PriceEvidence, PriceRate

    samples = [
        row
        for row in rows[-128:]
        if row["account_id"] == "account-a" and row["model"] == "model-0"
    ]
    parts = [
        {
            **{
                name: row[name]
                for name in (
                    "workspace_id",
                    "owner_id",
                    "account_id",
                    "hour_utc",
                    "model",
                    "host_id",
                    "basis",
                    "priced",
                    "updated_at",
                )
            },
            "agent": agent,
            "input_tokens": count,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "total_tokens": count,
            "requests": 1,
            "failed": 0,
            "cost_usd": cost,
        }
        for row in samples
        for agent, count, cost in (("claude", 30, 0.0003), ("codex", 50, 0.0005))
    ]
    run_write_transaction(
        service.hours_store._write_session(),
        "test_bulk_parts",
        lambda session: session.execute(insert(SqlUsageHourAgent), parts),
    )
    evidence = UsageEvidenceService(UsageEvidenceStore(db_uri))
    evidence.store.upsert_prices(
        OWNER,
        [
            PriceEvidence(
                account=row["account_id"],
                hour=row["hour_utc"],
                model=row["model"],
                estimate=PriceEstimate(
                    state="exact",
                    low_usd="0.0013",
                    high_usd="0.0013",
                    priced_units=130,
                    usage_units=130,
                    as_of=int(NOW),
                    rates=[
                        PriceRate(token_kind=kind, per_token="0.00001")
                        for kind in ("uncached_input", "cache_read", "cache_write", "output")
                    ],
                ),
            )
            for row in samples
        ],
        [],
    )
    original = service.hours_store.read_chunks

    def bounded(owner, start, end, **options):
        for chunk in original(owner, start, end, **options):
            assert len(chunk) <= 2000 or (
                len(chunk) <= 20000 and len({row.hour_utc for row in chunk}) == 1
            )
            yield chunk

    for key in ("d30", "d90"):
        chosen = AnalysisFilter.parse(
            QueryParams(f"group=account&range={key}&include=previous&include=rows"), NOW
        )
        spans = chosen.spans(NOW)
        expected = [
            [row for row in rows if span.counted_from <= row["hour_utc"] < span.until]
            for span in spans
        ]
        assert len(expected[0]) > 5000
        reference = None
        for size in (1, 7, 2000, None):
            if size is None:

                def whole(owner, start, end, **options):
                    yield [
                        row for chunk in original(owner, start, end, **options) for row in chunk
                    ]

                monkeypatch.setattr(service.hours_store, "read_chunks", whole)
            else:
                monkeypatch.setattr(service.hours_store, "read_chunks", bounded)
            started = perf_counter()
            result = read_analysis_v2(
                service, OWNER, chosen, now=NOW, chunk_size=size or 2000, evidence=evidence
            )
            print(
                f"window={key} chunk={size} rows={len(expected[0])} "
                f"seconds={perf_counter() - started:.3f}"
            )
            assert result["complete"] is True
            for actual, source in ((result, expected[0]), (result["previous"], expected[1])):
                assert actual["totals"]["tokens"] == sum(row["total_tokens"] for row in source)
                assert actual["totals"]["requests"] == sum(row["requests"] for row in source)
                assert Fraction(actual["totals"]["api_value"].low) == sum(
                    (Fraction(str(row["cost_usd"])) for row in source), Fraction(0)
                )
                assert (
                    sum(bucket["tokens"] for bucket in actual["series"])
                    == actual["totals"]["tokens"]
                )
            assert sum(day["tokens"] for day in result["days"]) == result["totals"]["tokens"]
            assert sum(cell["tokens"] for cell in result["heat"]) == result["totals"]["tokens"]
            assert all(
                sum(item["tokens"] for item in facet) == result["totals"]["tokens"]
                for facet in result["facets"].values()
            )
            assert len(result["rows"]) == 5000 and result["rows_truncated"]
            if reference is None:
                reference = result
            else:
                assert result == reference


@pytest.mark.parametrize("day,count", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst_day_hours_no_spill_and_previous_alignment(service, day, count):
    filters = AnalysisFilter.parse(
        QueryParams(f"group=host&range=d30&tz=America/New_York&day={day}&include=previous"), NOW
    )
    span, previous = filters.spans(NOW)
    assert local_slot(previous.counted_from, ZoneInfo(filters.tz))[0] < day
    entries = [
        ActivityEntryV2(hour=hour, model="m", host="h", tokens=1, requests=1, failed=0)
        for hour in range(span.counted_from, int(span.until), 3600)
    ]
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, activity=entries), now=NOW
    )
    result = read_analysis_v2(service, OWNER, filters, now=NOW, hours_only=True)
    assert result["complete"] and result["totals"]["tokens"] == count
    [selected] = result["days"]
    assert len(selected["hours"]) == count
    assert sum(hour["tokens"] for hour in selected["hours"]) == selected["tokens"]
    assert all(
        local_slot(hour["start"], ZoneInfo(filters.tz))[0] == day for hour in selected["hours"]
    )
    assert len({hour["label"] for hour in selected["hours"]}) == count


def test_half_hour_and_combined_untagged_filter(service):
    filters = AnalysisFilter.parse(
        QueryParams(
            "group=host&range=d30&day=2026-11-05&tz=Asia/Kolkata&host=h&model=m&agent=&slot=4-12&include=previous"
        ),
        NOW,
    )
    span, _ = filters.spans(NOW)
    entries = [
        ActivityEntryV2(hour=hour, model=model, host=host, tokens=10)
        for hour in range(span.counted_from, int(span.until), 3600)
        for model in ("m", "other")
        for host in ("h", "other")
    ]
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, activity=entries), now=NOW
    )
    result = read_analysis_v2(service, OWNER, filters, now=NOW)
    assert result["filter"]["agent"] == ("",)
    assert result["totals"]["tokens"] == 10
    assert result["totals"]["paid"].state == "unknown"
    assert sum(item["tokens"] for item in result["facets"]["host"]) == 20
    assert sum(item["tokens"] for item in result["groups"]) == 20
    assert result["approx"]
    assert all(":30 " in hour["label"] for day in result["days"] for hour in day["hours"])
    assert sum(cell["tokens"] for cell in result["heat"]) > result["totals"]["tokens"]


@pytest.mark.asyncio
async def test_v2_route_rejections_and_filter_echo(service):
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for query in (
            "group=unknown&range=d30",
            "group=account",
            "group=account&range=h24",
            "group=account&range=d30&dimension=account",
            "group=account&range=d30&pool=p",
            "group=account&range=d30&card=c",
            "group=account&range=d30&selection=[]",
            "group=account&range=d30&exclude=x",
            "group=account&range=d30&slot=1-24",
            "group=account&range=d30&slot=1-00&slot=2-00",
            "group=account&range=d30&tz=Missing/Zone",
            "group=account&range=d30&day=bad",
            "group=account&range=d30&day=2026-02-30",
            "group=account&range=d30&day=2026-11-01&day=2026-11-02",
            "group=account&range=d30&day=2026-11-01&from=2026-11-01",
            "group=account&range=d30&host=" + "a" * 65,
            "group=account&range=d30&account=" + "a" * 65,
            "group=account&range=d30&model=" + "a" * 129,
            "group=account&range=d30&agent=Uppercase",
            "group=account&range=d30&" + "&".join(["host=h"] * 65),
        ):
            response = await client.get("/v1/me/usage/analysis?" + query)
            assert response.status_code == 422, (query, response.text)
        response = await client.get(
            "/v1/me/usage/analysis?group=host&range=d30&host=z&host=a&agent="
        )
        assert response.status_code == 200, response.text
        assert response.json()["filter"]["host"] == ["a", "z"]
        assert response.json()["filter"]["agent"] == [""]
        assert (
            await client.get("/v1/me/usage/analysis/hours?group=host&range=d30")
        ).status_code == 422


def test_scan_budget_is_explicit_and_covers_prepass(service, monkeypatch):
    from types import SimpleNamespace

    from omnigent.usage.analysis_v2 import ScanBudget
    from tests.server.test_usage_pricing_math import baseline, card

    entries = [
        ActivityEntryV2(hour=HOUR - offset * 3600, model="m", tokens=1) for offset in range(2)
    ]
    service.report_account(
        OWNER,
        "account-a",
        UsageReportV2(schema_version=2, observed_at=NOW, activity=entries),
        now=NOW,
    )
    saved = baseline(card(target_id="account-a"))
    service.pricing_store = SimpleNamespace(
        read=lambda owner: saved, class_defaults=lambda *args: {}
    )
    budget = ScanBudget(limit=3)
    monkeypatch.setattr("omnigent.usage.analysis_v2.ScanBudget", lambda: budget)
    result = read_analysis_v2(service, OWNER, AnalysisFilter(group="account", range="d7"), now=NOW)
    assert result["complete"] is False and result["incomplete_reason"] == "scan_over_limit"
    assert budget.scanned == 4


def test_class_rates_and_missing_partitions_remain_distinct():
    from omnigent.usage.analysis_fold import ClassesFold
    from omnigent.usage.prices import PriceEstimate, PriceRate
    from tests.server.test_usage_pricing_math import view

    row = view().rows[0]
    row.price_estimate = PriceEstimate(
        state="range",
        low_usd="1",
        high_usd="2",
        rates=[
            PriceRate(token_kind="uncached_input", per_token="0.000001"),
            PriceRate(token_kind="uncached_input", per_token="0.000002"),
        ],
    )
    fold = ClassesFold()
    fold.add(
        [
            row,
            row.model_copy(
                update={
                    "uncached_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "output_tokens": None,
                }
            ),
        ]
    )
    result = fold.result()
    assert result["items"][0]["cost_low_usd"] == "10"
    assert result["items"][0]["cost_high_usd"] == "20"
    assert result["unpriced_tokens"]["output"] == 5_000_000
    assert result["unsplit_tokens"] == row.tokens


def test_agent_values_coexist_without_changing_unfiltered_money(service):
    from omnigent.usage.contract import AgentPart

    entry = ActivityEntryV2(
        hour=HOUR,
        model="m",
        host="h",
        tokens=100,
        cost_usd=0.1,
        cost_basis="list",
        agents=[
            AgentPart(agent="claude", tokens=30, cost_usd=0.03),
            AgentPart(agent="codex", tokens=50, cost_usd=0.05),
        ],
    )
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, activity=[entry]), now=NOW
    )
    result = read_analysis_v2(service, OWNER, AnalysisFilter(group="agent", range="d7"), now=NOW)
    assert result["totals"]["api_value"].low == "0.1"
    assert {group["key"]: group["tokens"] for group in result["groups"]} == {
        "claude": 30,
        "codex": 50,
        "": 20,
    }
    assert sum(Fraction(group["api_value"].low) for group in result["groups"]) == Fraction("0.1")
    selected = read_analysis_v2(
        service, OWNER, AnalysisFilter(group="agent", range="d7", agent=("codex",)), now=NOW
    )
    assert selected["totals"]["tokens"] == 50 and selected["totals"]["api_value"].low == "0.05"
    used = [hour for day in result["days"] for hour in day["hours"] if hour["tokens"]]
    assert used[0]["top_agent"] == "codex"


def test_native_agent_survives_account_retargeting(service, monkeypatch):
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, product="codex"), now=NOW
    )
    monkeypatch.setattr(service, "_activity_meters", lambda owner: {("codex", "m"): {"plan"}})
    service.hours_store.add_turn(
        OWNER, "native:codex", HOUR, "m", "h", "list", True, 10, 0, 0, 1, 11, 0.25
    )
    result = read_analysis_v2(
        service, OWNER, AnalysisFilter(group="agent", range="d7", account=("plan",)), now=NOW
    )
    assert result["totals"]["tokens"] == 11
    assert [(item["key"], item["tokens"]) for item in result["groups"]] == [("codex", 11)]


def test_card_model_prepass_scope_and_bound(service, monkeypatch):
    from types import SimpleNamespace

    from tests.server.test_usage_pricing_math import baseline, card

    saved = baseline(card(target_id="plan"))
    service.pricing_store = SimpleNamespace(
        read=lambda owner: saved, class_defaults=lambda *args: {}
    )
    entries = [
        ActivityEntryV2(
            hour=HOUR - (index + 1) * 3600,
            model=model,
            host=f"h{index}",
            uncached_input_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=0,
        )
        for index, model in enumerate(("model-a", "model-b"))
    ]
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, activity=entries), now=NOW
    )
    for size in (1, 7, 2000):
        result = read_analysis_v2(
            service, OWNER, AnalysisFilter(group="account", range="d7"), now=NOW, chunk_size=size
        )
        assert (
            result["complete"]
            and result["totals"]["api_value"].reason == "card rates name no model"
        )
    filtered = read_analysis_v2(
        service, OWNER, AnalysisFilter(group="account", range="d7", host=("h0",)), now=NOW
    )
    assert filtered["totals"]["api_value"].low is not None
    saved.cards[0].rates_usd_per_million.model = "model-a"
    named = read_analysis_v2(service, OWNER, AnalysisFilter(group="account", range="d7"), now=NOW)
    assert named["totals"]["api_value"].low is not None
    monkeypatch.setattr("omnigent.usage.analysis_v2.CARD_MODEL_LIMIT", 1)
    bounded = read_analysis_v2(
        service, OWNER, AnalysisFilter(group="account", range="d7"), now=NOW
    )
    assert not bounded["complete"] and bounded["incomplete_reason"] == "card_models_over_limit"


def test_facet_remainder_preserves_known_and_unknown_amounts():
    from omnigent.usage.analysis_fold import bounded_items
    from omnigent.usage.value import Value, exact

    values = [
        {
            "key": str(index),
            "api_value": exact(1, unit="money", cohort_id="test", as_of=1),
            "tokens": 10,
            "requests": 1,
            "failed": 0,
        }
        for index in range(202)
    ]
    result = bounded_items(values)
    assert len(result) == 201 and result[-1]["remainder"]
    assert result[-1]["tokens"] == 20
    assert result[-1]["api_value"].state == "exact" and result[-1]["api_value"].low == "2"
    values[-1]["api_value"] = Value(unit="money", cohort_id="test", as_of=1)
    result = bounded_items(values)
    assert result[-1]["api_value"].state == "unknown" and result[-1]["api_value"].low is None


def test_card_group_payment_scope():
    from omnigent.usage.analysis_fold import AnalysisFold
    from omnigent.usage.pricing_projection import card_model_sets
    from tests.server.test_usage_analysis_presentation import sample

    source, saved = sample()
    models = card_model_sets(source, saved)
    fold = AnalysisFold(
        source.model_copy(update={"rows": []}),
        saved,
        AnalysisFilter(group="card", range="d7"),
        {None: models, "host": models},
    )
    fold.add(source.rows)
    groups = fold.result()["groups"]
    assert {item["key"]: item["paid"].low for item in groups} == {
        "card-a": "100",
        "card-b": "50",
        "card-c": "999",
    }
    assert all(item["tokens"] is None and item["api_value"].state == "unknown" for item in groups)


@pytest.mark.parametrize("kind", ["free", "metered"])
def test_fee_classes_keep_payback_unknown(kind):
    from omnigent.usage.analysis_fold import AnalysisFold
    from omnigent.usage.pricing_projection import card_model_sets
    from tests.server.test_usage_pricing_math import baseline, view

    source, saved = view(), baseline()
    source.account_cost_classes = {"account-a": kind}
    models = card_model_sets(source, saved)
    fold = AnalysisFold(
        source.model_copy(update={"rows": []}),
        saved,
        AnalysisFilter(group="account", range="d7"),
        {None: models, "host": models},
    )
    fold.add(source.rows)
    totals = fold.result()["totals"]
    assert totals["tokens"] == source.rows[0].tokens
    assert totals["paid"].state == totals["payback"].state == "unknown"


def test_attribution_partial_is_folded_before_suppressed_usage():
    from omnigent.usage.analysis_fold import AnalysisFold
    from omnigent.usage.pricing_projection import card_model_sets
    from tests.server.test_usage_pricing_math import baseline, view

    source, saved = view(), baseline()
    models = card_model_sets(source, saved)
    fold = AnalysisFold(
        source.model_copy(update={"rows": []}),
        saved,
        AnalysisFilter(group="account", range="d7"),
        {None: models, "host": models},
    )
    fold.add(
        [
            source.rows[0],
            source.rows[0].model_copy(
                update={
                    "contributes_usage": False,
                    "contributes_money": False,
                    "reason": "unproven_overlap",
                }
            ),
        ],
        partial=True,
    )
    totals = fold.result()["totals"]
    assert totals["coverage"] == "partial"
    assert totals["api_value"].state != "exact" and totals["payback"].state != "exact"


def test_hourly_amounts_are_present():
    from omnigent.usage.analysis_fold import AnalysisFold
    from omnigent.usage.pricing_projection import card_model_sets
    from tests.server.test_usage_pricing_math import baseline, view

    source, saved = view(), baseline()
    models = card_model_sets(source, saved)
    fold = AnalysisFold(
        source.model_copy(update={"rows": []}),
        saved,
        AnalysisFilter(group="account", range="d7"),
        {None: models, "host": models},
    )
    fold.add(source.rows)
    [hour] = [hour for day in fold.result()["days"] for hour in day["hours"] if hour["tokens"]]
    assert hour["totals"]["api_value"].low == "49"
    assert hour["totals"]["tokens"] == source.rows[0].tokens


def test_unsplit_native_usage_matches_unlabelled_pool_price():
    from omnigent.usage.pricing import PricingBaseline
    from omnigent.usage.pricing_projection import value_usage
    from tests.server.test_usage_pricing_math import view

    source = view()
    usage = source.rows[0].model_copy(
        update={
            "account_id": "usage-destination",
            "pool_id": "pool-a",
            "channel": "usage",
            "agent": "codex",
            "attributed": True,
        }
    )
    price = usage.model_copy(
        update={
            "account_id": "price-destination",
            "channel": "list_cost",
            "agent": "",
            "basis": "list",
            "priced": True,
            "cost_usd": 7,
            "contributes_usage": False,
            "contributes_money": True,
        }
    )
    source.rows = [usage, price]
    assert value_usage(source, PricingBaseline())["api_value"].low == "7"


@pytest.mark.parametrize("with_prices", [False, True])
def test_agent_money_overflow_is_unknown_in_analysis_with_integer_split_kept(
    service, db_uri, with_prices
):
    from fastapi.encoders import jsonable_encoder

    from omnigent.server.usage_evidence import UsageEvidenceService
    from omnigent.stores.usage_evidence_store import UsageEvidenceStore
    from omnigent.usage.analysis_presentation import display_values
    from omnigent.usage.contract import AgentPart
    from omnigent.usage.prices import PriceEstimate, PriceEvidence, PriceRate

    service.report_account(
        OWNER,
        "account-a",
        UsageReportV2(
            schema_version=2,
            observed_at=NOW,
            activity=[
                ActivityEntryV2(
                    hour=HOUR,
                    model="m",
                    tokens=100,
                    uncached_input_tokens=100,
                    requests=10,
                    cost_usd=1,
                    cost_basis="list",
                    agents=[
                        AgentPart(
                            agent="codex",
                            tokens=100,
                            uncached_input_tokens=100,
                            requests=10,
                            cost_usd=2,
                        )
                    ],
                )
            ],
        ),
        now=NOW,
    )
    evidence = UsageEvidenceService(UsageEvidenceStore(db_uri)) if with_prices else None
    if evidence:
        evidence.store.upsert_prices(
            OWNER,
            [
                PriceEvidence(
                    account="account-a",
                    hour=HOUR,
                    model="m",
                    estimate=PriceEstimate(
                        state="exact",
                        low_usd="1",
                        high_usd="1",
                        priced_units=100,
                        usage_units=100,
                        rates=[PriceRate(token_kind="uncached_input", per_token="0.01")],
                    ),
                )
            ],
            [],
        )
    result = read_analysis_v2(
        service,
        OWNER,
        AnalysisFilter.parse(QueryParams("group=agent&range=d7&agent=codex&include=rows"), NOW),
        evidence=evidence,
        now=NOW,
    )
    assert result["totals"]["tokens"] == 100 and result["totals"]["requests"] == 10
    assert result["totals"]["api_value"].state == "unknown"
    assert result["groups"][0]["api_value"].state == "unknown"
    assert result["rows"][0].agent == "codex" and result["rows"][0].cost_usd is None
    if with_prices:
        [inputs] = [item for item in result["classes"]["items"] if item["kind"] == "input"]
        assert inputs["tokens"] == 100 and inputs["unpriced_tokens"] == 0
        assert inputs["cost_low_usd"] == inputs["cost_high_usd"] == "1"
    public = jsonable_encoder(display_values(result))
    assert public["totals"]["api_value"]["state"] == "unknown"
    assert public["rows"][0]["cost_usd"] is None
    assert "agent_money_unknown" not in public["rows"][0]
    if with_prices:
        assert public["rows"][0]["price_estimate"]["low_usd"] is None
        assert public["rows"][0]["price_estimate"]["rates"][0]["per_token"] == "0.01"
    unfiltered = read_analysis_v2(
        service, OWNER, AnalysisFilter(group="agent", range="d7"), now=NOW
    )
    assert unfiltered["totals"]["api_value"].low == "1"


@pytest.mark.asyncio
async def test_warning_reaches_report_api(service):
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/me/usage/accounts/plan",
            json={
                "schema_version": 2,
                "observed_at": datetime.now(UTC).timestamp(),
                "activity": [
                    {"hour": HOUR, "tokens": 100, "agents": [{"agent": "codex", "tokens": 101}]}
                ],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 1
    assert response.json()["warnings"][0]["reason"] == "agents_over_total"


def test_payment_free_scopes_share_context_and_skip_reset_display(monkeypatch):
    from omnigent.usage.analysis_fold import AnalysisFold
    from omnigent.usage.pricing import PurchasedCard
    from omnigent.usage.pricing_projection import _PricingFold, card_model_sets
    from tests.server.test_usage_pricing_math import baseline, card, view

    purchase = card(repeats=True, starts_on="2026-01-01")
    source, saved = view(purchase), baseline(purchase)
    source.span = source.span.model_copy(update={"until": source.span.until + 70 * 86400})
    source.account_spans = {"account-a": source.span}
    source.generated_at = source.span.until
    models = card_model_sets(source, saved)

    def no_reset_display(*args):
        pytest.fail("component-free folds must not construct card reset displays")

    monkeypatch.setattr(PurchasedCard, "reset_spans", no_reset_display)
    fold = AnalysisFold(
        source,
        saved,
        AnalysisFilter(group="host", range="d7"),
        {None: models, "host": models},
    )
    for index in range(20):
        fold.add([source.rows[0].model_copy(update={"host_id": str(index), "model": str(index)})])
    assert len(fold.payment_context) > 5
    scopes = [entry["counts"] for entry in fold.groups.values()]
    scopes += [item for facet in fold.facets.values() for item in facet.values()]
    for scope in scopes:
        assert scope.pricing is None
        state = scope.api.stream.gi_frame.f_locals
        assert state["periods"] is fold.payment_context
        assert state["cards"] == state["paid_components"] == []
        assert state["period_values"] == {}
        assert state["ratio_items"] == set()
    totals = fold.result()["totals"]
    plain = _PricingFold(source, saved, card_models=models, include_components=False)
    plain.add(source.rows)
    assert Fraction(totals["api_value"].low) == 20 * Fraction(plain.result()["api_value"].low)


def test_aggregation_scope_bound_is_explicit_and_reported(service, monkeypatch):
    import omnigent.usage.analysis_fold as module
    from omnigent.stores.usage_hours_store import UsageReadLimit
    from tests.server.test_usage_pricing_math import baseline, view

    monkeypatch.setattr(module, "SCOPE_LIMIT", 16)
    source, saved = view(), baseline()
    fold = module.AnalysisFold(
        source, saved, AnalysisFilter(group="host", range="d7"), {None: {}, "host": {}}
    )
    with pytest.raises(UsageReadLimit, match="aggregation_over_limit"):
        for index in range(100):
            fold.add([source.rows[0].model_copy(update={"host_id": str(index)})])
    assert fold.scope_count == 16
    assert sum(len(items) for items in fold.facets.values()) + len(fold.groups) < 16
    fold.result()  # A refused addition must leave a serializable partial fold.

    monkeypatch.setattr(module, "SCOPE_LIMIT", 9)
    entries = [
        ActivityEntryV2(hour=HOUR, model=str(index), host=str(index), tokens=1)
        for index in range(10)
    ]
    service.report_account(
        OWNER, "plan", UsageReportV2(schema_version=2, observed_at=NOW, activity=entries), now=NOW
    )
    response = read_analysis_v2(service, OWNER, AnalysisFilter(group="host", range="d7"), now=NOW)
    assert response["complete"] is False
    assert response["incomplete_reason"] == "aggregation_over_limit"


def test_aggregation_entry_and_payment_period_bounds(monkeypatch):
    import omnigent.usage.analysis_fold as module
    from omnigent.stores.usage_hours_store import UsageReadLimit
    from omnigent.usage.pricing_projection import payment_periods
    from tests.server.test_usage_pricing_math import baseline, card, view

    source, saved = view(), baseline()
    fold = module.AnalysisFold(
        source, saved, AnalysisFilter(group="host", range="d7"), {None: {}, "host": {}}
    )
    monkeypatch.setattr(module, "ACCUMULATOR_LIMIT", fold.accumulator_count + 2)
    fold.reserve()
    fold.reserve()
    with pytest.raises(UsageReadLimit, match="aggregation_over_limit"):
        fold.reserve()
    assert fold.accumulator_count == module.ACCUMULATOR_LIMIT
    purchase = card(repeats=True)
    saved = baseline(purchase)
    long = source.model_copy(
        update={
            "span": source.span.model_copy(update={"until": source.span.until + 90 * 86400}),
            "account_spans": {},
        }
    )
    long.account_spans = {"account-a": long.span}
    long.generated_at = long.span.until
    with pytest.raises(UsageReadLimit, match="aggregation_over_limit"):
        payment_periods(long, saved, limit=2)


@pytest.mark.asyncio
async def test_v2_openapi_repeated_filters_and_required_hours_queries(service):
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    schema = app.openapi()
    for path in ("/v1/me/usage/analysis", "/v1/me/usage/analysis/hours"):
        params = {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}
        for name in ("account", "model", "host", "agent", "include"):
            variants = params[name]["schema"].get("anyOf", [params[name]["schema"]])
            assert any(item.get("type") == "array" for item in variants), (path, name)
        if path.endswith("/hours"):
            assert {name for name, param in params.items() if param["required"]} == {
                "group",
                "range",
                "day",
            }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for query in (
            "range=d7&day=2026-11-05",
            "group=host&day=2026-11-05",
            "group=host&range=d7",
        ):
            assert (await client.get("/v1/me/usage/analysis/hours?" + query)).status_code == 422
        response = await client.get(
            "/v1/me/usage/analysis?group=host&range=d7&account=b&account=a&model=y&model=x"
        )
        assert response.status_code == 200
        assert response.json()["filter"]["account"] == ["a", "b"]
        assert response.json()["filter"]["model"] == ["x", "y"]
