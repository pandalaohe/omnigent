"""Small, single-database store for browser annotation targets and receiving state."""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlAnnotationReceiver,
    SqlConversation,
    SqlConversationMetadata,
    SqlHost,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.enum_codecs import encode_host_status
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import LEVEL_EDIT
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S


class ReceiverConflict(Exception):
    """The expected receiving revision no longer matches."""


class ReceiverTargetUnavailable(Exception):
    """The target is archived, absent, or lacks an explicit edit grant."""


class InvalidTargetCursor(Exception):
    """The session-list cursor is malformed or belongs to another search."""


@dataclass(frozen=True)
class ReceiverState:
    session_id: str | None
    revision: int
    set_at: int | None


def _editable_session(session: Session, user_id: str, session_id: str) -> int | None:
    return session.execute(
        select(SqlConversation.archive_revision)
        .join(
            SqlSessionPermission,
            and_(
                SqlSessionPermission.workspace_id == SqlConversation.workspace_id,
                SqlSessionPermission.conversation_id == SqlConversation.id,
            ),
        )
        .join(
            SqlConversationMetadata,
            and_(
                SqlConversationMetadata.workspace_id == SqlConversation.workspace_id,
                SqlConversationMetadata.id == SqlConversation.id,
            ),
        )
        .where(
            SqlConversation.workspace_id == current_workspace_id(),
            SqlConversation.id == session_id,
            SqlConversation.archived.is_(False),
            SqlConversation.deletion_claim_token.is_(None),
            SqlConversationMetadata.kind == 1,
            SqlSessionPermission.user_id == user_id,
            SqlSessionPermission.level >= LEVEL_EDIT,
        )
    ).scalar_one_or_none()


def _decode_cursor(cursor: str | None, query: str) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or payload[0] != query
            or not isinstance(payload[1], int)
            or not 0 <= payload[1] < 2**63
            or not isinstance(payload[2], str)
            or len(payload[2]) != 32
            or any(char not in "0123456789abcdef" for char in payload[2])
        ):
            raise ValueError
        return payload[1], payload[2]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as error:
        raise InvalidTargetCursor from error


def _encode_cursor(query: str, updated_at: int, session_id: str) -> str:
    value = json.dumps([query, updated_at, session_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class AnnotationReceiverStore:
    """Uses the same SQLAlchemy engine as accounts, permissions, and conversations."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.annotation_receiver_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.annotation_receiver_store", immediate=True
        )

    def read(self, user_id: str) -> ReceiverState:
        """Clear a stale reference, including one archived and later restored."""

        def read_and_validate(session: Session) -> ReceiverState:
            row = session.get(
                SqlAnnotationReceiver, (current_workspace_id(), user_id), with_for_update=True
            )
            if row is None:
                return ReceiverState(None, 0, None)
            if row.session_id is not None:
                account = session.get(SqlUser, (current_workspace_id(), user_id))
                target = _editable_session(session, user_id, row.session_id)
                if (
                    account is None
                    or account.deleted_at is not None
                    or account.account_generation != row.account_generation
                    or target is None
                    or target != row.session_archive_revision
                ):
                    row.session_id = None
                    row.session_archive_revision = None
                    row.set_at = None
                    row.revision += 1
            return ReceiverState(row.session_id, row.revision, row.set_at)

        return run_write_transaction(
            self._session_immediate, "read_annotation_receiver", read_and_validate
        )

    def is_editable(self, user_id: str, session_id: str) -> bool:
        """Use explicit grants only; administrators do not get an override."""
        with self._session("check_annotation_target") as session:
            return _editable_session(session, user_id, session_id) is not None

    def set(self, user_id: str, session_id: str | None, expected_revision: int) -> ReceiverState:
        """Replace or clear one user's receiving target with a revision guard."""

        def write(session: Session) -> ReceiverState:
            account_generation = require_active_account(session, user_id)
            row = session.get(
                SqlAnnotationReceiver, (current_workspace_id(), user_id), with_for_update=True
            )
            if row is None:
                if expected_revision != 0:
                    raise ReceiverConflict
                try:
                    with session.begin_nested():
                        row = SqlAnnotationReceiver(user_id=user_id, revision=0)
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    row = session.get(
                        SqlAnnotationReceiver,
                        (current_workspace_id(), user_id),
                        with_for_update=True,
                        populate_existing=True,
                    )
            if row is None or row.revision != expected_revision:
                raise ReceiverConflict
            archive_revision = None
            if session_id is not None:
                target = _editable_session(session, user_id, session_id)
                if target is None:
                    raise ReceiverTargetUnavailable
                archive_revision = target
            row.session_id = session_id
            row.account_generation = account_generation
            row.session_archive_revision = archive_revision
            row.revision += 1
            row.set_at = int(time.time()) if session_id is not None else None
            return ReceiverState(row.session_id, row.revision, row.set_at)

        return run_write_transaction(self._session_immediate, "set_annotation_receiver", write)

    def list_targets(
        self, user_id: str, *, query: str = "", cursor: str | None = None, limit: int = 20
    ) -> dict[str, object]:
        """Return one bounded page of explicitly editable active sessions."""
        if not 1 <= limit <= 50 or len(query) > 120:
            raise ValueError("invalid target list bounds")
        position = _decode_cursor(cursor, query)
        receiver = self.read(user_id)
        now = int(time.time())
        with self._session("list_annotation_targets") as session:
            conditions = [
                SqlConversation.workspace_id == current_workspace_id(),
                SqlConversation.archived.is_(False),
                SqlConversation.deletion_claim_token.is_(None),
                SqlConversationMetadata.kind == 1,
                SqlSessionPermission.user_id == user_id,
                SqlSessionPermission.level >= LEVEL_EDIT,
            ]
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                conditions.append(SqlConversation.title.ilike(f"%{escaped}%", escape="\\"))
            if position is not None:
                conditions.append(
                    or_(
                        SqlConversation.updated_at < position[0],
                        and_(
                            SqlConversation.updated_at == position[0],
                            SqlConversation.id < position[1],
                        ),
                    )
                )
            base = (
                select(
                    SqlConversation.id,
                    SqlConversation.title,
                    SqlConversation.updated_at,
                    SqlConversationMetadata.workspace,
                    SqlConversationMetadata.host_id,
                    SqlHost.name.label("host_name"),
                    SqlHost.status.label("host_status"),
                    SqlHost.updated_at.label("host_updated_at"),
                )
                .join(
                    SqlSessionPermission,
                    and_(
                        SqlSessionPermission.workspace_id == SqlConversation.workspace_id,
                        SqlSessionPermission.conversation_id == SqlConversation.id,
                    ),
                )
                .join(
                    SqlConversationMetadata,
                    and_(
                        SqlConversationMetadata.workspace_id == SqlConversation.workspace_id,
                        SqlConversationMetadata.id == SqlConversation.id,
                    ),
                )
                .outerjoin(
                    SqlHost,
                    and_(
                        SqlHost.workspace_id == SqlConversation.workspace_id,
                        SqlHost.host_id == SqlConversationMetadata.host_id,
                    ),
                )
            )
            rows = session.execute(
                base.where(*conditions)
                .order_by(SqlConversation.updated_at.desc(), SqlConversation.id.desc())
                .limit(limit + 1)
            ).all()
            receiving_row = None
            if receiver.session_id is not None:
                receiving_row = session.execute(
                    base.where(
                        SqlConversation.workspace_id == current_workspace_id(),
                        SqlConversation.id == receiver.session_id,
                        SqlConversation.archived.is_(False),
                        SqlConversation.deletion_claim_token.is_(None),
                        SqlConversationMetadata.kind == 1,
                        SqlSessionPermission.user_id == user_id,
                        SqlSessionPermission.level >= LEVEL_EDIT,
                    )
                ).one_or_none()

        def present(row: Any) -> dict[str, object]:
            host_online = (
                row.host_id is not None
                and row.host_status == encode_host_status("online")
                and row.host_updated_at is not None
                and row.host_updated_at >= now - HOST_LIVENESS_TTL_S
            )
            return {
                "id": row.id,
                "title": row.title or "New session",
                "host_name": row.host_name,
                "workspace": row.workspace,
                "available": host_online,
                "reason": None if host_online else "host_offline",
            }

        page = rows[:limit]
        last = page[-1] if len(rows) > limit else None
        return {
            "sessions": [present(row) for row in page],
            "next_cursor": _encode_cursor(query, last.updated_at, last.id) if last else None,
            "receiving_session_id": receiver.session_id,
            "receiving_revision": receiver.revision,
            "receiving_session": present(receiving_row) if receiving_row is not None else None,
        }
