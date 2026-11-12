"""Price replay, independent replacement and lifecycle on every backend."""

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from omnigent.db.db_models import SqlUsageHour, workspace_scope
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.server.usage_evidence import UsageEvidenceService
from omnigent.stores.usage_evidence_store import UsageEvidenceStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.usage.evidence import EvidenceBody

OWNER = "owner@example.test"


def price(hour: int | None = None, **changes: Any) -> dict[str, Any]:
    return dict(
        account="a", hour=hour or (now_epoch() // 3600 - 1) * 3600, model="model-a", **changes
    )


def report(service, **parts):
    return service.report_sources(OWNER, EvidenceBody(schema_version=1, **parts))


def test_price_replay_parts_aliases_decimal_and_labels(db_uri):
    service = UsageEvidenceService(UsageEvidenceStore(db_uri))
    estimate = {
        "low_usd": "0.10",
        "high_usd": "0.20",
        "state": "range",
        "price_source": "public",
        "price_version": "v1",
        "fetched_at": 1,
        "cache_savings_low_usd": "0.01",
        "cache_savings_high_usd": "0.02",
        "priced_units": 8,
        "usage_units": 10,
    }
    first = price(estimate=estimate)
    for _ in range(2):
        assert report(service, prices=[first])["applied"]["prices"] == 1
    part = {
        "context_class": 0,
        "upstream_host": "api.example.test",
        "calls": 1,
        "prompt_tokens": 2,
        "prompt_present": True,
        "completion_tokens": 3,
        "completion_present": True,
        "cache_read_tokens": 0,
        "cache_read_present": False,
        "cache_write_tokens": 0,
        "cache_write_present": False,
    }
    report(service, prices=[price(partitions=[part])])
    row = service.read_prices(OWNER, detail="hours")["rows"][0]
    assert row["hours"] == 1
    assert Decimal(row["estimate"]["low_usd"]) == Decimal("0.10")
    assert row["hourly"][0]["estimate"]["price_version"] == "v1"
    assert row["partitions"] == [part]
    second = price(
        hour=first["hour"] - 3600,
        estimate={
            "low_usd": "0.20",
            "high_usd": "0.30",
            "state": "range",
            "price_source": "public",
            "price_version": "v2",
            "fetched_at": 2,
        },
    )
    report(service, prices=[second], price_aliases=[{"account": "a", "mappings": {"alias": None}}])
    row = service.read_prices(OWNER)["rows"][0]
    assert Decimal(row["estimate"]["low_usd"]) == Decimal("0.30")
    assert row["estimate"]["priced_units"] is None
    assert len(row["estimate"]["price_sources"]) == 2
    report(
        service, prices=[price(partitions=[])], price_aliases=[{"account": "a", "mappings": {}}]
    )
    result = service.read_prices(OWNER)
    assert result["aliases"][0]["mappings"] == {}
    assert result["rows"][0]["partitions"] == []
    report(service)
    assert service.read_prices(OWNER) == dict(
        result, generated_at=service.read_prices(OWNER)["generated_at"]
    )
    assert service.read_prices("other@example.test")["rows"] == []
    with workspace_scope(9):
        assert service.read_prices(OWNER)["rows"] == []
    with get_or_create_engine(db_uri).connect() as connection:
        assert connection.execute(select(SqlUsageHour)).first() is None
    UsageHoursStore(db_uri).delete_account(OWNER, "a")
    assert service.read_prices(OWNER)["rows"] == []
    assert service.read_prices(OWNER)["aliases"] == []


def test_price_validation_ranges_unknown_and_prune(db_uri):
    service = UsageEvidenceService(UsageEvidenceStore(db_uri))
    invalid = [
        price(estimate={"state": "range", "low_usd": "NaN"}),
        price(estimate={"state": "range", "low_usd": "2", "high_usd": "1"}),
        price(estimate={"state": "partial", "priced_units": 2, "usage_units": 1}),
        price(estimate=None),
    ]
    receipt = report(
        service,
        prices=[
            *invalid,
            price(hour=3600, estimate={"state": "unknown"}),
            price(hour=(now_epoch() // 3600) * 3600, estimate={"state": "unknown"}),
            price(estimate={"state": "unknown"}),
        ],
    )
    assert [v["reason"] for v in receipt["skipped"]] == ["invalid_value"] * 4 + [
        "out_of_range"
    ] * 2
    row = service.read_prices(OWNER)["rows"][0]
    assert row["estimate"]["low_usd"] is None
    report(service, price_aliases=[{"account": "a", "mappings": {}}])
    service.store.prune(
        {"usage_price_evidence": now_epoch() + 1, "usage_price_aliases": now_epoch() + 1}
    )
    assert service.read_prices(OWNER)["rows"] == []
    assert service.read_prices(OWNER)["aliases"] == []
