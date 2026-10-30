"""Legacy history remains byte-identical when instruction scopes are added."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import CompressedText, Uuid16
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)

_PREVIOUS = "a30c20261015"
_MIGRATION = "a31c20261019"
_TABLE = "global_instruction_revisions"


def test_instruction_layers_migration_is_in_single_head_chain():
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1
    assert _MIGRATION in {revision.revision for revision in script.walk_revisions()}
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_preserves_all_shared_history_and_dev_downgrade_keeps_it(db_uri: str):
    uri = db_uri
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)

    def run(action, revision):
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            config.attributes["connection"] = connection
            getattr(command, action)(config, revision)
            connection.commit()

    legacy = sa.table(
        _TABLE,
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("text", CompressedText()),
        sa.column("created_us", sa.BigInteger()),
        sa.column("created_by", sa.String(128)),
    )
    history = [
        {
            "workspace_id": 0,
            "id": "1" * 32,
            "text": "  old shared\n正文",
            "created_us": 1_000_001,
            "created_by": "admin@example.test",
        },
        {
            "workspace_id": 0,
            "id": "2" * 32,
            "text": "",
            "created_us": 1_000_002,
            "created_by": None,
        },
        {
            "workspace_id": 22,
            "id": "3" * 32,
            "text": "another workspace",
            "created_us": 1_000_003,
            "created_by": None,
        },
    ]
    try:
        run("downgrade", _PREVIOUS)
        with engine.begin() as connection:
            connection.execute(legacy.insert(), history)
            before = connection.execute(sa.text(f"SELECT * FROM {_TABLE} ORDER BY id")).all()
        run("upgrade", _MIGRATION)
        with engine.connect() as connection:
            after = connection.execute(
                sa.text(
                    "SELECT workspace_id, id, text, created_us, created_by "
                    f"FROM {_TABLE} ORDER BY id"
                )
            ).all()
            assert after == before
            assert connection.execute(
                sa.text(f"SELECT DISTINCT scope, agent_key, host_id FROM {_TABLE}")
            ).all() == [("shared", "", "")]
        indexes = {index["name"]: index for index in sa.inspect(engine).get_indexes(_TABLE)}
        assert "ix_global_instruction_revisions_latest" not in indexes
        assert indexes["ix_global_instruction_revisions_layer_latest"]["column_names"] == [
            "workspace_id",
            "scope",
            "agent_key",
            "host_id",
            "created_us",
            "id",
        ]
        run("upgrade", "head")
        store = SqlAlchemyGlobalInstructionsStore(uri)
        revisions = store.list_revisions()
        assert [(r.id, r.text, r.created_us, r.created_by) for r in revisions] == [
            ("2" * 32, "", 1_000_002, None),
            ("1" * 32, "  old shared\n正文", 1_000_001, "admin@example.test"),
        ]
        assert store.current().text == ""
        store.save("agent", created_by=None, scope="agent", agent_key="codex")
        store.save("host", created_by=None, scope="host", host_id="host-a")
        store.save(
            "independent",
            created_by=None,
            scope="independent",
            agent_key="codex",
            host_id="host-a",
        )
        store.save("mother", created_by=None, scope="mother")
        store.save("child", created_by=None, scope="child")
        if engine.dialect.name == "cockroachdb":

            def interrupt_older_downgrade(
                conn, cursor, statement, parameters, context, executemany
            ):
                if "DROP INDEX ix_session_peer_messages_pair_sequence" in statement:
                    raise RuntimeError("injected older downgrade failure")

            sa.event.listen(engine, "before_cursor_execute", interrupt_older_downgrade)
            try:
                with pytest.raises(RuntimeError, match="injected older downgrade failure"):
                    run("downgrade", "a29c20261010")
            finally:
                sa.event.remove(engine, "before_cursor_execute", interrupt_older_downgrade)
            with engine.connect() as connection:
                assert set(
                    connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    ).scalars()
                ) == {_MIGRATION, "d0a1f879a01d"}
            assert "scope" not in {
                column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
            }
            run("downgrade", "a29c20261010")
            run("upgrade", _PREVIOUS)
        else:
            run("downgrade", _PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text(f"SELECT * FROM {_TABLE} ORDER BY id")).all() == before
            )
        assert {column["name"] for column in sa.inspect(engine).get_columns(_TABLE)} == {
            "workspace_id",
            "id",
            "text",
            "created_us",
            "created_by",
        }
        assert {index["name"] for index in sa.inspect(engine).get_indexes(_TABLE)} == {
            "ix_global_instruction_revisions_latest"
        }
    finally:
        run("upgrade", "head")
        engine.dispose()
        clear_engine_cache()
