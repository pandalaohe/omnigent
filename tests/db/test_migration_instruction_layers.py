"""Legacy history remains byte-identical when instruction scopes are added."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.db_models import CompressedText, Uuid16
from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)

_PREVIOUS = "a30c20261015"
_MIGRATION = "a31c20261019"
_TABLE = "global_instruction_revisions"


def test_instruction_layers_migration_is_only_head():
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert script.get_heads() == [_MIGRATION]
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_preserves_all_shared_history_and_dev_downgrade_keeps_it(tmp_path: Path):
    uri = f"sqlite:///{tmp_path / 'instruction-layers.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)

    def run(action, revision):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            getattr(command, action)(config, revision)

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
        run("upgrade", _PREVIOUS)
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
        engine.dispose()
        clear_engine_cache()
