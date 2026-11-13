"""The pricing migration expands schema without altering existing usage."""

import json

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from omnigent.db.utils import get_or_create_engine
from tests.db.test_migration_usage_hours import _alembic_command


@pytest.mark.parametrize("repeats", [False, True])
def test_renewal_downgrade_payload_compatibility(db_uri, repeats):
    from omnigent.stores.usage_pricing_store import UsagePricingStore
    from omnigent.usage.pricing import PricingBaseline, PurchasedCard
    from tests.server.test_usage_pricing_math import card

    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    store = UsagePricingStore(db_uri)
    store.save(
        "rollback@example.test",
        PricingBaseline(
            cards=[card(repeats=repeats)], account_classes={"account-a": "subscription"}
        ),
        known_accounts={"account-a"},
        pools=set(),
    )
    try:
        if repeats:
            with pytest.raises(RuntimeError, match="repeating"):
                _alembic_command(db_uri, migration_engine, "downgrade", "a70c20261113")
            assert "account_classes" in {
                column["name"]
                for column in sa.inspect(engine).get_columns("usage_pricing_defaults")
            }
            assert store.read("rollback@example.test").cards[0].repeats
        else:
            _alembic_command(db_uri, migration_engine, "downgrade", "a70c20261113")
            with engine.connect() as connection:
                payload = connection.execute(
                    sa.text("SELECT payload FROM usage_pricing_cards")
                ).scalar_one()
            # a70's PurchasedCard forbids fields introduced by a71.
            data = json.loads(payload)
            assert "repeats" not in data and "ends_on" not in data
            assert PurchasedCard.model_validate(data).allowance_usd == "100"
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")


def test_pricing_migration_additive_roundtrip(db_uri):
    config = Config()
    config.set_main_option("script_location", "omnigent/db/migrations")
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    lineage = {revision.revision for revision in script.iterate_revisions(heads[0], "base")}
    assert "a71c20261113" in lineage
    for revision, parent in [
        ("a71c20261113", "a70c20261113"),
        ("a70c20261113", "a69c20261113"),
        ("a69c20261113", "a68c20261112"),
    ]:
        assert script.get_revision(revision).down_revision == parent
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a63c20261112")
        assert "usage_pricing_cards" not in sa.inspect(engine).get_table_names()
        assert "usage_account_relations" in sa.inspect(engine).get_table_names()
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        for name in (
            "usage_pricing_defaults",
            "usage_pricing_cards",
            "usage_quota_closings",
            "usage_price_overrides",
        ):
            assert name in sa.inspect(engine).get_table_names()
        amount = next(
            column
            for column in sa.inspect(engine).get_columns("usage_pricing_cards")
            if column["name"] == "retail_amount"
        )
        assert amount["type"].precision == 38
        assert amount["type"].scale == 18
        columns = {
            column["name"]: column
            for column in sa.inspect(engine).get_columns("usage_pricing_cards")
        }
        assert columns["allowance_usd"]["nullable"]
        assert "account_classes" in {
            column["name"] for column in sa.inspect(engine).get_columns("usage_pricing_defaults")
        }
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")


def test_account_price_and_native_ref_migrations(db_uri):
    from omnigent.stores.usage_hours_store import UsageHoursStore
    from omnigent.stores.usage_pricing_store import UsagePricingStore
    from omnigent.usage.pricing import PricingBaseline
    from tests.server.test_usage_pricing_math import card

    engine = get_or_create_engine(db_uri)
    owner = "migration-owner@example.test"
    hours = UsageHoursStore(db_uri)
    hours.add_turn(
        owner,
        "native:codex",
        1800000000,
        "model-a",
        "host-a",
        "unknown",
        True,
        10,
        2,
        1,
        3,
        16,
        0.25,
    )
    pricing = UsagePricingStore(db_uri)
    saved = pricing.save(
        owner, PricingBaseline(cards=[card()]), known_accounts={"account-a"}, pools=set()
    )
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a68c20261112")
        assert "usage_price_overrides" not in sa.inspect(engine).get_table_names()
        assert "price_ref" not in {
            c["name"] for c in sa.inspect(engine).get_columns("usage_hours")
        }
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns("usage_price_overrides")}
        assert columns["read_rate"]["nullable"]
        assert columns["read_rate"]["type"].precision == 38
        assert columns["read_rate"]["type"].scale == 18
        assert next(
            c for c in sa.inspect(engine).get_columns("usage_hours") if c["name"] == "price_ref"
        )["nullable"]
        [kept] = hours.read(owner, 1800000000, 1800003600)
        assert (kept.total_tokens, kept.cost_usd, kept.price_ref) == (16, 0.25, None)
        assert pricing.read(owner) == saved
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
