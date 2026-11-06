"""Unknown usage survives upgrade; downgrade restores the former non-null schema."""

import io

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.migrations.versions import a54c20261106_usage_report_v2 as migration
from tests.db.test_migration_usage_hours import _alembic_command


def test_report_v2_sqlite_roundtrip_preserves_native_and_coalesces_unknowns(tmp_path):
    uri = f"sqlite:///{tmp_path / 'report.db'}"
    engine = sa.create_engine(uri)
    _alembic_command(uri, engine, "upgrade", "zb1c2d3e4f5a")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO usage_hours (workspace_id, owner_id, account_id, hour_utc, model, "
                "host_id, basis, priced, input_tokens, total_tokens, updated_at) "
                "VALUES (0, 'owner', 'native:codex', 3600, 'm', '', 'metered', 1, 10, 12, 1)"
            )
        )
    _alembic_command(uri, engine, "upgrade", "a54c20261106")

    with engine.begin() as conn:
        assert conn.execute(
            sa.text("SELECT input_tokens, total_tokens FROM usage_hours")
        ).one() == (10, 12)
        conn.execute(
            sa.text(
                "INSERT INTO usage_hours (workspace_id, owner_id, account_id, hour_utc, model, "
                "host_id, basis, priced, input_tokens, cached_tokens, cache_write_tokens, "
                "output_tokens, total_tokens, coverage, updated_at) "
                "VALUES (0, 'owner', 'pushed', 3600, '', '', 'billed', 1, NULL, NULL, NULL, "
                "NULL, NULL, 'complete', 1)"
            )
        )
    _alembic_command(uri, engine, "downgrade", "zb1c2d3e4f5a")
    with engine.connect() as conn:
        assert conn.execute(
            sa.text(
                "SELECT input_tokens, total_tokens FROM usage_hours WHERE account_id = 'pushed'"
            )
        ).one() == (0, 0)
        assert conn.execute(
            sa.text(
                "SELECT input_tokens, total_tokens FROM usage_hours "
                "WHERE account_id = 'native:codex'"
            )
        ).one() == (10, 12)
    assert all(
        not c["nullable"]
        for c in sa.inspect(engine).get_columns("usage_hours")
        if c["name"] in {"input_tokens", "total_tokens"}
    )
    _alembic_command(uri, engine, "upgrade", "a54c20261106")


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_report_v2_mysql_ddl_keeps_token_defaults(direction):
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(migration, direction)()
    modifications = [line for line in output.getvalue().splitlines() if " MODIFY " in line]
    assert len(modifications) == 5
    assert all("DEFAULT '0'" in line or "DEFAULT 0" in line for line in modifications)
