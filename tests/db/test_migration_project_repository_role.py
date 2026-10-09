"""Tests for the project-repository-role migration (``a28c20261009``).

Upgrade adds ``project_repositories.role`` and backfills one code repository
per project: the one the most primary+enabled bindings reference, tie-broken
by oldest ``created_at`` then smallest ``id``. Downgrade drops the column
only; ``is_primary`` values and every other row stay untouched.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a27c20261009"
_MIGRATION = "a28c20261009"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def _id(seed: str) -> bytes:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).bytes


def _insert_repository(
    conn: sa.Connection,
    *,
    workspace_id: int,
    project_seed: str,
    repository_seed: str,
    created_at: int = 100,
    repository_id: bytes | None = None,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO project_repositories (workspace_id, id, project_id, name,"
            " remote_url, default_branch, context_manifest_path, revision, created_at,"
            " updated_at) VALUES (:ws, :id, :project_id, :name, 'https://e/r.git',"
            " 'main', '.agents/project/manifest.json', 1, :created_at, NULL)"
        ),
        {
            "ws": workspace_id,
            "id": _id(repository_seed) if repository_id is None else repository_id,
            "project_id": _id(project_seed),
            "name": repository_seed,
            "created_at": created_at,
        },
    )


def _insert_binding(
    conn: sa.Connection,
    *,
    workspace_id: int,
    project_seed: str,
    repository_seed: str,
    host_seed: str,
    is_primary: bool,
    enabled: bool,
    repository_id: bytes | None = None,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO project_host_bindings (workspace_id, id, project_id, host_id,"
            " name, is_primary, repository_id, workspace, enabled, revision,"
            " path_verified_at, created_at, updated_at) VALUES (:ws, :id, :project_id,"
            " :host_id, :name, :is_primary, :repository_id, '/opt/work', :enabled, 1,"
            " NULL, 1000, NULL)"
        ),
        {
            "ws": workspace_id,
            "id": _id(f"binding-{host_seed}-{repository_seed}"),
            "project_id": _id(project_seed),
            "host_id": _id(f"host-{host_seed}"),
            "name": host_seed,
            "is_primary": is_primary,
            "repository_id": _id(repository_seed) if repository_id is None else repository_id,
            "enabled": enabled,
        },
    )


def test_single_alembic_head_includes_project_repository_role() -> None:
    """One head, and the role revision is on the way to it."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage, "role migration is not an ancestor of head"
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_backfills_code_repository_and_downgrade_drops_the_column(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'repository-role.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        before_columns = [
            c["name"] for c in sa.inspect(engine).get_columns("project_repositories")
        ]
        assert "role" not in before_columns

        small_id = bytes([1]) * 16
        big_id = bytes([2]) * 16
        with engine.begin() as conn:
            # A: two primary+enabled bindings; B: one, plus disabled and
            # non-primary rows that must not count.
            _insert_repository(conn, workspace_id=0, project_seed="p1", repository_seed="a")
            _insert_repository(
                conn, workspace_id=0, project_seed="p1", repository_seed="b", created_at=200
            )
            for host_seed in ("h1", "h2"):
                _insert_binding(
                    conn,
                    workspace_id=0,
                    project_seed="p1",
                    repository_seed="a",
                    host_seed=host_seed,
                    is_primary=True,
                    enabled=True,
                )
            _insert_binding(
                conn,
                workspace_id=0,
                project_seed="p1",
                repository_seed="b",
                host_seed="h3",
                is_primary=True,
                enabled=True,
            )
            _insert_binding(
                conn,
                workspace_id=0,
                project_seed="p1",
                repository_seed="b",
                host_seed="h4",
                is_primary=True,
                enabled=False,
            )
            _insert_binding(
                conn,
                workspace_id=0,
                project_seed="p1",
                repository_seed="b",
                host_seed="h5",
                is_primary=False,
                enabled=True,
            )
            # A tie on count: smallest id wins even with identical created_at.
            _insert_repository(
                conn,
                workspace_id=0,
                project_seed="p2",
                repository_seed="c1",
                created_at=300,
                repository_id=small_id,
            )
            _insert_repository(
                conn,
                workspace_id=0,
                project_seed="p2",
                repository_seed="c2",
                created_at=300,
                repository_id=big_id,
            )
            _insert_binding(
                conn,
                workspace_id=0,
                project_seed="p2",
                repository_seed="c1",
                host_seed="h6",
                is_primary=True,
                enabled=True,
                repository_id=small_id,
            )
            _insert_binding(
                conn,
                workspace_id=0,
                project_seed="p2",
                repository_seed="c2",
                host_seed="h7",
                is_primary=True,
                enabled=True,
                repository_id=big_id,
            )
            # No primary+enabled binding anywhere: this repository stays related.
            _insert_repository(
                conn, workspace_id=0, project_seed="p3", repository_seed="d", created_at=400
            )
            # Same project id in another tenant: its own winner, scoped by workspace.
            _insert_repository(
                conn, workspace_id=7, project_seed="p1", repository_seed="b", created_at=100
            )
            _insert_binding(
                conn,
                workspace_id=7,
                project_seed="p1",
                repository_seed="b",
                host_seed="h8",
                is_primary=True,
                enabled=True,
            )

        _run(uri, engine, "upgrade", _MIGRATION)

        with engine.connect() as conn:
            roles = {
                (row.workspace_id, row.id): row.role
                for row in conn.execute(
                    sa.text("SELECT workspace_id, id, role FROM project_repositories")
                )
            }
        assert roles[(0, _id("a"))] == "code"
        assert roles[(0, _id("b"))] == "related"
        assert roles[(0, small_id)] == "code"
        assert roles[(0, big_id)] == "related"
        assert roles[(0, _id("d"))] == "related"
        assert roles[(7, _id("b"))] == "code"

        with engine.connect() as conn:
            primaries = {
                (row.workspace_id, row.id): row.is_primary
                for row in conn.execute(
                    sa.text("SELECT workspace_id, id, is_primary FROM project_host_bindings")
                )
            }
        assert primaries[(0, _id("binding-h1-a"))] == 1
        assert primaries[(0, _id("binding-h4-b"))] == 1
        assert primaries[(0, _id("binding-h5-b"))] == 0

        _run(uri, engine, "downgrade", _PREVIOUS)
        after_columns = [c["name"] for c in sa.inspect(engine).get_columns("project_repositories")]
        assert after_columns == before_columns
    finally:
        engine.dispose()
        clear_engine_cache()
