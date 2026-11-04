"""Additive retry schema preserves existing data and refuses receipt destruction."""

import importlib
import io
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.subagent_retry import (
    SqlRetryChain,
    SqlRetryControl,
    SqlRetryFinal,
    SqlRetryInput,
    SqlRetryOwner,
    SqlRetryTerminal,
)
from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.entities.subagent_retry import RetryOutcome
from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.subagent_retry_store.sqlalchemy_store import SqlAlchemySubagentRetryStore

_MODELS = (
    SqlRetryOwner,
    SqlRetryControl,
    SqlRetryChain,
    SqlRetryTerminal,
    SqlRetryInput,
    SqlRetryFinal,
)
_MIGRATION = "omnigent.db.migrations.versions.a51c20261029_add_subagent_retry_journal"


def test_upgrade_is_additive_and_matches_registered_metadata(db_uri):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    command.downgrade(config, "f3c914a721ab")
    before = set(sa.inspect(engine).get_table_names())
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(users.insert().values(workspace_id=0, id="preserved", is_admin=False))
    command.upgrade(config, "a51c20261029")
    command.upgrade(config, "a51c20261029")
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) - before == {model.__tablename__ for model in _MODELS}
    for model in _MODELS:
        assert next(iter(model.__table__.primary_key.columns.keys())) == "workspace_id"
        assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(
            model.__table__.c.keys()
        )
        assert not inspector.get_foreign_keys(model.__tablename__)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(users.c.id).where(users.c.id == "preserved"))
            == "preserved"
        )


@pytest.mark.parametrize("model", _MODELS)
def test_downgrade_refuses_any_retained_journal_table(db_uri, model):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation(runner_id="owner")
    child = conversations.create_conversation(parent_conversation_id=parent.id)
    store = SqlAlchemySubagentRetryStore(db_uri)
    owner = store.claim_owner(parent.id, "owner", expected_epoch=0)
    input_id = uuid4().hex
    store.record_input(
        child.id,
        input_id=input_id,
        turn_id="failed-turn",
        expected_generation=0,
        payload={"text": "original"},
    )
    terminal = TerminalEnvelope.from_data(
        child.id,
        parent_id=parent.id,
        status="failed",
        data={"turn_id": "failed-turn", "input_id": input_id, "input_generation": 1},
        error={"message": "retained error"},
    )
    chain = store.open_chain(terminal, owner=owner, limit=3, observed_at=100).chain
    store.finalize(
        chain.chain_id,
        owner=owner,
        expected_version=chain.version,
        outcome=RetryOutcome(status="failed", reason="host_offline", output="retained result"),
        wake_payload={"type": "message", "data": {"role": "user", "content": []}},
    )
    # Leave one real receipt family populated to exercise every guard branch.
    with engine.begin() as connection:
        for other in _MODELS:
            if other is not model:
                connection.execute(other.__table__.delete())
        count = connection.scalar(sa.select(sa.func.count()).select_from(model))
        assert count > 0
    with pytest.raises(RuntimeError, match="retain its additive schema"):
        command.downgrade(config, "f3c914a721ab")
    assert {entry.__tablename__ for entry in _MODELS} <= set(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(model)) == count


@pytest.mark.parametrize("backend", ["sqlite", "postgresql", "mysql", "cockroachdb"])
def test_migration_upgrade_compiles_for_supported_backends(backend):
    if backend == "cockroachdb":
        pytest.importorskip(
            "sqlalchemy_cockroachdb", reason="Optional dialect is installed in the release DB lane"
        )
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=backend, opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        importlib.import_module(_MIGRATION).upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE") == 6
    assert "UNIQUE (workspace_id, child_id, root_turn_id)" in sql
    assert "UNIQUE (workspace_id, chain_id, kind, attempt)" in sql
    assert "DROP " not in sql and "ALTER TABLE" not in sql
    assert "FOREIGN KEY" not in sql
    if backend == "mysql":
        assert "BINARY(16)" in sql and "BLOB" not in sql
