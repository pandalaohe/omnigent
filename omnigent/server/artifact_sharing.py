"""External-access gate: per-owner sharing settings, grants and remember cookie.

An artifact link is governed by the *owner's* sharing record — one
``preferences`` row (key :data:`ARTIFACT_SHARING_PREFERENCE_KEY`) holding an
"external access" switch, an optional share-code hash and a visitor-comments
switch. A settings write regenerates a gate key, so a change invalidates every
grant and every remembered browser the record signed; an
``allow_comments``-only write keeps the key, so that switch does not sign
visitors out.

This module owns the record store plus the two credentials layered on the
artifact token: the URL-path grant (``<token>~<grant>``) that lets a page's
sub-resources through a closed gate, and the artifact-path-scoped remember
cookie set by a successful unlock. The HTTP routes and the serve path consume
them; the record itself never leaves the server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import cast

import zstandard
from sqlalchemy import LargeBinary, literal_column, select, type_coerce, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.artifact_links import _b64url_decode, _b64url_encode
from omnigent.server.passwords import InvalidPasswordError, hash_password, verify_password

# The ``preferences`` row key. Server-only: no API route returns the row, and
# the synced preferences envelope (a different column) never sees it.
ARTIFACT_SHARING_PREFERENCE_KEY = "artifact_sharing"

GRANT_TTL_SECONDS = 3600
REMEMBER_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_COOKIE_NAME_PREFIX = "omni_artifact_"
_MAC_BYTES = 16
_GATE_KEY_BYTES = 32
# Guards the read against a corrupted or oversized row; the record is ~200
# bytes, so anything past this is junk and reads as "no row".
_RECORD_MAX_DECODED_BYTES = 4096


@dataclass(frozen=True)
class SharingRecord:
    """One owner's artifact-sharing settings.

    :param external: Whether visitors without an Omnigent identity may open
        the owner's links once they pass the gate.
    :param code_hash: argon2id hash of the share code, or ``None`` when no
        code is set. Never returned to a client.
    :param gate_key: Random secret regenerated on a settings write; signs
        grants and remember cookies, so a settings change invalidates all of
        them. An ``allow_comments``-only write keeps the stored key, so the
        switch does not sign visitors out. Never returned to a client.
    :param allow_comments: Whether visitors holding a link may leave comments
        on the shared page. Defaults to ``True`` when absent.
    """

    external: bool
    code_hash: str | None
    gate_key: str
    allow_comments: bool = True


class KeepShareCode:
    """Marker type for "leave the stored share code alone"."""

    __slots__ = ()


KEEP_SHARE_CODE = KeepShareCode()


def _decode_record(raw: bytes | str | memoryview | None) -> SharingRecord | None:
    """Parse a stored record, tolerating a malformed row as "no row".

    :param raw: Raw ``preferences.value`` bytes as read with
        ``type_coerce(..., LargeBinary)``.
    :returns: The record, or ``None`` when absent, undecodable, or failing
        any field check.
    """
    if raw is None:
        return None
    try:
        decoded = json.loads(decode(raw, max_decoded_bytes=_RECORD_MAX_DECODED_BYTES) or "null")
    except (ValueError, TypeError, RecursionError, zstandard.ZstdError):
        return None
    if not isinstance(decoded, dict):
        return None
    external = decoded.get("external")
    code_hash = decoded.get("code_hash")
    gate_key = decoded.get("gate_key")
    # Absent means the documented default: visitor comments on. A row written
    # before the field existed must keep that default, not fail closed.
    allow_comments = decoded.get("allow_comments", True)
    if not isinstance(external, bool) or not isinstance(gate_key, str) or not gate_key:
        return None
    if code_hash is not None and not isinstance(code_hash, str):
        return None
    if not isinstance(allow_comments, bool):
        return None
    return SharingRecord(
        external=external,
        code_hash=code_hash,
        gate_key=gate_key,
        allow_comments=allow_comments,
    )


def _encode_record(record: SharingRecord) -> str:
    """Serialize a record for the ``preferences`` value column."""
    return json.dumps(
        {
            "external": record.external,
            "code_hash": record.code_hash,
            "gate_key": record.gate_key,
            "allow_comments": record.allow_comments,
        },
        separators=(",", ":"),
    )


def generate_gate_key() -> str:
    """Return a fresh gate key: 32 random bytes, base64url, 43 chars."""
    return secrets.token_urlsafe(_GATE_KEY_BYTES)


def gate_key_id(gate_key: str) -> str:
    """Return the identifier a grant binds to *gate_key*.

    :param gate_key: The owner's current gate key.
    :returns: First 8 hex of sha256(gate_key). An owner with no record (or an
        unresolvable owner) uses ``""`` instead.
    """
    return hashlib.sha256(gate_key.encode("utf-8")).hexdigest()[:8]


def _insert_default_if_absent(session: Session, workspace_id: int, owner: str) -> None:
    """Seed an absent sharing row with the documented default.

    Raced first writes each attempt the insert; exactly one wins and the
    other's conflict clause is a no-op, so both then lock and merge the row
    one of them created. The seeded gate key is replaced by the caller's
    update in the same transaction.

    MySQL uses ON DUPLICATE KEY UPDATE, never INSERT IGNORE: IGNORE turns an
    over-long owner into a silent truncation, and the locked read then misses
    the row it seeded.
    """

    values = {
        "workspace_id": workspace_id,
        "user_id": owner,
        "key": ARTIFACT_SHARING_PREFERENCE_KEY,
        "value": _encode_record(
            SharingRecord(external=True, code_hash=None, gate_key=generate_gate_key())
        ),
    }
    dialect = session.get_bind().dialect.name
    stmt: Insert
    if dialect == "mysql":
        # A PK no-op: duplicate keys stay silent without IGNORE's blanket
        # downgrade of data errors to warnings.
        stmt = (
            mysql_insert(SqlPreference)
            .values(**values)
            .on_duplicate_key_update(user_id=literal_column("user_id"))
        )
    elif dialect == "sqlite":
        stmt = (
            sqlite_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["workspace_id", "user_id", "key"])
        )
    else:
        stmt = (
            pg_insert(SqlPreference)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["workspace_id", "user_id", "key"])
        )
    session.execute(stmt)


class ArtifactSharingStore:
    """Read/write the per-owner sharing row in ``preferences``.

    Built from a conversation store's ``storage_location``; the engine and the
    session makers are created on first use so an app that never touches the
    gate pays nothing. The store calls are sync — routes invoke them through
    ``asyncio.to_thread``, like the other stores.
    """

    def __init__(self, storage_location: str) -> None:
        """Bind the store to a database URI without connecting.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///omnigent.db"``.
        """
        self._storage_location = storage_location
        self._read_sessions: NamedManagedSessionMaker | None = None
        self._write_sessions: NamedManagedSessionMaker | None = None

    def _read_session(self) -> NamedManagedSessionMaker:
        """Return (creating on first use) the read session maker."""
        if self._read_sessions is None:
            self._read_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.artifact_sharing",
            )
        return self._read_sessions

    def _write_session(self) -> NamedManagedSessionMaker:
        """Return (creating on first use) the write session maker."""
        if self._write_sessions is None:
            self._write_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.artifact_sharing",
                immediate=True,
            )
        return self._write_sessions

    def read_sharing(self, owner: str) -> SharingRecord | None:
        """Return *owner*'s settings, or ``None`` when there is no valid row.

        :param owner: User id whose record is read, e.g.
            ``"alice@example.com"`` or the reserved local user.
        :returns: The record, or ``None`` (absent / malformed — both mean the
            documented default: external on, no code).
        """
        with self._read_session()("read_sharing") as session:
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == owner,
                    SqlPreference.key == ARTIFACT_SHARING_PREFERENCE_KEY,
                )
            )
        return _decode_record(raw)

    def write_sharing(
        self,
        owner: str,
        *,
        external: bool | None = None,
        share_code: str | None | KeepShareCode = KEEP_SHARE_CODE,
        allow_comments: bool | None = None,
    ) -> SharingRecord:
        """Merge an update into *owner*'s record and return the new state.

        An absent row is seeded first (insert-if-absent per dialect), then
        read under a row lock, so an absent field keeps its value without a
        lost-update race between concurrent settings writes. The seed matters
        on the first write: without it the locked read matches no row (and
        ``require_active_account`` may take no lock), so two racing first
        writes would each merge against the defaults and the later update
        would replace the other's field. ``share_code=None`` clears the code;
        a string replaces it (hashed here). Every successful write
        regenerates the gate key, except one that carries only
        *allow_comments*: keeping the key on that switch is what stops
        flipping visitor comments from signing every open visitor out.

        :param owner: User id whose record is written.
        :param external: New switch value, or ``None`` to keep the current one.
        :param share_code: New plaintext code, ``None`` to clear, or
            :data:`KEEP_SHARE_CODE` to keep.
        :param allow_comments: New visitor-comments switch, or ``None`` to
            keep the current one (``True`` on a first write).
        :returns: The persisted record.
        :raises RuntimeError: When the seeded row is not the one updated — an
            owner the column cannot store (MySQL truncation) — so callers
            never report a write that did not land.
        """

        def write(session: Session) -> SharingRecord:
            require_active_account(session, owner)
            workspace_id = current_workspace_id()
            _insert_default_if_absent(session, workspace_id, owner)
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary))
                .where(
                    SqlPreference.workspace_id == workspace_id,
                    SqlPreference.user_id == owner,
                    SqlPreference.key == ARTIFACT_SHARING_PREFERENCE_KEY,
                )
                .with_for_update()
            )
            current = _decode_record(raw)
            if isinstance(share_code, str):
                code_hash = hash_password(share_code)
            elif share_code is None:
                code_hash = None
            else:
                # KEEP_SHARE_CODE: the stored hash survives this write.
                code_hash = current.code_hash if current is not None else None
            # Only an allow_comments-only write on an existing row keeps the
            # gate key; every other write rotates it.
            gate_key = generate_gate_key()
            if (
                current is not None
                and external is None
                and isinstance(share_code, KeepShareCode)
                and allow_comments is not None
            ):
                gate_key = current.gate_key
            record = SharingRecord(
                external=(current.external if current is not None else True)
                if external is None
                else external,
                code_hash=code_hash,
                gate_key=gate_key,
                allow_comments=(current.allow_comments if current is not None else True)
                if allow_comments is None
                else allow_comments,
            )
            result = cast(
                "CursorResult[tuple[object]]",
                session.execute(
                    update(SqlPreference)
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == owner,
                        SqlPreference.key == ARTIFACT_SHARING_PREFERENCE_KEY,
                    )
                    .values(value=_encode_record(record))
                ),
            )
            # A fresh gate key makes every write differ; exactly one row must
            # match. Zero means the seed stored a different owner, so fail
            # rather than return a record that was never persisted.
            if result.rowcount != 1:
                raise RuntimeError("artifact sharing write matched no row")
            return record

        return run_write_transaction(self._write_session(), "write_sharing", write)


def _grant_mac(
    session_key: bytes, token: str, expiry_bytes: bytes, gate_key_id_value: str
) -> bytes:
    """The truncated HMAC binding a grant to one token and one gate key."""
    return hmac.new(
        session_key,
        b"grant" + token.encode("utf-8") + expiry_bytes + gate_key_id_value.encode("utf-8"),
        hashlib.sha256,
    ).digest()[:_MAC_BYTES]


def mint_grant(session_key: bytes, token: str, gate_key_id: str, now: int) -> str:
    """Mint a fresh grant for *token* (the bare token, without any suffix).

    :param session_key: The session's current artifact-link key.
    :param token: The token the grant travels with, e.g. ``"a1.eyJ..."``.
    :param gate_key_id: First 8 hex of the owner's gate key, or ``""`` when
        the owner has no record or cannot be resolved.
    :param now: Current unix seconds.
    :returns: ``base64url(expiry) + "." + base64url(mac)``.
    """
    expiry_bytes = (now + GRANT_TTL_SECONDS).to_bytes(4, "big")
    mac = _grant_mac(session_key, token, expiry_bytes, gate_key_id)
    return f"{_b64url_encode(expiry_bytes)}.{_b64url_encode(mac)}"


def grant_valid(
    session_key: bytes,
    token: str,
    grant: str,
    gate_key_id: str,
    now: int,
) -> bool:
    """Whether *grant* is a live, correctly signed grant for *token*.

    :param session_key: The session's current artifact-link key.
    :param token: The bare token the grant claims to accompany.
    :param grant: The grant suffix from the URL.
    :param gate_key_id: First 8 hex of the owner's current gate key, or ``""``.
    :param now: Current unix seconds.
    :returns: ``True`` only for a well-formed, unexpired, matching grant.
    """
    expiry_b64, separator, mac_b64 = grant.partition(".")
    if not separator:
        return False
    try:
        expiry_bytes = _b64url_decode(expiry_b64)
        mac = _b64url_decode(mac_b64)
    except (ValueError, TypeError):
        return False
    if len(expiry_bytes) != 4 or len(mac) != _MAC_BYTES:
        return False
    if int.from_bytes(expiry_bytes, "big") <= now:
        return False
    return hmac.compare_digest(_grant_mac(session_key, token, expiry_bytes, gate_key_id), mac)


def _cookie_mac(gate_key: str, owner: str, expiry: int) -> bytes:
    """The truncated HMAC binding a remember cookie to one owner and gate key."""
    return hmac.new(
        gate_key.encode("utf-8"),
        f"{owner}{expiry}".encode(),
        hashlib.sha256,
    ).digest()[:_MAC_BYTES]


def remember_cookie_name(owner: str, *, secure: bool) -> str:
    """Return the remember-cookie name for *owner*.

    The name carries a hash of the owner so two accounts on one browser keep
    separate cookies; it is distinct from every auth cookie the provider
    reads, so it can never be mistaken for an identity.

    :param owner: User id the cookie remembers.
    :param secure: Whether the request is https (adds the ``__Secure-``
        prefix the browser then enforces).
    :returns: The cookie name.
    """
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:8]
    name = f"{_COOKIE_NAME_PREFIX}{digest}"
    return f"__Secure-{name}" if secure else name


def issue_remember_cookie(gate_key: str, owner: str, now: int) -> str:
    """Mint a remember-cookie value for *owner*.

    :param gate_key: The owner's current gate key.
    :param owner: User id the cookie remembers.
    :param now: Current unix seconds.
    :returns: ``base64url(4-byte big-endian expiry) + "." + base64url(mac)``;
        the MAC input is ``owner + str(expiry)``.
    """
    expiry = now + REMEMBER_COOKIE_MAX_AGE_SECONDS
    mac = _cookie_mac(gate_key, owner, expiry)
    return f"{_b64url_encode(expiry.to_bytes(4, 'big'))}.{_b64url_encode(mac)}"


def remember_cookie_valid(gate_key: str, owner: str, value: str, now: int) -> bool:
    """Whether *value* is a live, correctly signed remember cookie for *owner*.

    :param gate_key: The owner's current gate key (a regenerated key forgets
        every remembered browser).
    :param owner: User id the cookie claims to remember.
    :param value: The cookie value from the request.
    :param now: Current unix seconds.
    :returns: ``True`` only for a well-formed, unexpired, matching cookie.
    """
    expiry_b64, separator, mac_b64 = value.partition(".")
    if not separator:
        return False
    try:
        expiry_bytes = _b64url_decode(expiry_b64)
        mac = _b64url_decode(mac_b64)
    except (ValueError, TypeError):
        return False
    if len(expiry_bytes) != 4 or len(mac) != _MAC_BYTES:
        return False
    expiry = int.from_bytes(expiry_bytes, "big")
    if expiry <= now:
        return False
    return hmac.compare_digest(_cookie_mac(gate_key, owner, expiry), mac)


class UnlockFailureBudget:
    """Failure-only unlock throttle keyed by owner (in-memory, per process).

    ``SlidingWindowRateLimiter`` cannot express this: it counts every call and
    fails open once its key cap is reached. An attempt reserves its slot
    through :meth:`reserve` before the expensive verify, and a success returns
    the slot through :meth:`release`; a correct code therefore never consumes
    budget and parallel attempts cannot all pass the spent check. The key set
    is bounded: at the cap, aged-out keys are dropped, then the oldest key is
    evicted (an evicted owner starts over rather than being refused).
    """

    def __init__(self, *, max_failures: int, window_seconds: float, max_keys: int) -> None:
        """Build a budget of *max_failures* per owner per *window_seconds*.

        :param max_failures: Failed attempts allowed inside the window.
        :param window_seconds: Width of the sliding window, in seconds.
        :param max_keys: Hard cap on tracked owners.
        """
        self._max_failures = max_failures
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def reserve(self, owner: str, now: float) -> bool:
        """Count one attempt against *owner*'s budget before it verifies.

        :param owner: User id the attempt is attributed to.
        :param now: Current wall-clock time, seconds since the epoch.
        :returns: ``True`` once the attempt is counted, ``False`` when the
            budget is already spent and the attempt must be refused.
        """
        if self.exhausted(owner, now):
            return False
        self.record_failure(owner, now)
        return True

    def release(self, owner: str, reserved_at: float) -> None:
        """Return the slot a successful attempt reserved at *reserved_at*.

        :param owner: User id the reservation belongs to.
        :param reserved_at: The ``now`` passed to :meth:`reserve`.
        """
        hits = self._hits.get(owner)
        if hits is None:
            return
        try:
            hits.remove(reserved_at)
        except ValueError:
            return
        if not hits:
            self._hits.pop(owner, None)

    def exhausted(self, owner: str, now: float) -> bool:
        """Whether *owner* has spent the failure budget for the window.

        :param owner: User id the attempts are attributed to.
        :param now: Current wall-clock time, seconds since the epoch.
        :returns: ``True`` when the next attempt must be refused.
        """
        hits = self._recent(owner, now)
        if hits:
            self._hits[owner] = hits
        else:
            self._hits.pop(owner, None)
        return len(hits) >= self._max_failures

    def record_failure(self, owner: str, now: float) -> None:
        """Record one failed attempt for *owner*.

        :param owner: User id the attempt is attributed to.
        :param now: Current wall-clock time, seconds since the epoch.
        """
        if owner not in self._hits and len(self._hits) >= self._max_keys:
            self._evict(now)
        hits = self._recent(owner, now)
        hits.append(now)
        self._hits[owner] = hits

    def _recent(self, owner: str, now: float) -> list[float]:
        """The owner's failures inside the window."""
        cutoff = now - self._window
        return [hit for hit in self._hits.get(owner, ()) if hit > cutoff]

    def _evict(self, now: float) -> None:
        """Drop aged-out owners, then the oldest, to admit a new key."""
        cutoff = now - self._window
        aged_out = [key for key, hits in self._hits.items() if not any(h > cutoff for h in hits)]
        for owner in aged_out:
            self._hits.pop(owner, None)
        while len(self._hits) >= self._max_keys:
            self._hits.pop(next(iter(self._hits)))


def verify_share_code(code: str, code_hash: str) -> bool:
    """Whether *code* matches the stored hash, without leaking the reason.

    :param code: The plaintext code from the unlock form.
    :param code_hash: The stored argon2id hash.
    :returns: ``True`` on a match, ``False`` for a mismatch or a malformed
        stored hash.
    """
    try:
        verify_password(code, code_hash)
    except InvalidPasswordError:
        return False
    return True
