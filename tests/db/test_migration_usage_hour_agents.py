"""Agent-table recovery drops splits and preserves the authoritative main key."""

import time

import pytest
import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine
from tests.db.test_migration_usage_hours import _alembic_command


def test_agent_migration_roundtrip(db_uri):
    engine = get_or_create_engine(db_uri)
    migration = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    key = sa.inspect(engine).get_pk_constraint("usage_hours")["constrained_columns"]
    for table in ("usage_hours", "usage_hour_agents"):
        column = next(
            item for item in sa.inspect(engine).get_columns(table) if item["name"] == "cost_usd"
        )
        print(f"{engine.dialect.name} {table}.cost_usd: {column['type']}")
    from omnigent.stores.usage_hours_store import UsageHoursStore, hours_of
    from omnigent.usage.contract import ActivityEntryV2, AgentPart

    store = UsageHoursStore(db_uri)
    hour = hours_of(int(time.time())) - 3600
    store.replace_rows(
        "owner@example.test",
        "plan",
        [
            ActivityEntryV2(
                hour=hour, tokens=2, cost_usd=0.5, agents=[AgentPart(agent="codex", tokens=1)]
            )
        ],
    )
    try:
        assert sa.inspect(engine).get_pk_constraint("usage_hour_agents")[
            "constrained_columns"
        ] == [*key, "agent"]
        _alembic_command(db_uri, migration, "downgrade", "a71c20261113")
        assert "usage_hour_agents" not in sa.inspect(engine).get_table_names()
        assert sa.inspect(engine).get_pk_constraint("usage_hours")["constrained_columns"] == key
        [preserved] = store.read("owner@example.test", hour, hour + 3600)
        assert preserved.total_tokens == 2 and preserved.cost_usd == 0.5
        _alembic_command(db_uri, migration, "upgrade", "head")
        assert sa.inspect(engine).get_pk_constraint("usage_hour_agents")[
            "constrained_columns"
        ] == [*key, "agent"]
    finally:
        _alembic_command(db_uri, migration, "upgrade", "head")
    [preserved] = next(store.read_chunks("owner@example.test", hour, hour + 3600))
    assert preserved.total_tokens == 2 and preserved.cost_usd == 0.5 and not preserved.agent_parts


@pytest.mark.parametrize(
    ("target", "incompatible", "missing_allowance", "reason"),
    [
        ("a70c20261113", {"repeats": True}, False, "repeating"),
        ("-2", {"repeats": True, "ends_on": "2026-11-03"}, False, "repeating"),
        ("base", {}, True, "without allowance"),
    ],
)
def test_mysql_deep_downgrade_refuses_before_ddl_and_keeps_stamp(
    db_uri, target, incompatible, missing_allowance, reason
):
    from omnigent.stores.usage_pricing_store import UsagePricingStore
    from omnigent.usage.pricing import PricingBaseline
    from tests.server.test_usage_pricing_math import card

    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "mysql":
        pytest.skip("MySQL DDL commits before a later revision can refuse")
    pricing = UsagePricingStore(db_uri)
    pricing.save(
        "owner@example.test",
        PricingBaseline(
            cards=[card(**incompatible)], account_classes={"account-a": "subscription"}
        ),
        known_accounts={"account-a"},
        pools=set(),
    )
    if missing_allowance:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE usage_pricing_cards SET allowance_usd=NULL WHERE owner_id=:owner"),
                {"owner": "owner@example.test"},
            )
    with engine.connect() as connection:
        before = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    ddl = []

    def capture_ddl(connection, cursor, statement, parameters, context, executemany):
        if context.isddl or statement.lstrip().upper().startswith(("ALTER ", "DROP ", "CREATE ")):
            ddl.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture_ddl)
    try:
        with pytest.raises(RuntimeError, match=reason):
            _alembic_command(db_uri, engine, "downgrade", target)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture_ddl)
    assert ddl == []
    assert "usage_hour_agents" in sa.inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == before
        )
    try:
        recovery = "-1" if target == "-2" else "a71c20261113"
        _alembic_command(db_uri, engine, "downgrade", recovery)
        assert "usage_hour_agents" not in sa.inspect(engine).get_table_names()
    finally:
        _alembic_command(db_uri, engine, "upgrade", "head")
    assert "usage_hour_agents" in sa.inspect(engine).get_table_names()
