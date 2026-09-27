"""Move synced user preferences from the users envelope into preferences rows.

Revision ID: a21c20260927
Revises: c4b2d3e4f5a6
Create Date: 2026-09-27 00:00:00.000000

The web preferences store now keeps one ``settings.<namespace>`` row per
namespace in the shared ``preferences`` table, next to a ``settings.version``
marker row (the row store in ``omnigent/server/user_preferences_store.py``).
Before this revision each user's whole envelope lived in the
``users.preferences`` column (``f7a1b2c3d4e5``).

This migration copies every non-NULL envelope into the settings rows so the
row store reads exist; the column itself is deliberately left untouched (a
rollback to an image without the row store still finds it). A user whose
``settings.version`` row already exists is skipped, so re-running is
idempotent and never overwrites rows a client wrote. A batch that keeps
failing after three attempts fails the migration instead of stamping the
revision, so no user is left without rows; the retry that follows a fix
resumes from the users still missing a ``settings.version`` row.

Only a version-1 envelope with an object ``settings`` moves. Unknown
namespace keys are dropped; an undecodable or malformed envelope is skipped
with a warning; one namespace whose stored value would exceed the 65,535-byte
BLOB cap is skipped with a warning while its siblings still move.

Downgrade deletes every ``settings.*`` row. The pre-move envelopes survive in
``users.preferences``, so a rollback loses only writes made after the move.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa
import zstandard
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.sql import Executable

from omnigent.db.compression import decode, encode

revision: str = "a21c20260927"
down_revision: str | None = "c4b2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)

_SETTINGS_KEY_PREFIX = "settings."
_ENVELOPE_VERSION_KEY = f"{_SETTINGS_KEY_PREFIX}version"
_ENVELOPE_VERSION = 1
_MAX_STORED_VALUE_BYTES = 65_535
_BATCH_SIZE = 500

# Frozen copy of the store's allowlist at move time: the legacy envelope can
# only hold namespaces the old store accepted.
_NAMESPACES = frozenset(
    {
        "keyboard_shortcuts",
        "mobile_assistant",
        "session_navigation",
        "context_indicator",
        "usage_context",
        "agent_badges",
        "approval_timeout",
        "agent_pins",
    }
)

_BINARY = sa.LargeBinary().with_variant(MEDIUMBLOB(), "mysql")
_USERS = sa.table(
    "users",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("id", sa.String(128)),
    sa.column("preferences", _BINARY),
)
_PREFERENCES = sa.table(
    "preferences",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("user_id", sa.String(128)),
    sa.column("key", sa.String(128)),
    sa.column("value", sa.LargeBinary()),
)


def _begin_sqlite_transaction(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        # Legacy sqlite3 transaction control otherwise commits DDL and outermost savepoints.
        bind.exec_driver_sql("BEGIN")


def _publish_crdb_changes(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        # Publish a batch's durable copies before reading the next one.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _copy_with_retries(
    bind: sa.Connection, statements: Sequence[Executable], *, operation: str
) -> None:
    is_crdb = bind.dialect.name == "cockroachdb"
    for attempt in range(3):
        try:
            if is_crdb:
                # Serialization failures require a new transaction, including failures at commit.
                if not bind.in_transaction():
                    bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                for statement in statements:
                    bind.execute(statement)
                bind.commit()
            else:
                # PostgreSQL needs rollback to the savepoint before another statement can run.
                with bind.begin_nested():
                    for statement in statements:
                        bind.execute(statement)
            return
        except sa.exc.SQLAlchemyError:
            if is_crdb:
                bind.rollback()
            if attempt == 2:
                _logger.warning(
                    "Could not %s user preference rows after 3 attempts; "
                    "failing the migration so these users keep their pre-move envelopes",
                    operation,
                    exc_info=True,
                )
                raise
            time.sleep(0.1 * (2**attempt))


def _load_batch(
    bind: sa.Connection, cursor: tuple[int, str] | None
) -> list[tuple[int, str, bytes | str | memoryview]]:
    """Read one keyset page of users that still carry an envelope."""
    statement = (
        sa.select(_USERS.c.workspace_id, _USERS.c.id, _USERS.c.preferences)
        .where(_USERS.c.preferences.is_not(None))
        .order_by(_USERS.c.workspace_id, _USERS.c.id)
        .limit(_BATCH_SIZE)
    )
    if cursor is not None:
        workspace_id, user_id = cursor
        statement = statement.where(
            sa.or_(
                _USERS.c.workspace_id > workspace_id,
                sa.and_(_USERS.c.workspace_id == workspace_id, _USERS.c.id > user_id),
            )
        )
    return [(row[0], row[1], row[2]) for row in bind.execute(statement)]


def _initialized_pairs(
    bind: sa.Connection, users: Sequence[tuple[int, str, bytes | str | memoryview]]
) -> set[tuple[int, str]]:
    """The batch's (workspace_id, user_id) pairs that already own a version row."""
    if not users:
        return set()
    candidates = {(workspace_id, user_id) for workspace_id, user_id, _ in users}
    user_ids = {user_id for _, user_id, _ in users}
    rows = bind.execute(
        sa.select(_PREFERENCES.c.workspace_id, _PREFERENCES.c.user_id).where(
            _PREFERENCES.c.key == _ENVELOPE_VERSION_KEY,
            _PREFERENCES.c.user_id.in_(user_ids),
        )
    )
    return {
        (workspace_id, user_id)
        for workspace_id, user_id in rows
        if (workspace_id, user_id) in candidates
    }


def _encode_value(value: object) -> str:
    """The store's canonical text for one namespace row (frozen for replay)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _settings_rows(
    workspace_id: int, user_id: str, envelope: object
) -> list[tuple[int, str, str, bytes]]:
    """One user's rows, or [] when the envelope does not qualify."""
    if not isinstance(envelope, dict):
        _logger.warning(
            "Skipping preferences move for user %s in workspace %s: envelope is not an object",
            user_id,
            workspace_id,
        )
        return []
    version = envelope.get("version")
    if type(version) is not int or version != _ENVELOPE_VERSION:
        _logger.warning(
            "Skipping preferences move for user %s in workspace %s: envelope version is not 1",
            user_id,
            workspace_id,
        )
        return []
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        _logger.warning(
            "Skipping preferences move for user %s in workspace %s: settings is not an object",
            user_id,
            workspace_id,
        )
        return []
    rows: list[tuple[int, str, str, bytes]] = [
        (
            workspace_id,
            user_id,
            _ENVELOPE_VERSION_KEY,
            encode(_encode_value(_ENVELOPE_VERSION)) or b"",
        )
    ]
    for namespace, value in settings.items():
        if namespace not in _NAMESPACES:
            continue
        try:
            stored = encode(_encode_value(value)) or b""
        except (TypeError, ValueError) as exc:
            _logger.warning(
                "Skipping preferences namespace %s for user %s in workspace %s: %s",
                namespace,
                user_id,
                workspace_id,
                exc,
            )
            continue
        if len(stored) > _MAX_STORED_VALUE_BYTES:
            _logger.warning(
                "Skipping preferences namespace %s for user %s in workspace %s: "
                "stored value exceeds %d bytes",
                namespace,
                user_id,
                workspace_id,
                _MAX_STORED_VALUE_BYTES,
            )
            continue
        rows.append((workspace_id, user_id, f"{_SETTINGS_KEY_PREFIX}{namespace}", stored))
    return rows


def upgrade() -> None:
    bind = op.get_bind()
    _begin_sqlite_transaction(bind)
    if not sa.inspect(bind).has_table("preferences"):
        return
    if "preferences" not in {column["name"] for column in sa.inspect(bind).get_columns("users")}:
        return

    cursor: tuple[int, str] | None = None
    while True:
        users = _load_batch(bind, cursor)
        if not users:
            return
        cursor = (users[-1][0], users[-1][1])
        initialized = _initialized_pairs(bind, users)
        moves: list[tuple[int, str, str, bytes]] = []
        for workspace_id, user_id, raw in users:
            if (workspace_id, user_id) in initialized:
                continue
            try:
                text = decode(raw, max_decoded_bytes=4 * 1024 * 1024)
                envelope = json.loads(text or "")
            except (TypeError, ValueError, json.JSONDecodeError, zstandard.ZstdError) as exc:
                _logger.warning(
                    "Skipping preferences move for user %s in workspace %s: %s",
                    user_id,
                    workspace_id,
                    exc,
                )
                continue
            moves.extend(_settings_rows(workspace_id, user_id, envelope))
        if moves:
            statements: list[Executable] = [
                _PREFERENCES.insert().values(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    key=key,
                    value=value,
                )
                for workspace_id, user_id, key, value in moves
            ]
            _copy_with_retries(bind, statements, operation="move")
        _publish_crdb_changes(bind)


def downgrade() -> None:
    """Delete the moved settings rows; the envelopes stay in ``users``."""
    bind = op.get_bind()
    _begin_sqlite_transaction(bind)
    if not sa.inspect(bind).has_table("preferences"):
        return
    op.execute(_PREFERENCES.delete().where(_PREFERENCES.c.key.like(f"{_SETTINGS_KEY_PREFIX}%")))
