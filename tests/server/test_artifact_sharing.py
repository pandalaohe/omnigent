"""Unit tests for the external-access gate's record, grant, cookie and budget.

The store exercises the real ``preferences`` table through a migrated SQLite
database; grants, remember cookies and the unlock failure budget are pure
functions over their wire forms. One full-app test pins the boundary that no
settings or current-user response carries the stored hash or gate key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server import artifact_sharing
from omnigent.server.artifact_sharing import (
    ARTIFACT_SHARING_PREFERENCE_KEY,
    GRANT_TTL_SECONDS,
    REMEMBER_COOKIE_MAX_AGE_SECONDS,
    ArtifactSharingStore,
    SharingRecord,
    UnlockFailureBudget,
    gate_key_id,
    grant_valid,
    issue_remember_cookie,
    mint_grant,
    remember_cookie_name,
    remember_cookie_valid,
    verify_share_code,
)
from omnigent.server.passwords import hash_password

_SESSION_KEY = b"\x11" * 32
_OTHER_KEY = b"\x22" * 32
_TOKEN = "a1.eyJzIjoiY29udiJ9.bWFj"


# ── Record store ────────────────────────────────────────────────────


class _RecordingSession:
    """Session stub capturing executed statements for one dialect.

    ``_insert_default_if_absent`` only reads the bind's dialect name and
    executes the statement, so no connection is needed to inspect the
    compiled MySQL form.
    """

    def __init__(self, dialect: str) -> None:
        """Record statements as if bound to *dialect*."""
        self._dialect_name = dialect
        self.statements: list[Any] = []

    def get_bind(self) -> Any:
        """Report a bind whose dialect is the configured one."""
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect_name))

    def execute(self, statement: Any) -> None:
        """Capture one executed statement."""
        self.statements.append(statement)


def test_mysql_seed_insert_is_not_ignore_and_noops_on_conflict() -> None:
    """MySQL must not use INSERT IGNORE, which truncates an over-long owner.

    Under IGNORE a too-long owner is stored truncated instead of failing; the
    locked read then matches nothing and the settings PUT would report a
    success that was never written. The duplicate-key no-op keeps conflicts
    silent without suppressing data errors.
    """
    session = _RecordingSession("mysql")
    artifact_sharing._insert_default_if_absent(session, 1, "alice")  # type: ignore[arg-type]

    assert len(session.statements) == 1
    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "IGNORE" not in sql.upper()
    assert "ON DUPLICATE KEY UPDATE" in sql


def test_read_sharing_without_a_row_is_none(db_uri: str) -> None:
    """No row is the documented default, represented as ``None``."""
    assert ArtifactSharingStore(db_uri).read_sharing("alice") is None


def test_write_sharing_merges_and_regenerates_the_gate_key(db_uri: str) -> None:
    """Absent update fields keep values; every write rotates the gate key."""
    store = ArtifactSharingStore(db_uri)

    first = store.write_sharing("alice", external=False)
    assert first.external is False
    assert first.code_hash is None

    second = store.write_sharing("alice", share_code="open-sesame")
    assert second.external is False
    assert second.code_hash is not None
    assert second.code_hash != "open-sesame"
    assert verify_share_code("open-sesame", second.code_hash)
    assert second.gate_key != first.gate_key

    third = store.write_sharing("alice", share_code=None)
    assert third.external is False
    assert third.code_hash is None
    assert third.gate_key != second.gate_key
    assert store.read_sharing("alice") == third


def test_write_sharing_is_per_owner(db_uri: str) -> None:
    """One owner's settings never leak into another owner's read."""
    store = ArtifactSharingStore(db_uri)
    store.write_sharing("alice", external=False)

    assert store.read_sharing("bob") is None


def test_write_sharing_seeds_the_row_before_the_locked_read(db_uri: str) -> None:
    """Disjoint first writes merge because the locked read finds a seeded row.

    A first write's ``SELECT ... FOR UPDATE`` would lock nothing while no row
    exists, so two racing first writes would each merge against the defaults
    and the later one would replace the other's field. This pins the statement
    order — insert-if-absent, locked read, update — and the merge itself.
    """
    store = ArtifactSharingStore(db_uri)
    engine = get_or_create_engine(db_uri)
    statements: list[str] = []

    def _capture(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        first = store.write_sharing("alice", external=False)
        second = store.write_sharing("alice", share_code="open-sesame")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert first.external is False
    assert second.external is False
    assert second.code_hash is not None
    assert store.read_sharing("alice") == second
    preferences = [statement for statement in statements if "preferences" in statement.lower()]
    assert [statement.lstrip().split(None, 1)[0].lower() for statement in preferences] == [
        "insert",
        "select",
        "update",
        "insert",
        "select",
        "update",
    ]


def test_write_sharing_raises_when_the_update_matches_no_row(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write whose update lands nowhere raises instead of reporting success.

    An owner the seed cannot store (MySQL truncation) leaves the locked read
    and the update matching nothing; returning a record there would claim a
    settings change that was never persisted.
    """
    store = ArtifactSharingStore(db_uri)
    monkeypatch.setattr(artifact_sharing, "_insert_default_if_absent", lambda *args: None)

    with pytest.raises(RuntimeError, match="matched no row"):
        store.write_sharing("alice", external=False)

    assert store.read_sharing("alice") is None


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"external": "yes", "gate_key": "k"}),
        json.dumps({"external": True, "gate_key": ""}),
        json.dumps({"external": True, "gate_key": "k", "code_hash": 7}),
        json.dumps([1, 2]),
    ],
)
def test_malformed_row_reads_as_no_row(db_uri: str, value: str) -> None:
    """A damaged row degrades to the open default instead of erroring."""
    engine = get_or_create_engine(db_uri)
    maker = make_named_managed_session_maker(engine, query_name_prefix="test.artifact_sharing")
    with maker("insert_malformed_row") as session:
        session.add(
            SqlPreference(
                workspace_id=current_workspace_id(),
                user_id="alice",
                key=ARTIFACT_SHARING_PREFERENCE_KEY,
                value=value,
            )
        )

    assert ArtifactSharingStore(db_uri).read_sharing("alice") is None


def test_gate_key_id_is_eight_hex_of_the_key_hash() -> None:
    """The grant binds to a short, stable fingerprint of the gate key."""
    identifier = gate_key_id("gate-key")

    assert len(identifier) == 8
    assert all(char in "0123456789abcdef" for char in identifier)
    assert identifier == gate_key_id("gate-key")
    assert identifier != gate_key_id("other-key")


# ── Grant ───────────────────────────────────────────────────────────


def test_grant_round_trip_is_deterministic_and_time_bound() -> None:
    """A grant verifies under its own key, token and gate id, until expiry."""
    grant = mint_grant(_SESSION_KEY, _TOKEN, "abcd1234", now=1_000)
    expiry = 1_000 + GRANT_TTL_SECONDS

    assert grant == mint_grant(_SESSION_KEY, _TOKEN, "abcd1234", now=1_000)
    assert grant_valid(_SESSION_KEY, _TOKEN, grant, "abcd1234", now=1_000) is True
    assert grant_valid(_SESSION_KEY, _TOKEN, grant, "abcd1234", now=expiry - 1) is True
    assert grant_valid(_SESSION_KEY, _TOKEN, grant, "abcd1234", now=expiry) is False


@pytest.mark.parametrize(
    ("session_key", "token", "gate_id"),
    [
        (_OTHER_KEY, _TOKEN, "abcd1234"),  # key rotation (revoke / archive)
        (_SESSION_KEY, "a1.other.bWFj", "abcd1234"),  # a different token
        (_SESSION_KEY, _TOKEN, "ffffffff"),  # a settings change
    ],
)
def test_grant_refuses_a_mismatched_binding(session_key: bytes, token: str, gate_id: str) -> None:
    """Each input of the MAC is load-bearing."""
    grant = mint_grant(_SESSION_KEY, _TOKEN, "abcd1234", now=1_000)

    assert grant_valid(session_key, token, grant, gate_id, now=1_000) is False


def test_tampered_or_malformed_grant_is_refused() -> None:
    """A flipped byte, a wrong shape or a non-grant string never verifies."""
    grant = mint_grant(_SESSION_KEY, _TOKEN, "abcd1234", now=1_000)
    expiry, mac = grant.split(".")
    flipped = ("A" if mac[0] != "A" else "B") + mac[1:]

    for value in ("", "not-a-grant.", f"{expiry}.{flipped}", f"{expiry}.AAAA"):
        assert grant_valid(_SESSION_KEY, _TOKEN, value, "abcd1234", now=1_000) is False


# ── Remember cookie ─────────────────────────────────────────────────


def test_remember_cookie_name_is_owner_scoped_and_secure_prefixed() -> None:
    """Two accounts on one browser get distinct cookies; https prefixes."""
    plain = remember_cookie_name("alice", secure=False)

    assert plain.startswith("omni_artifact_")
    assert len(plain) == len("omni_artifact_") + 8
    assert remember_cookie_name("bob", secure=False) != plain
    assert remember_cookie_name("alice", secure=True) == f"__Secure-{plain}"


def test_remember_cookie_round_trip_is_bound_to_owner_and_gate_key() -> None:
    """The cookie verifies for its owner and key until its own expiry."""
    value = issue_remember_cookie("gate-key", "alice", now=1_000)
    expiry = 1_000 + REMEMBER_COOKIE_MAX_AGE_SECONDS

    assert remember_cookie_valid("gate-key", "alice", value, now=1_000) is True
    assert remember_cookie_valid("gate-key", "alice", value, now=expiry - 1) is True
    assert remember_cookie_valid("gate-key", "alice", value, now=expiry) is False
    # A settings change regenerates the gate key and forgets the browser.
    assert remember_cookie_valid("new-key", "alice", value, now=1_000) is False
    assert remember_cookie_valid("gate-key", "bob", value, now=1_000) is False


def test_malformed_remember_cookie_is_refused() -> None:
    """A short, padded or tampered value never verifies."""
    value = issue_remember_cookie("gate-key", "alice", now=1_000)
    expiry, mac = value.split(".")

    for candidate in ("", f"{expiry}.AAAA", f"bad.{mac}", f"{expiry}.{mac}x"):
        assert remember_cookie_valid("gate-key", "alice", candidate, now=1_000) is False


# ── Unlock failure budget ───────────────────────────────────────────


def test_failure_budget_reserves_each_attempt_and_releases_a_success() -> None:
    """Reaching the limit refuses; a success returns its reserved slot."""
    budget = UnlockFailureBudget(max_failures=3, window_seconds=100, max_keys=10)

    assert [budget.reserve("alice", now=moment) for moment in (0, 1, 2)] == [True, True, True]
    assert budget.reserve("alice", now=2) is False
    budget.release("alice", reserved_at=1)
    assert budget.reserve("alice", now=2) is True
    assert budget.reserve("bob", now=2) is True


def test_failure_budget_forgets_hits_past_the_window() -> None:
    """Failures older than the window no longer count."""
    budget = UnlockFailureBudget(max_failures=2, window_seconds=100, max_keys=10)
    budget.reserve("alice", now=0)
    budget.reserve("alice", now=1)

    assert budget.reserve("alice", now=1) is False
    assert budget.reserve("alice", now=103) is True


def test_failure_budget_bounds_its_key_set() -> None:
    """At the cap, the oldest key is evicted to admit a new one."""
    budget = UnlockFailureBudget(max_failures=1, window_seconds=100, max_keys=2)
    budget.reserve("alice", now=0)
    budget.reserve("bob", now=0)

    budget.reserve("carol", now=0)

    assert budget.reserve("carol", now=0) is False
    assert budget.reserve("alice", now=0) is True


# ── Share-code verify ───────────────────────────────────────────────


def test_verify_share_code_only_matches_the_right_code() -> None:
    """The stored hash accepts its own code and nothing else."""
    stored = hash_password("open-sesame")

    assert verify_share_code("open-sesame", stored) is True
    assert verify_share_code("wrong", stored) is False
    assert verify_share_code("open-sesame", "not-a-hash") is False


# ── Full app: secrets never leave ───────────────────────────────────


async def test_no_api_response_carries_the_hash_or_gate_key(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Settings and current-user responses carry the summary, never the row."""
    put = await client.put(
        "/v1/artifact-sharing",
        json={"external": False, "share_code": "open-sesame"},
    )
    get = await client.get("/v1/artifact-sharing")
    me = await client.get("/v1/me")
    preferences_put = await client.put(
        "/v1/me/preferences",
        json={"version": 1, "settings": {}},
    )
    record = ArtifactSharingStore(db_uri).read_sharing("local")

    assert put.status_code == 200, put.text
    assert get.status_code == 200
    assert me.status_code == 200
    assert record is not None
    assert record.code_hash is not None
    assert isinstance(record, SharingRecord)
    for response in (put, get, me, preferences_put):
        assert record.gate_key not in response.text
        assert record.code_hash not in response.text
    assert set(get.json()) == {"external", "share_code_set"}
