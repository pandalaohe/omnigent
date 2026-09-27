"""Store behavior for durable session hand-offs."""

from __future__ import annotations

import uuid

import pytest

from omnigent.entities.session_handoff import SessionHandoff
from omnigent.stores.session_handoff_store.sqlalchemy_store import SqlAlchemySessionHandoffStore


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _record(seed: str, **overrides: object) -> SessionHandoff:
    values: dict[str, object] = {
        "id": _uid(f"handoff-{seed}"),
        "owner_user_id": "owner-a",
        "sender_session_id": _uid("sender-a"),
        "receiver_session_id": _uid(f"receiver-{seed}"),
        "create_session": True,
        "project_id": _uid("project-a"),
        "state": "open",
        "brief_hash": "hash-a",
        "brief": "task",
        "allow_onward": False,
        "brief_peer_id": _uid(f"brief-{seed}"),
        "created_at": 100,
        "updated_at": 100,
        "expires_at": 200,
    }
    values.update(overrides)
    return SessionHandoff(**values)  # type: ignore[arg-type]


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionHandoffStore:
    return SqlAlchemySessionHandoffStore(db_uri)


def test_create_get_transition_and_set_fields(store: SqlAlchemySessionHandoffStore) -> None:
    record = _record(
        "one", git_plan={"branch": "main"}, disclosure={"dirty": 2}, outcome={"done": []}
    )
    assert store.create(record) == record
    assert store.get(record.id) == record
    assert store.get(_uid("missing")) is None
    assert store.set_fields(record.id, ("failed",), brief="wrong") is False
    with pytest.raises(ValueError, match="cannot change"):
        store.set_fields(record.id, ("open",), state="failed")
    assert store.set_fields(record.id, ("open",), disclosure={"dirty": 3}) is True
    assert store.get(record.id).disclosure == {"dirty": 3}  # type: ignore[union-attr]
    assert store.transition(record.id, "delivered", None, ("failed",)) is False
    assert store.transition(record.id, "delivered", "sent", ("open",), outcome={"ok": True})
    updated = store.get(record.id)
    assert updated is not None
    assert (updated.state, updated.reason, updated.outcome) == ("delivered", "sent", {"ok": True})
    assert store.transition(_uid("missing"), "failed", None, ("open",)) is False


def test_claim_and_release(store: SqlAlchemySessionHandoffStore) -> None:
    record = store.create(_record("lease"))
    assert store.claim(record.id, 100, 10) is True
    assert store.claim(record.id, 110, 10) is False
    assert store.claim(record.id, 111, 10) is True
    store.release(record.id)
    assert store.get(record.id).lease_until is None  # type: ignore[union-attr]
    assert store.claim(record.id, 112, 10) is True


def test_counts_duplicate_branch_and_sender_list(store: SqlAlchemySessionHandoffStore) -> None:
    sender = _uid("sender-a")
    host = _uid("host-a")
    first = store.create(
        _record(
            "first",
            created_at=100,
            host_id=host,
            root="/root",
            checkout="/root",
            git_branch="main",
        )
    )
    second = store.create(
        _record(
            "second",
            created_at=200,
            host_id=host,
            root="/root",
            checkout="/root",
            git_branch="main",
        )
    )
    store.create(_record("terminal", created_at=300, state="completed"))
    assert store.count_unfinished("owner-a") == 2
    assert store.count_unfinished("owner-b") == 0
    assert store.count_recent(sender, 200) == 2
    assert store.find_unfinished_duplicate(sender, "hash-a").id == second.id  # type: ignore[union-attr]
    assert store.find_unfinished_duplicate(sender, "missing") is None
    assert store.find_branch_reservation(host, "/root", "main").id == second.id  # type: ignore[union-attr]
    assert store.find_branch_reservation(host, "/root", "other") is None
    # The reservation follows the checkout, never the entry root.
    assert store.find_branch_reservation(host, "/other-checkout", "main") is None
    assert [r.id for r in store.list_for_sender(sender, 100, 2)] == [
        _uid("handoff-terminal"),
        second.id,
    ]
    assert store.list_for_sender(_uid("other"), 0, 10) == []
    assert first.id != second.id


def test_receiver_binding_includes_unreported_expiry(store: SqlAlchemySessionHandoffStore) -> None:
    receiver = _uid("shared-receiver")
    active = store.create(_record("active", receiver_session_id=receiver, created_at=100))
    expired = store.create(
        _record(
            "expired",
            receiver_session_id=receiver,
            created_at=200,
            state="expired",
            reason="no_report",
            reported_at=None,
        )
    )
    assert store.find_binding_for_receiver(receiver).id == expired.id  # type: ignore[union-attr]
    assert store.set_fields(expired.id, ("expired",), reported_at=210)
    assert store.find_binding_for_receiver(receiver).id == active.id  # type: ignore[union-attr]
    assert store.find_binding_for_receiver(_uid("missing")) is None


def test_list_needing_work_filters_lease_and_terminal_state(
    store: SqlAlchemySessionHandoffStore,
) -> None:
    oldest = store.create(_record("oldest", updated_at=10))
    store.create(_record("leased", updated_at=20, lease_until=200))
    result = store.create(
        _record("result", state="completed", result_state="pending", updated_at=30)
    )
    stop = store.create(_record("stop", state="cancelled", stop_state="pending", updated_at=40))
    store.create(_record("done", state="completed", updated_at=5))
    assert [r.id for r in store.list_needing_work(100, 2)] == [oldest.id, result.id]
    assert [r.id for r in store.list_needing_work(100, 10)] == [oldest.id, result.id, stop.id]
    assert len(store.list_needing_work(201, 10)) == 4
