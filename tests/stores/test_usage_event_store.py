"""Behavior of the durable owner usage feed and producer registry."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

import omnigent.usage_export.store as usage_store_module
from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.usage_export.models import UsageObservation
from omnigent.usage_export.store import UsageEventStore, UsageExportError


def _store(tmp_path: Path) -> UsageEventStore:
    return UsageEventStore(f"sqlite:///{tmp_path / 'usage.db'}")


def _observation(**changes: object) -> UsageObservation:
    fields: dict[str, object] = {
        "producer_unit_id": "unit-1",
        "meter_epoch": "epoch-1",
        "host_id": "host-1",
        "harness": "codex-native",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "measurement_kind": "increment",
        "thread_kind": "main",
        "harness_family": "codex",
        "identity_namespace": "codex/rollout/v1",
        "input_tokens": 3,
        "output_tokens": 0,
        "usage_status": "complete",
    }
    fields.update(changes)
    return UsageObservation.model_validate(fields)


def _response(unit: str, call: str, **changes: object) -> UsageObservation:
    return _observation(
        producer_unit_id=unit,
        logical_call_id=call,
        call_identity_coverage="complete",
        source_refs=[
            {
                "kind": "response",
                "namespace": "codex-responses/response-id/v1",
                "id": call,
            }
        ],
        **changes,
    )


def test_replay_restart_and_full_replacement_even_after_revert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _observation(input_tokens=3)
    first = store.append("alice", original)
    assert first["request_count"] is None
    assert first["cached_input_tokens"] is None
    assert first["output_tokens"] == 0
    restarted = _store(tmp_path)
    assert restarted.append("alice", original) == first
    changed = restarted.append("alice", _observation(input_tokens=4))
    reverted = restarted.append("alice", original)
    assert [event["revision"] for event in (first, changed, reverted)] == [1, 2, 3]
    assert [event["kind"] for event in (first, changed, reverted)] == [
        "usage",
        "correction",
        "correction",
    ]
    assert changed["correction_of"] == first["event_id"]
    assert reverted["correction_of"] == first["event_id"]
    assert len({event["event_id"] for event in (first, changed, reverted)}) == 3
    assert {event["original_event_id"] for event in (first, changed, reverted)} == {
        first["event_id"]
    }
    assert [event["input_tokens"] for event in restarted.page("alice")["events"]] == [3, 4, 3]


def test_canonical_call_allocation_and_unit_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append("alice", _response("unit-1", "response-1"))
    second = store.append("alice", _response("unit-2", "response-1"))
    assert [first["request_count"], second["request_count"]] == [1, 0]
    correction = store.append("alice", _response("unit-2", "response-1", input_tokens=9))
    assert correction["request_count"] == 0
    with pytest.raises(UsageExportError) as identity_error:
        store.append("alice", _response("unit-2", "response-2"))
    assert identity_error.value.status_code == 409
    with pytest.raises(UsageExportError) as unit_error:
        store.append("alice", _response("unit-2", "response-1", session_id="other"))
    assert unit_error.value.code == "unit_identity_conflict"
    assert len(store.page("alice")["events"]) == 3


def test_cursor_scope_tampering_and_stable_pagination_fence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("alice", _observation(producer_unit_id="a"))
    store.append("alice", _observation(producer_unit_id="b"))
    page = store.page("alice", limit=1)
    assert page["has_more"] is True
    cursor = page["next_cursor"]
    store.append("alice", _observation(producer_unit_id="c"))
    rest = store.page("alice", cursor=cursor, limit=1)
    assert [item["producer_unit_id"] for item in rest["events"]] == ["b"]
    assert rest["has_more"] is False
    later = store.page("alice", cursor=rest["next_cursor"])
    assert [item["producer_unit_id"] for item in later["events"]] == ["c"]
    with pytest.raises(UsageExportError) as other_owner:
        store.page("bob", cursor=cursor)
    assert other_owner.value.status_code == 403
    with pytest.raises(UsageExportError) as tampered:
        store.page("alice", cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))
    assert tampered.value.status_code == 400
    with workspace_scope(9), pytest.raises(UsageExportError) as other_workspace:
        store.page("alice", cursor=cursor)
    assert other_workspace.value.status_code == 403
    with workspace_scope(9):
        assert store.page("alice")["events"] == []


def test_concurrent_first_feed_writes_have_one_complete_order(tmp_path: Path) -> None:
    stores = (_store(tmp_path), _store(tmp_path))

    def append(index: int) -> dict[str, object]:
        return stores[index % 2].append("alice", _observation(producer_unit_id=f"unit-{index}"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        emitted = list(pool.map(append, range(12)))
    page = stores[0].page("alice")
    assert len({event["event_id"] for event in emitted}) == 12
    assert len(page["events"]) == 12
    assert {event["event_id"] for event in page["events"]} == {
        event["event_id"] for event in emitted
    }
    assert page["has_more"] is False


def test_tail_page_uses_one_snapshot_after_long_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    for index in range(45):
        store.append("alice", _observation(producer_unit_id=f"unit-{index}"))
    cursor = store.page("alice", limit=50)["next_cursor"]
    decoded = 0
    original = usage_store_module._event_from_payload

    def count_decode(payload: str) -> dict[str, object]:
        nonlocal decoded
        decoded += 1
        return original(payload)

    monkeypatch.setattr(usage_store_module, "_event_from_payload", count_decode)
    tail = store.page("alice", cursor=cursor)
    assert tail["events"] == []
    assert decoded == 1


def test_lost_cursor_returns_scoped_genesis_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("alice", _observation())
    cursor = store.page("alice")["next_cursor"]
    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    with get_or_create_engine(uri).begin() as connection:
        connection.execute(text("UPDATE usage_feeds SET next_sequence = 1"))
    with pytest.raises(UsageExportError) as expired:
        store.page("alice", cursor=cursor)
    assert expired.value.status_code == 410
    assert expired.value.detail is not None
    assert expired.value.detail["recovery_state"] == "genesis_replay_available"
    assert expired.value.detail["replay_from_cursor"] != cursor
    assert expired.value.detail["gap_ranges"] == []


def test_prefix_progress_and_shared_durable_throttle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("alice", _observation(producer_unit_id="a", ended_at="2026-01-01T00:00:00Z"))
    store.append("alice", _observation(producer_unit_id="b", ended_at="2026-01-02T00:00:00Z"))
    store.append("alice", _observation(producer_unit_id="c", ended_at=None))
    store.append("alice", _observation(producer_unit_id="d", host_id="host-2", ended_at=None))
    first = store.page("alice", limit=1)
    assert first["host_progress"] == [
        {
            "host_id": "host-1",
            "last_sealed_turn_at": "2026-01-01T00:00:00Z",
            "state": "unknown",
            "observed_at": first["events"][0]["recorded_at"],
        }
    ]
    second = store.page("alice", cursor=first["next_cursor"])
    assert second["host_progress"] == [
        {
            "host_id": "host-1",
            "last_sealed_turn_at": "2026-01-02T00:00:00Z",
            "state": "unknown",
            "observed_at": second["events"][1]["recorded_at"],
        },
        {
            "host_id": "host-2",
            "last_sealed_turn_at": None,
            "state": "unknown",
            "observed_at": second["events"][2]["recorded_at"],
        },
    ]
    restarted = _store(tmp_path)
    for _ in range(58):
        restarted.page("alice")
    with pytest.raises(UsageExportError) as limited:
        store.page("alice")
    assert limited.value.status_code == 429
    assert limited.value.retry_after is not None


def test_host_progress_limit_rejects_new_host_without_committing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(32):
        store.append(
            "alice",
            _observation(producer_unit_id=f"unit-{index}", host_id=f"host-{index}", ended_at=None),
        )
    with pytest.raises(UsageExportError) as limited:
        store.append("alice", _observation(producer_unit_id="unit-33", host_id="host-33"))
    assert limited.value.status_code == 422
    page = store.page("alice")
    assert len(page["events"]) == len(page["host_progress"]) == 32
    assert page["has_more"] is False


def test_producer_binding_survives_restart_and_is_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer_id = str(uuid.uuid4())
    binding = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "host_id": "host-1",
        "harness": "codex-native",
        "parent_session_id": None,
    }
    registration = store.register_producer("alice", producer_id, binding)
    assert registration["producer_id"] == uuid.UUID(producer_id).hex
    restarted = _store(tmp_path)
    assert restarted.register_producer("alice", producer_id, binding) == registration
    assert restarted.get_producer("alice", producer_id) == registration
    assert restarted.get_producer("bob", producer_id) is None
    with workspace_scope(9):
        assert restarted.get_producer("alice", producer_id) is None
    with pytest.raises(UsageExportError) as conflict:
        restarted.register_producer("alice", producer_id, {**binding, "host_id": "other"})
    assert conflict.value.status_code == 409


def test_account_generation_partitions_feed_and_revokes_old_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    engine = get_or_create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (workspace_id, id, is_admin, account_generation) "
                "VALUES (0, 'alice', 0, 'generation-one')"
            )
        )
    producer_id = uuid.uuid4().hex
    binding = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "host_id": "host-1",
        "harness": "codex-native",
        "parent_session_id": None,
    }
    with account_authority_scope("alice", "generation-one"):
        old_feed = store.page("alice")
        store.append("alice", _observation())
        store.register_producer("alice", producer_id, binding)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET account_generation = 'generation-two' WHERE id = 'alice'")
        )
    with account_authority_scope("alice", "generation-one"), pytest.raises(OmnigentError):
        store.append("alice", _observation(producer_unit_id="late"))
    with account_authority_scope("alice", "generation-two"):
        current_feed = store.page("alice")
        assert current_feed["events"] == []
        assert current_feed["owner_scope_id"] != old_feed["owner_scope_id"]
        assert store.get_producer("alice", producer_id) is None


def test_model_bounds_and_corrupt_row_stops_page(tmp_path: Path) -> None:
    assert _observation(cost_usd="1.2300").cost_usd == "1.23"
    assert _observation(cost_usd="9" * 64).cost_usd == "9" * 64
    with pytest.raises(ValidationError):
        _observation(input_tokens=True)
    with pytest.raises(ValidationError):
        _observation(host_id="é" * 129)
    with pytest.raises(ValidationError):
        _observation(ended_at="2026-01-01T08:00:00+08:00")
    with pytest.raises(ValidationError):
        _observation(identity_namespace="claude-code/transcript/v1")
    store = _store(tmp_path)
    store.append("alice", _observation())
    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    with get_or_create_engine(uri).begin() as connection:
        original_payload = json.loads(
            connection.execute(text("SELECT payload FROM usage_events")).scalar_one()
        )
    for field, value in (
        ("kind", "unknown"),
        ("usage_status", "unknown"),
        ("input_tokens", "not-an-integer"),
        ("cost_usd", "NaN"),
    ):
        payload = {**original_payload, field: value}
        with get_or_create_engine(uri).begin() as connection:
            connection.execute(
                text("UPDATE usage_events SET payload = :payload"),
                {"payload": json.dumps(payload, sort_keys=True, separators=(",", ":"))},
            )
        with pytest.raises(UsageExportError) as corrupt:
            store.page("alice")
        assert corrupt.value.status_code == 422
        assert corrupt.value.code == "corrupt_usage_event"
    with get_or_create_engine(uri).begin() as connection:
        connection.execute(
            text("UPDATE usage_events SET payload = :payload"),
            {"payload": "x" * (32 * 1024 + 1)},
        )
    with pytest.raises(UsageExportError) as oversized:
        store.page("alice")
    assert oversized.value.code == "export_record_too_large"
    with pytest.raises(UsageExportError) as invalid_limit:
        store.page("alice", limit=0)
    assert invalid_limit.value.status_code == 422


def test_additive_migration_roundtrip(tmp_path: Path) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    from omnigent.db.enum_codecs import encode_device_grant_status
    from omnigent.db.utils import _build_alembic_config

    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "a31c20261019")
    assert ScriptDirectory.from_config(config).get_heads() == ["a33c20261020"]
    engine = create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO device_grants "
                "(workspace_id,id,device_code_hash,user_code,status,client_id,"
                "created_at,expires_at) "
                "VALUES (0,:id,:digest,:code,:status,:client_id,1000,1600)"
            ),
            [
                {
                    "id": str(i),
                    "digest": str(i) * 64,
                    "code": f"CODE-{i}",
                    "client_id": client,
                    "status": encode_device_grant_status("redeemed"),
                }
                for i, client in enumerate([None, "slack", "omnigent-cli"], 1)
            ],
        )
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT name FROM sqlite_master WHERE name='usage_feeds'")
        )
        assert before.scalar_one_or_none() is None
    command.upgrade(config, "a32c20261020")
    with engine.connect() as connection:
        upgraded = connection.execute(
            text("SELECT name FROM sqlite_master WHERE name='usage_feeds'")
        )
        assert upgraded.scalar_one() == "usage_feeds"
        assert connection.execute(
            text("SELECT client_id,scope FROM device_grants ORDER BY id")
        ).all() == [(None, "sessions"), ("slack", "sessions"), ("omnigent-cli", None)]
    command.downgrade(config, "a31c20261019")
    with engine.connect() as connection:
        after = connection.execute(text("SELECT name FROM sqlite_master WHERE name='usage_feeds'"))
        assert after.scalar_one_or_none() is None
    command.upgrade(config, "a32c20261020")
    with engine.connect() as connection:
        final = connection.execute(text("SELECT name FROM sqlite_master WHERE name='usage_feeds'"))
        assert final.scalar_one() == "usage_feeds"
