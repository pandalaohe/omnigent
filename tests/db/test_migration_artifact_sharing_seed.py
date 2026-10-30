"""Tests for the artifact-sharing seed migration (``a52c20261030``).

Upgrade gives every owner of a session with artifact links, and the reserved
``local`` user, an explicit external-on ``artifact_sharing`` row, so existing
links keep opening once an absent row reads as external off. Owners resolve
through the session's owner grant or its parent chain; existing rows and
owners without links are left alone. Downgrade keeps the rows.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.compression import decode
from omnigent.db.utils import _build_alembic_config

_PREVIOUS = "f3c914a721ab"
_MIGRATION = "a52c20261030"
_LINK_KEY = "omnigent.artifact_link_key"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def _id(seed: str) -> bytes:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).bytes


def _insert_session(
    conn: sa.Connection,
    seed: str,
    *,
    parent: str | None = None,
    owner: str | None = None,
    linked: bool = False,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO conversations (workspace_id, id, root_conversation_id,"
            " parent_conversation_id, created_at, updated_at, archive_revision,"
            " archive_close_requested_revision) VALUES (0, :id, :id, :parent, 1, 1, 1, 1)"
        ),
        {"id": _id(seed), "parent": _id(parent) if parent else None},
    )
    if owner is not None:
        conn.execute(
            sa.text(
                "INSERT INTO session_permissions (workspace_id, user_id, conversation_id,"
                " level) VALUES (0, :user, :id, 4)"
            ),
            {"user": owner, "id": _id(seed)},
        )
    if linked:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_labels (workspace_id, conversation_id, key, value,"
                " updated_at) VALUES (0, :id, :key, 'secret', 1)"
            ),
            {"id": _id(seed), "key": _LINK_KEY},
        )


def _sharing_rows(engine: sa.Engine) -> dict[str, dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT user_id, value FROM preferences WHERE key = 'artifact_sharing'")
        ).all()
    return {user_id: json.loads(decode(value) or "null") for user_id, value in rows}


def test_single_alembic_head_includes_the_sharing_seed() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_seeds_external_on_for_link_owners_only(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'sharing-seed.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        with engine.begin() as conn:
            # alice: her own session minted links.
            _insert_session(conn, "a", owner="alice@example.com", linked=True)
            # bob: links minted in a sub-agent session two hops below his.
            _insert_session(conn, "b", owner="bob@example.com")
            _insert_session(conn, "b-child", parent="b")
            _insert_session(conn, "b-grandchild", parent="b-child", linked=True)
            # carol: owns a session without links.
            _insert_session(conn, "c", owner="carol@example.com")
            # dave: links, but he already chose external off.
            _insert_session(conn, "d", owner="dave@example.com", linked=True)
            conn.execute(
                sa.text(
                    "INSERT INTO preferences (workspace_id, user_id, key, value)"
                    " VALUES (0, 'dave@example.com', 'artifact_sharing', :value)"
                ),
                {"value": b'{"external":false,"code_hash":null,"gate_key":"k"}'},
            )

        _run(uri, engine, "upgrade", _MIGRATION)
        rows = _sharing_rows(engine)

        assert set(rows) == {"alice@example.com", "bob@example.com", "dave@example.com", "local"}
        for owner in ("alice@example.com", "bob@example.com", "local"):
            assert rows[owner]["external"] is True
            assert rows[owner]["code_hash"] is None
            assert rows[owner]["allow_comments"] is True
            assert isinstance(rows[owner]["gate_key"], str) and rows[owner]["gate_key"]
        assert rows["dave@example.com"]["external"] is False
        assert rows["alice@example.com"]["gate_key"] != rows["bob@example.com"]["gate_key"]

        _run(uri, engine, "downgrade", _PREVIOUS)
        assert _sharing_rows(engine) == rows
    finally:
        engine.dispose()


def test_upgrade_without_links_writes_nothing(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'sharing-seed-empty.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        with engine.begin() as conn:
            _insert_session(conn, "a", owner="alice@example.com")

        _run(uri, engine, "upgrade", _MIGRATION)

        assert _sharing_rows(engine) == {}
    finally:
        engine.dispose()
