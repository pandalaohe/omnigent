"""Tests for the scheduled_tasks/scheduled_task_runs migration (z6a2b3c4d5e6).

Verifies the migration creates both tables with the expected shape, that
neither carries a database-level foreign key (schema Rule R032 — the
``agent_id`` / ``conversation_id`` / ``scheduled_task_id`` relationships are
application-owned), that ``scheduled_tasks.rrule`` is NOT NULL and the
``scheduled_task_runs.status`` CHECK is enforced, and that a downgrade drops both
tables cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_PREVIOUS_HEAD = "9d820f91deef"
# The revision the scheduled-task binding migration (c4b2d3e4f5a6) revises.
_PRE_BINDING_HEAD = "c1a6e2f4b610"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full migration chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_migration_creates_both_tables(db_engine: Engine) -> None:
    """Both ``scheduled_tasks`` and ``scheduled_task_runs`` exist after migrating to head."""
    tables = set(sa.inspect(db_engine).get_table_names())
    assert "scheduled_tasks" in tables
    assert "scheduled_task_runs" in tables


def test_scheduled_tasks_columns(db_engine: Engine) -> None:
    """``scheduled_tasks`` has the full expected column set."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert cols == {
        "workspace_id",
        "id",
        "name",
        "prompt",
        "rrule",
        "user_id",
        "agent_id",
        "custom_agent_id",
        # Added by a23c20260928 (calling defaults): the owning project and
        # the creator's explicit-null field list.
        "project_id",
        "explicit_null_fields",
        "account_generation",
        "model_override",
        "reasoning_effort",
        "permission_mode",
        "max_cost_usd",
        "workspace",
        "base_branch",
        "execution_target",
        "host_id",
        "timezone",
        "state",
        "last_run_at",
        "last_run_conversation_id",
        "created_at",
        "updated_at",
    }


def test_scheduled_task_runs_columns(db_engine: Engine) -> None:
    """``scheduled_task_runs`` has the full expected column set."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    assert cols == {
        "workspace_id",
        "id",
        "scheduled_task_id",
        "conversation_id",
        "status",
        "scheduled_at",
        "fired_at",
        "finished_at",
        "error",
        "error_code",
    }


def test_workspace_id_leads_primary_keys(db_engine: Engine) -> None:
    """``workspace_id`` is the leading PK member on both tables."""
    insp = sa.inspect(db_engine)
    assert insp.get_pk_constraint("scheduled_tasks")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]
    assert insp.get_pk_constraint("scheduled_task_runs")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]


def test_no_foreign_keys(db_engine: Engine) -> None:
    """Neither table carries a DB-level FK (schema Rule R032).

    ``agent_id`` / ``last_run_conversation_id`` / ``scheduled_task_id`` /
    ``conversation_id`` are plain columns; referential cleanup is
    application-owned. A FK here would violate the repo-wide standard set by
    ``p1a2b3c4d5e6_remove_all_fks``.
    """
    insp = sa.inspect(db_engine)
    assert insp.get_foreign_keys("scheduled_tasks") == []
    assert insp.get_foreign_keys("scheduled_task_runs") == []


def test_expected_indexes(db_engine: Engine) -> None:
    """Both tables expose the indexes that back the read paths."""
    insp = sa.inspect(db_engine)
    scheduled_tasks_idx = {i["name"] for i in insp.get_indexes("scheduled_tasks")}
    assert "ix_scheduled_tasks_user_scope" in scheduled_tasks_idx
    scheduled_tasks_idx_cols = {
        i["name"]: list(i["column_names"]) for i in insp.get_indexes("scheduled_tasks")
    }
    assert scheduled_tasks_idx_cols["ix_scheduled_tasks_user_scope"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    assert "ix_scheduled_tasks_agent_id" not in scheduled_tasks_idx
    # The separate created_at + user_id listing indexes were folded into the
    # single user-scope index above: with the user_id filter pushed into SQL the
    # per-user list is a covered seek, and the boot scan reads whole rows
    # regardless (see migration f4664ca64ea8).
    assert "ix_scheduled_tasks_created_at" not in scheduled_tasks_idx
    assert "ix_scheduled_tasks_user_id" not in scheduled_tasks_idx
    # ix_scheduled_tasks_state was dropped earlier (see migration e5c8b1f4a2d7).
    assert "ix_scheduled_tasks_state" not in scheduled_tasks_idx
    runs_idx = {i["name"] for i in insp.get_indexes("scheduled_task_runs")}
    assert "ix_scheduled_task_runs_scheduled_task_id" in runs_idx
    runs_idx_cols = {
        i["name"]: list(i["column_names"]) for i in insp.get_indexes("scheduled_task_runs")
    }
    assert runs_idx_cols["ix_scheduled_task_runs_scheduled_task_id"] == [
        "workspace_id",
        "scheduled_task_id",
        "scheduled_at",
        "id",
    ]


def test_state_default_on_omitted_insert(db_engine: Engine) -> None:
    """A raw insert omitting ``state`` / ``workspace_id`` / ``execution_target``
    picks up their defaults.

    Only the integer server_defaults (``state`` / ``workspace_id`` /
    ``execution_target``) are exercised here — all are omitted from the insert
    and must fall back to their defaults.
    """
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, "
                " timezone, created_at) "
                "VALUES (X'00000000000000000000000000000de1', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag_1', 'UTC', 1)"
            )
        )
        state, workspace_id, execution_target = conn.execute(
            sa.text(
                "SELECT state, workspace_id, execution_target FROM scheduled_tasks "
                "WHERE id = X'00000000000000000000000000000de1'"
            )
        ).one()
    assert state == 1  # 1 = 'active'
    assert workspace_id == 0
    assert execution_target == 1  # 1 = 'connected_host'


def test_execution_target_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_tasks.execution_target`` CHECK rejects codes outside the set.

    execution_target is a stable int code (see enum_codecs
    SCHEDULED_TASK_EXECUTION_TARGET, codes 1-2); a code outside that range must
    fail the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, agent_id, "
                    " timezone, execution_target, created_at) "
                    "VALUES (X'00000000000000000000000000e6bad0', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 99, 1)"
                )
            )


def test_rrule_accepts_recurring_row(db_engine: Engine) -> None:
    """A row with ``rrule`` set inserts cleanly (the recurring trigger)."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, "
                " timezone, created_at) "
                "VALUES (X'0000000000000000000000000000c40e', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 1)"
            )
        )


def test_rrule_is_not_null(db_engine: Engine) -> None:
    """A row omitting ``rrule`` fails the NOT NULL constraint."""
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, user_id, agent_id, timezone, "
                    " created_at) "
                    "VALUES (X'000000000000000000000000000000e0', 'n', 'p', "
                    "'u', 'ag', 'UTC', 1)"
                )
            )


def test_state_stored_as_smallint(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert "INT" in str(cols["state"]["type"]).upper()


def test_state_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` CHECK rejects codes outside the closed set.

    State is stored as a stable int code (see enum_codecs SCHEDULED_TASK_STATE,
    codes 1-3); a code outside that range must fail the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, agent_id, "
                    " timezone, state, created_at) "
                    "VALUES (X'00000000000000000000000000badc0d', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 99, 1)"
                )
            )


def test_scheduled_task_runs_status_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_task_runs.status`` CHECK rejects codes outside the closed set.

    Status is stored as a stable int code (see enum_codecs
    SCHEDULED_TASK_RUN_STATUS, codes 1-5); a code outside that range must fail
    the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs "
                    "(id, scheduled_task_id, status, scheduled_at) "
                    "VALUES (X'00000000000000000000000000005bad', "
                    "X'00000000000000000000000000000001', 99, 1)"
                )
            )


def test_scheduled_task_runs_status_check_accepts_valid_code(db_engine: Engine) -> None:
    """A valid status code inserts cleanly."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(id, scheduled_task_id, status, scheduled_at) "
                "VALUES (X'000000000000000000000000000000c0', "
                "X'00000000000000000000000000000001', 1, 1)"  # 1 = 'scheduled'
            )
        )
        status = conn.execute(
            sa.text(
                "SELECT status FROM scheduled_task_runs "
                "WHERE id = X'000000000000000000000000000000c0'"
            )
        ).scalar_one()
    assert status == 1


def test_scheduled_task_runs_status_stored_as_smallint(db_engine: Engine) -> None:
    """The ``status`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    assert "INT" in str(cols["status"]["type"]).upper()


def test_library_agent_binding_keeps_legacy_bytes_and_round_trips_ca_ids(
    tmp_path: Path,
) -> None:
    """The additive binding column leaves stored-agent bytes and admits ``ca_`` ids.

    Migration ``c4b2d3e4f5a6`` adds ``custom_agent_id`` and makes ``agent_id``
    nullable instead of retyping the binary column. A row written before the
    migration must read back the same 16 raw bytes, and a saved library Agent
    id must round-trip through the new text column with ``agent_id`` NULL.
    """
    db_path = tmp_path / "library-binding.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    legacy_bytes = bytes.fromhex("ab" * 16)

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PRE_BINDING_HEAD)
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, timezone, created_at) "
                "VALUES (X'00000000000000000000000000000001', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, :agent_id, 'UTC', 1)"
            ),
            {"agent_id": legacy_bytes},
        )
        command.upgrade(config, "head")

    with engine.begin() as conn:
        agent_id, custom_agent_id = conn.execute(
            sa.text(
                "SELECT agent_id, custom_agent_id FROM scheduled_tasks "
                "WHERE id = X'00000000000000000000000000000001'"
            )
        ).one()
        assert agent_id == legacy_bytes
        assert custom_agent_id is None
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, custom_agent_id, "
                " timezone, created_at) "
                "VALUES (X'00000000000000000000000000000002', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, NULL, "
                ":custom_agent_id, 'UTC', 1)"
            ),
            {"custom_agent_id": "ca_1234567890abcdef1234567890abcdef"},
        )
        library_id = conn.execute(
            sa.text(
                "SELECT custom_agent_id FROM scheduled_tasks "
                "WHERE id = X'00000000000000000000000000000002'"
            )
        ).scalar_one()
    assert library_id == "ca_1234567890abcdef1234567890abcdef"

    engine.dispose()
    clear_engine_cache()


def test_agent_binding_check_requires_exactly_one_column(db_engine: Engine) -> None:
    """``ck_scheduled_tasks_agent_binding`` rejects neither and both bindings.

    The store writes exactly one of ``agent_id`` / ``custom_agent_id``; the
    CHECK makes that a database invariant, so a raw write with zero or two
    bindings fails instead of persisting an unlaunchable row.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, timezone, created_at) "
                    "VALUES (X'000000000000000000000000000000b0', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, 'UTC', 1)"
                )
            )
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, agent_id, custom_agent_id, "
                    " timezone, created_at) "
                    "VALUES (X'000000000000000000000000000000b1', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, 'ag', 'ca_0', 'UTC', 1)"
                )
            )


def test_downgrade_drops_library_agent_tasks_and_restores_not_null(tmp_path: Path) -> None:
    """Downgrade deletes ``ca_``-bound tasks with their runs, keeps stored rows.

    A ``ca_`` task has no pre-change representation, so it goes with its run
    history (the store's task deletion drops the same rows). The stored-agent
    row survives, which lets ``agent_id`` go back to NOT NULL.
    """
    db_path = tmp_path / "library-binding-downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    legacy_bytes = bytes.fromhex("cd" * 16)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, timezone, created_at) "
                "VALUES (X'00000000000000000000000000000011', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, :agent_id, 'UTC', 1)"
            ),
            {"agent_id": legacy_bytes},
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(id, scheduled_task_id, status, scheduled_at) "
                "VALUES (X'00000000000000000000000000000021', "
                "X'00000000000000000000000000000011', 1, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, custom_agent_id, "
                " timezone, created_at) "
                "VALUES (X'00000000000000000000000000000012', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, NULL, "
                "'ca_1234567890abcdef1234567890abcdef', 'UTC', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(id, scheduled_task_id, status, scheduled_at) "
                "VALUES (X'00000000000000000000000000000022', "
                "X'00000000000000000000000000000012', 1, 1)"
            )
        )
        config.attributes["connection"] = conn
        command.downgrade(config, _PRE_BINDING_HEAD)

    with engine.begin() as conn:
        tasks = conn.execute(sa.text("SELECT agent_id FROM scheduled_tasks")).scalars().all()
        assert tasks == [legacy_bytes]
        runs = (
            conn.execute(sa.text("SELECT scheduled_task_id FROM scheduled_task_runs"))
            .scalars()
            .all()
        )
        assert runs == [bytes.fromhex("00000000000000000000000000000011")]

    # agent_id is required again: a row with no binding column has nowhere to go.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, timezone, created_at) "
                    "VALUES (X'00000000000000000000000000000013', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, 'UTC', 1)"
                )
            )

    engine.dispose()
    clear_engine_cache()


def test_downgrade_keeps_other_workspaces_runs_with_same_task_id(tmp_path: Path) -> None:
    """Downgrade correlates runs by ``(workspace_id, scheduled_task_id)``.

    Task keys are ``(workspace_id, id)``, so a stored-agent task in a second
    workspace can reuse the library-bound task's id. Only the library-bound
    workspace's task and runs may be deleted; the other workspace's matching-id
    task and its runs must survive.
    """
    db_path = tmp_path / "library-binding-workspaces.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    legacy_bytes = bytes.fromhex("ef" * 16)
    task_id = "X'00000000000000000000000000000031'"

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(workspace_id, id, name, prompt, rrule, user_id, agent_id, "
                " custom_agent_id, timezone, created_at) "
                f"VALUES (1, {task_id}, 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, NULL, "
                "'ca_1234567890abcdef1234567890abcdef', 'UTC', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(workspace_id, id, scheduled_task_id, status, scheduled_at) "
                f"VALUES (1, X'00000000000000000000000000000041', {task_id}, 1, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(workspace_id, id, name, prompt, rrule, user_id, agent_id, "
                " timezone, created_at) "
                f"VALUES (2, {task_id}, 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', NULL, :agent_id, 'UTC', 1)"
            ),
            {"agent_id": legacy_bytes},
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(workspace_id, id, scheduled_task_id, status, scheduled_at) "
                f"VALUES (2, X'00000000000000000000000000000042', {task_id}, 1, 1)"
            )
        )
        config.attributes["connection"] = conn
        command.downgrade(config, _PRE_BINDING_HEAD)

    with engine.begin() as conn:
        tasks = conn.execute(sa.text("SELECT workspace_id, agent_id FROM scheduled_tasks")).all()
        assert tasks == [(2, legacy_bytes)]
        runs = conn.execute(
            sa.text("SELECT workspace_id, scheduled_task_id FROM scheduled_task_runs")
        ).all()
        assert runs == [(2, bytes.fromhex("00000000000000000000000000000031"))]

    engine.dispose()
    clear_engine_cache()


def test_downgrade_drops_both_tables(tmp_path: Path) -> None:
    """Downgrading one step removes both tables; re-upgrade restores them."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    tables = set(sa.inspect(engine).get_table_names())
    assert {"scheduled_tasks", "scheduled_task_runs"} <= tables

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    tables = set(sa.inspect(engine).get_table_names())
    assert "scheduled_tasks" not in tables
    assert "scheduled_task_runs" not in tables

    # Re-upgrade restores both tables — proves the upgrade is replayable.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "z6a2b3c4d5e6")
    tables = set(sa.inspect(engine).get_table_names())
    assert {"scheduled_tasks", "scheduled_task_runs"} <= tables

    engine.dispose()
    clear_engine_cache()
