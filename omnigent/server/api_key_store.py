"""Workspace-partitioned personal API keys, serialized with account deletion."""

import hashlib
import secrets
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlUser, SqlUserApiKey, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.entities.user_api_key import UserApiKey

T = TypeVar("T")


class ApiKeyNameTaken(ValueError):
    pass


class _Unset:
    pass


UNSET = _Unset()


def mint_api_key_value() -> str:
    return "omk_" + secrets.token_urlsafe(32)


def _entity(row: SqlUserApiKey) -> UserApiKey:
    return UserApiKey(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        prefix=row.prefix,
        scopes=tuple(row.scopes.split()),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        regenerated_at=row.regenerated_at,
    )


def delete_user_api_keys_in_session(session: Session, user_id: str) -> None:
    session.execute(
        delete(SqlUserApiKey).where(
            SqlUserApiKey.workspace_id == current_workspace_id(),
            SqlUserApiKey.user_id == user_id,
        )
    )


class UserApiKeyStore:
    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.api_key_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.api_key_store", immediate=True
        )

    def _write(self, user_id: str, operation: str, action: Callable[[Session], T]) -> T:
        def write(session: Session) -> T:
            require_active_account(session, user_id)
            return action(session)

        try:
            return run_write_transaction(self._session_immediate, operation, write)
        except IntegrityError as exc:
            # Only the name constraint is a recoverable caller conflict.
            message = str(exc.orig).lower()
            if "ix_user_api_keys_name" in message or "user_api_keys.name" in message:
                raise ApiKeyNameTaken("API key name already used") from None
            raise

    def _owned(self, session: Session, user_id: str, key_id: str) -> SqlUserApiKey | None:
        return session.scalar(
            select(SqlUserApiKey)
            .where(
                SqlUserApiKey.workspace_id == current_workspace_id(),
                SqlUserApiKey.user_id == user_id,
                SqlUserApiKey.id == key_id,
            )
            .with_for_update()
        )

    def create(
        self, user_id: str, *, name: str, scopes: tuple[str, ...], expires_at: int | None, now: int
    ) -> tuple[UserApiKey, str]:
        value = mint_api_key_value()

        def write(session: Session) -> tuple[UserApiKey, str]:
            row = SqlUserApiKey(
                id=secrets.token_hex(16),
                user_id=user_id,
                name=name,
                scopes=" ".join(scopes),
                prefix=value[:12],
                secret_sha256=hashlib.sha256(value.encode()).hexdigest(),
                created_at=now,
                expires_at=expires_at,
                last_used_at=None,
                regenerated_at=None,
            )
            session.add(row)
            session.flush()
            return _entity(row), value

        return self._write(user_id, "create_key", write)

    def list_for_user(self, user_id: str) -> list[UserApiKey]:
        with self._session("list_keys") as session:
            return [
                _entity(row)
                for row in session.scalars(
                    select(SqlUserApiKey)
                    .where(
                        SqlUserApiKey.workspace_id == current_workspace_id(),
                        SqlUserApiKey.user_id == user_id,
                    )
                    .order_by(SqlUserApiKey.created_at, SqlUserApiKey.id)
                )
            ]

    def update(
        self,
        user_id: str,
        key_id: str,
        *,
        name: str | None = None,
        scopes: tuple[str, ...] | None = None,
        expires_at: int | _Unset | None = UNSET,
    ) -> UserApiKey | None:
        def write(session: Session) -> UserApiKey | None:
            row = self._owned(session, user_id, key_id)
            if row is None:
                return None
            if name is not None:
                row.name = name
            if scopes is not None:
                row.scopes = " ".join(scopes)
            if not isinstance(expires_at, _Unset):
                row.expires_at = expires_at
            session.flush()
            return _entity(row)

        return self._write(user_id, "edit_key", write)

    def regenerate(
        self, user_id: str, key_id: str, *, expires_at: int | None, now: int
    ) -> tuple[UserApiKey, str] | None:
        value = mint_api_key_value()

        def write(session: Session) -> tuple[UserApiKey, str] | None:
            row = self._owned(session, user_id, key_id)
            if row is None:
                return None
            row.secret_sha256 = hashlib.sha256(value.encode()).hexdigest()
            row.prefix = value[:12]
            row.regenerated_at = now
            row.last_used_at = None
            row.expires_at = expires_at
            return _entity(row), value

        return self._write(user_id, "regenerate_key", write)

    def delete(self, user_id: str, key_id: str) -> bool:
        def write(session: Session) -> bool:
            row = self._owned(session, user_id, key_id)
            if row is None:
                return False
            session.delete(row)
            return True

        return self._write(user_id, "delete_key", write)

    def resolve(self, secret_sha256: str, now: int) -> tuple[UserApiKey, str] | None:
        with self._session("resolve_key") as session:
            row = session.execute(
                select(SqlUserApiKey, SqlUser.account_generation)
                .join(
                    SqlUser,
                    and_(
                        SqlUser.workspace_id == SqlUserApiKey.workspace_id,
                        SqlUser.id == SqlUserApiKey.user_id,
                    ),
                )
                .where(
                    SqlUserApiKey.workspace_id == current_workspace_id(),
                    SqlUserApiKey.secret_sha256 == secret_sha256,
                    SqlUser.deleted_at.is_(None),
                    or_(SqlUserApiKey.expires_at.is_(None), SqlUserApiKey.expires_at > now),
                )
            ).one_or_none()
            if row is None or row[1] is None:
                return None
            return _entity(row[0]), row[1]

    def touch(self, key_id: str, secret_sha256: str, now: int) -> None:
        def write(session: Session) -> None:
            session.execute(
                update(SqlUserApiKey)
                .where(
                    SqlUserApiKey.workspace_id == current_workspace_id(),
                    SqlUserApiKey.id == key_id,
                    SqlUserApiKey.secret_sha256 == secret_sha256,
                    or_(
                        SqlUserApiKey.last_used_at.is_(None),
                        SqlUserApiKey.last_used_at <= now - 600,
                    ),
                )
                .values(last_used_at=now)
            )

        run_write_transaction(self._session_immediate, "touch_key", write)

    def has_usage_key(self, user_id: str, now: int) -> bool:
        return any(
            (key.expires_at is None or key.expires_at > now)
            and ("*" in key.scopes or "usage" in key.scopes)
            for key in self.list_for_user(user_id)
        )
