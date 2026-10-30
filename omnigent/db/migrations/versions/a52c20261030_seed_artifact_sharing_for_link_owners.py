"""Keep existing artifact links open when the external-access default turns off.

Revision ID: a52c20261030
Revises: f3c914a721ab
Create Date: 2026-10-30 00:00:00.000000

An owner without an ``artifact_sharing`` preferences row now reads as
external access off. Owners whose sessions already minted artifact links
(the ``omnigent.artifact_link_key`` label) get an explicit external-on row,
so their existing links keep opening. The owner is resolved as the serve path
does: the session's owner grant, else the nearest parent's within eight hops;
the reserved ``local`` user, which owns every link when no auth provider is
configured, gets a row for each workspace with links.

Additive only: a row is inserted only where none exists, so a re-run and an
owner's own settings are left alone. When the conversation tables live on a
separate database (``conversation_storage_location``) there is nothing to join
here and the revision does nothing. Downgrade keeps the rows: an older server
reads an external-on row exactly as it read no row.
"""

from __future__ import annotations

import json
import secrets
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import encode

revision: str = "a52c20261030"
down_revision: str | None = "f3c914a721ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the serve path's constants at this revision.
_LINK_KEY_LABEL = "omnigent.artifact_link_key"
_SHARING_KEY = "artifact_sharing"
_LOCAL_OWNER = "local"
_LEVEL_OWNER = 4
_OWNER_CHAIN_MAX_HOPS = 8

_LABELS = sa.table(
    "conversation_labels",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("conversation_id"),
    sa.column("key", sa.String(128)),
)
_CONVERSATIONS = sa.table(
    "conversations",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("id"),
    sa.column("parent_conversation_id"),
)
_PERMISSIONS = sa.table(
    "session_permissions",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("user_id", sa.String(128)),
    sa.column("conversation_id"),
    sa.column("level", sa.Integer()),
)
_PREFERENCES = sa.table(
    "preferences",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("user_id", sa.String(128)),
    sa.column("key", sa.String(128)),
    sa.column("value", sa.LargeBinary()),
)


def _session_id(value: object) -> object:
    """Hashable form of a raw 16-byte id (some drivers return ``memoryview``)."""
    return bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else value


def _link_owners(bind: sa.Connection) -> set[tuple[int, str]]:
    """Return ``(workspace_id, owner)`` for every owner of a session with links."""
    linked = [
        (workspace_id, _session_id(conversation_id))
        for workspace_id, conversation_id in bind.execute(
            sa.select(_LABELS.c.workspace_id, _LABELS.c.conversation_id).where(
                _LABELS.c.key == _LINK_KEY_LABEL
            )
        )
    ]
    if not linked:
        return set()
    owners_by_session: defaultdict[tuple[int, object], set[str]] = defaultdict(set)
    for workspace_id, user_id, conversation_id in bind.execute(
        sa.select(
            _PERMISSIONS.c.workspace_id,
            _PERMISSIONS.c.user_id,
            _PERMISSIONS.c.conversation_id,
        ).where(_PERMISSIONS.c.level >= _LEVEL_OWNER)
    ):
        owners_by_session[(workspace_id, _session_id(conversation_id))].add(user_id)
    parents: dict[tuple[int, object], object] = {
        (workspace_id, _session_id(session_id)): _session_id(parent_id)
        for workspace_id, session_id, parent_id in bind.execute(
            sa.select(
                _CONVERSATIONS.c.workspace_id,
                _CONVERSATIONS.c.id,
                _CONVERSATIONS.c.parent_conversation_id,
            ).where(_CONVERSATIONS.c.parent_conversation_id.is_not(None))
        )
    }
    owners: set[tuple[int, str]] = set()
    for workspace_id, session_id in linked:
        owners.add((workspace_id, _LOCAL_OWNER))
        current: object | None = session_id
        for _hop in range(_OWNER_CHAIN_MAX_HOPS):
            found = owners_by_session.get((workspace_id, current))
            if found:
                owners.update((workspace_id, user_id) for user_id in found)
                break
            current = parents.get((workspace_id, current))
            if current is None:
                break
    return owners


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = ("conversation_labels", "conversations", "session_permissions", "preferences")
    if not all(inspector.has_table(table) for table in tables):
        return
    owners = _link_owners(bind)
    if not owners:
        return
    existing = {
        (workspace_id, user_id)
        for workspace_id, user_id in bind.execute(
            sa.select(_PREFERENCES.c.workspace_id, _PREFERENCES.c.user_id).where(
                _PREFERENCES.c.key == _SHARING_KEY
            )
        )
    }
    rows = [
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "key": _SHARING_KEY,
            # The store's record shape; a fresh gate key per owner.
            "value": encode(
                json.dumps(
                    {
                        "external": True,
                        "code_hash": None,
                        "gate_key": secrets.token_urlsafe(32),
                        "allow_comments": True,
                    },
                    separators=(",", ":"),
                )
            ),
        }
        for workspace_id, user_id in sorted(owners - existing)
    ]
    if rows:
        bind.execute(_PREFERENCES.insert(), rows)


def downgrade() -> None:
    """Keep the seeded rows: older servers read external-on like no row."""
