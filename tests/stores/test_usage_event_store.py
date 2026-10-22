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


def _sealed(unit: str, **changes: object) -> UsageObservation:
    """One unit of a physical thread that already reported termination."""
    fields: dict[str, object] = {
        "producer_unit_id": unit,
        "physical_session_id": "phys-main",
        "terminal": [
            {
                "physical_session_id": "phys-main",
                "physical_subagent_id": None,
                "evidence": "terminal_record",
            }
        ],
    }
    fields.update(changes)
    harness = str(fields.get("harness", "codex-native"))
    family = harness.split("-")[0]
    fields.setdefault("harness_family", family)
    fields.setdefault(
        "identity_namespace",
        "codex/rollout/v1" if family == "codex" else "claude-code/transcript/v1",
    )
    return _observation(**fields)


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


def test_schema_two_sequence_and_server_end_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append("alice", _sealed("a"))
    assert first["sequence"] == 1
    assert first["ended_at"] == first["recorded_at"]
    assert first["call_started_not_before"] is None
    assert first["call_start_basis"] is None
    assert first["account_hint"] is None
    second = store.append("alice", _sealed("a", input_tokens=5))
    assert second["sequence"] == 2
    assert second["ended_at"] == first["ended_at"]
    page = store.page("alice")
    assert page["schema_version"] == 2
    assert [event["sequence"] for event in page["events"]] == [1, 2]
    # Producer association evidence never reaches the committed event.
    for event in page["events"]:
        assert "admission_id" not in event
        assert "route_check" not in event
        assert "terminal" not in event


def test_unsealed_unit_keeps_a_null_end_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = store.append("alice", _observation(producer_unit_id="a", input_tokens=1))
    assert event["ended_at"] is None
    later = store.append("alice", _observation(producer_unit_id="a", input_tokens=2))
    assert later["ended_at"] is None


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


def test_pi_response_identity_counts_once_and_unknown_call_stays_null(tmp_path: Path) -> None:
    store = _store(tmp_path)
    shared = {
        "harness": "pi-native",
        "harness_family": "pi",
        "identity_namespace": "pi/session/v1",
        "measurement_kind": "request",
        "logical_call_id": "pi-response-1",
        "call_identity_coverage": "complete",
        "source_refs": [
            {"kind": "response", "namespace": "pi/response-id/v1", "id": "pi-response-1"}
        ],
    }
    first = store.append("alice", _observation(**shared))
    second = store.append("alice", _observation(producer_unit_id="unit-2", **shared))
    warm = store.append(
        "alice",
        _observation(
            producer_unit_id="warm-1",
            harness="pi-native",
            harness_family="pi",
            identity_namespace="pi/session/v1",
            measurement_kind="increment",
            input_tokens=0,
            cached_input_tokens=8,
        ),
    )
    assert [first["request_count"], second["request_count"], warm["request_count"]] == [1, 0, None]


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
    store.append("alice", _sealed("a"))
    store.append("alice", _sealed("b"))
    store.append("alice", _observation(producer_unit_id="c"))
    store.append("alice", _observation(producer_unit_id="d", host_id="host-2"))
    first = store.page("alice", limit=1)
    assert first["host_progress"] == [
        {
            "host_id": "host-1",
            "last_sealed_turn_at": first["events"][0]["recorded_at"],
            "state": "unknown",
            "observed_at": first["events"][0]["recorded_at"],
        }
    ]
    second = store.page("alice", cursor=first["next_cursor"])
    assert second["host_progress"] == [
        {
            "host_id": "host-1",
            "last_sealed_turn_at": second["events"][0]["recorded_at"],
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
            _observation(producer_unit_id=f"unit-{index}", host_id=f"host-{index}"),
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
        _observation(occurred_at="2026-01-01T08:00:00+08:00")
    for server_field, value in (
        ("ended_at", "2026-01-01T00:00:00Z"),
        ("call_started_not_before", "2026-01-01T00:00:00Z"),
        ("account_hint", {}),
    ):
        with pytest.raises(ValidationError):
            _observation(**{server_field: value})
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


def _cutover(tmp_path: Path):
    from omnigent.usage_export.cutover import UsageCutoverStore

    return UsageCutoverStore(f"sqlite:///{tmp_path / 'usage.db'}")


def _drain_facts(**changes: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "detection": {
            "checkpoint_digest": "0" * 64,
            "clock_offset_ms": 0,
            "threshold_at": "2026-01-01T00:00:00Z",
        },
        "admissions": [],
        "calls": [],
        "counts": {},
        "failure_codes": [],
    }
    facts.update(changes)
    return facts


def _rescan() -> dict[str, object]:
    return {
        "checkpoint_digest": "0" * 64,
        "calls_unadmitted_after_hold": 0,
        "outbox_pending_observations": 0,
    }


def _usage_grant(uri: str) -> None:
    from omnigent.server.device_grant_store import DeviceGrantStore

    store = DeviceGrantStore(uri)
    store.create_grant(
        "grant-1",
        device_code_hash="test-device-hash",
        user_code="TEST-READER",
        client_id="usage-reader",
        scope="usage:read",
        created_at=1767225600,
        expires_at=1767226200,
    )
    store.approve("grant-1", user_id="alice", now_epoch_seconds=1767225600)
    store.redeem_approved(
        "grant-1", refresh_token_hash="test-refresh-hash", now_epoch_seconds=1767225600
    )


def _consumer_receipt(drained: dict[str, object], **changes: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "kind": "drained",
        "owner_scope_id": drained["owner_scope_id"],
        "feed_id": drained["feed_id"],
        "feed_epoch": drained["feed_epoch"],
        "domain": drained["domain"],
        "generation": drained["generation"],
        "producer_receipt_digest": drained["digest"],
        "acked_through_sequence": drained["last_committed_sequence"],
        "usage_read_grant_id": "grant-1",
        "legacy_frozen": True,
        "cutover_at": "2026-01-01T00:00:00Z",
        "authority_revision": 4,
        "authority_digest": "a" * 64,
        "authority_published": True,
        "restored_authority_revision": None,
        "restored_authority_digest": None,
    }
    receipt.update(changes)
    return receipt


def test_cutover_hold_machine_generations_and_admission_refusal(tmp_path: Path) -> None:
    store = _cutover(tmp_path)
    host = uuid.uuid4().hex
    first = store.create_hold("alice", host, "claude-sdk", 1)
    assert first["state"] == "held"
    assert first["active"] is True
    assert first["generation"] == 1
    assert first["hold_created_at"] is not None
    resumed = store.create_hold("alice", host, "claude-sdk", 1)
    assert resumed["hold_created_at"] == first["hold_created_at"]
    with pytest.raises(UsageExportError) as conflict:
        store.create_hold("alice", host, "claude-sdk", 2)
    assert conflict.value.code == "hold_generation_conflict"
    with pytest.raises(UsageExportError) as active:
        store.create_hold("alice", host, "claude-sdk", 1, goal_active=True)
    assert active.value.code == "codex_goal_active"
    assert store.active_hold(host, "claude-sdk") == {
        "host_id": host,
        "harness": "claude-sdk",
        "generation": 1,
        "state": "held",
    }
    assert store.active_hold(host, "codex-sdk") is None
    # An undecided harness refuses while any domain of the host is held.
    assert store.active_hold(host, None) is not None
    with pytest.raises(UsageExportError) as refused:
        store.admit_turn(
            "alice",
            admission_id=uuid.uuid4().hex,
            session_id=uuid.uuid4().hex,
            host_id=host,
            harness="claude-sdk",
            source="web",
        )
    assert refused.value.status_code == 409
    assert refused.value.code == "usage_cutover_hold"
    assert refused.value.detail == {"host_id": host, "harness": "claude-sdk", "generation": 1}
    other = store.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id=uuid.uuid4().hex,
        host_id=uuid.uuid4().hex,
        harness="claude-sdk",
        source="web",
    )
    assert other["admitted_at"].endswith("Z")


def test_receipts_page_shape_pagination_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _cutover(tmp_path)
    host = uuid.uuid4().hex
    with pytest.raises(UsageExportError) as unknown:
        store.receipts_page("alice", host, "claude-sdk", 1, 0, 10)
    assert unknown.value.code == "usage_cutover_domain_not_found"
    assert unknown.value.status_code == 404
    store.create_hold("alice", host, "claude-sdk", 3)
    empty = store.receipts_page("alice", host, "claude-sdk", 3, 2, 10)
    assert empty == {
        "receipt_page_schema_version": 1,
        "domain": {"harness": "claude-sdk", "host_id": host},
        "generation": 3,
        "receipts": [],
        "has_more": False,
        "next_after_seq": 2,
    }
    with pytest.raises(UsageExportError) as no_generation:
        store.receipts_page("alice", host, "claude-sdk", 2, 0, 10)
    assert no_generation.value.code == "usage_cutover_generation_not_found"
    for args, code in (
        ((host, "claude-sdk", 0, 0, 10), "usage_cutover_bad_request"),
        ((host, "claude-sdk", 3, -1, 10), "usage_cutover_bad_request"),
        ((host, "claude-sdk", 3, 0, 0), "usage_cutover_bad_request"),
        ((host, "claude-sdk", 3, 0, 51), "usage_cutover_bad_request"),
        (("h" * 257, "claude-sdk", 3, 0, 10), "usage_cutover_bad_request"),
        ((host, "opencode", 3, 0, 10), "usage_cutover_bad_request"),
    ):
        with pytest.raises(UsageExportError) as bad:
            store.receipts_page("alice", *args)
        assert bad.value.code == code
        assert bad.value.status_code == 400
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    drained = store.submit_drain("alice", host, "claude-sdk", 3, _drain_facts())
    assert drained["result"] == "drained"
    page = store.receipts_page("alice", host, "claude-sdk", 3, 0, 10)
    assert page["receipts"][0]["digest"] == drained["digest"]
    assert page["receipts"][0]["receipt_seq"] == 1
    assert page["next_after_seq"] == 1
    assert page["has_more"] is False
    first = store.receipts_page("alice", host, "claude-sdk", 3, 0, 1)
    assert [item["receipt_seq"] for item in first["receipts"]] == [1]
    assert first["has_more"] is False
    for _ in range(57):
        store.receipts_page("alice", host, "claude-sdk", 3, 1, 10)
    with pytest.raises(UsageExportError) as limited:
        store.receipts_page("alice", host, "claude-sdk", 3, 1, 10)
    assert limited.value.code == "usage_read_rate_limited"
    assert limited.value.status_code == 429
    assert limited.value.retry_after


def test_admission_entry_shape_sorted_and_zero_thread_rule() -> None:
    from omnigent.db.db_models import SqlUsageTurnAdmission
    from omnigent.usage_export.cutover import _admission_entry

    evidence = {
        "threads": {
            '["parent","main-child"]': [
                {"kind": "stop_hook", "reported_at": "2026-01-01T00:00:02Z", "source_ref": "h2"},
                {
                    "kind": "terminal_record",
                    "reported_at": "2026-01-01T00:00:01Z",
                    "source_ref": "r1",
                },
                {
                    "kind": "terminal_record",
                    "reported_at": "2026-01-01T00:00:03Z",
                    "source_ref": "r2",
                },
            ],
            '["parent",null]': [
                {"kind": "telemetry", "reported_at": "2026-01-01T00:00:05Z", "source_ref": None},
            ],
        },
        "turn_ended_at": "2026-01-01T00:00:09Z",
    }
    row = SqlUsageTurnAdmission(
        admission_id=uuid.uuid4().hex,
        session_id=uuid.uuid4().hex,
        admitted_at=1767225600,
        source="web",
        binding_revision=3,
        binding_digest=b"\x01" * 32,
        terminal_evidence=json.dumps(evidence),
    )
    entry = _admission_entry(row, linked_call=False)
    assert set(entry) == {
        "admission_id",
        "session_id",
        "admitted_at",
        "binding_revision",
        "binding_digest",
        "terminal",
        "terminal_state",
        "turn_ended_at",
        "threads",
    }
    assert entry["admitted_at"] == "2026-01-01T00:00:00Z"
    assert entry["binding_revision"] == 3
    assert entry["binding_digest"] == "01" * 32
    assert entry["terminal"] is True
    assert [thread["physical_subagent_id"] for thread in entry["threads"]] == [
        None,
        "main-child",
    ]
    child = entry["threads"][1]
    assert [item["kind"] for item in child["evidence"]] == [
        "stop_hook",
        "terminal_record",
        "terminal_record",
    ]
    assert [item["reported_at"] for item in child["evidence"]] == [
        "2026-01-01T00:00:02Z",
        "2026-01-01T00:00:01Z",
        "2026-01-01T00:00:03Z",
    ]
    assert entry["terminal_state"] == ["stop_hook", "telemetry", "terminal_record"]
    zero = SqlUsageTurnAdmission(
        admission_id=uuid.uuid4().hex,
        admitted_at=1767225600,
        source="web",
        terminal_evidence=json.dumps({"threads": {}, "turn_ended_at": None}),
    )
    idle = _admission_entry(zero, linked_call=False)
    assert idle["terminal"] is False
    assert idle["terminal_state"] == []
    linked = _admission_entry(zero, linked_call=True)
    assert linked["threads"] == []
    assert linked["terminal_state"] == []
    assert linked["terminal"] is False
    zero.terminal_evidence = json.dumps({"threads": {}, "turn_ended_at": "2026-01-01T00:00:09Z"})
    done = _admission_entry(zero, linked_call=False)
    assert done["terminal"] is True
    assert done["terminal_state"] == ["executor_end"]
    assert done["turn_ended_at"] == "2026-01-01T00:00:09Z"


def test_drain_merges_thread_evidence_and_release_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    _usage_grant(uri)
    host = uuid.uuid4().hex
    feed.append(
        "alice",
        _sealed("a", host_id=host, harness="claude-sdk", occurred_at="2025-12-31T23:58:59Z"),
    )
    cutover = UsageCutoverStore(uri)
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    admission = cutover.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id=uuid.uuid4().hex,
        host_id=host,
        harness="claude-sdk",
        source="web",
    )
    cutover.create_hold("alice", host, "claude-sdk", 1)
    open_drain = cutover.submit_drain(
        "alice",
        host,
        "claude-sdk",
        1,
        _drain_facts(
            admissions=[
                {
                    "admission_id": admission["admission_id"],
                    "threads": [
                        {
                            "physical_session_id": "phys-main",
                            "physical_subagent_id": None,
                            "evidence": [{"kind": "terminal_record", "source_ref": "r1"}],
                        }
                    ],
                }
            ]
        ),
    )
    assert open_drain["result"] == "drained"
    assert open_drain["last_committed_sequence"] == 1
    assert open_drain["admissions"][0]["terminal"] is True
    assert open_drain["admissions"][0]["terminal_state"] == ["terminal_record"]
    assert open_drain["admissions"][0]["threads"][0]["evidence"][0]["kind"] == ("terminal_record")
    page = feed.page("alice")
    feed.page("alice", cursor=page["next_cursor"], grant_id="grant-1")
    consumer = _consumer_receipt(open_drain, acked_through_sequence=1)
    released = cutover.release(
        "alice", host, "claude-sdk", 1, consumer, "2026-01-01T00:00:00Z", _rescan()
    )
    assert released["kind"] == "release"
    assert released["result"] == "released"
    assert released["release"]["cutover_at"] == "2026-01-01T00:00:00Z"
    assert cutover.active_hold(host, "claude-sdk") is None
    with pytest.raises(UsageExportError) as done:
        cutover.submit_drain("alice", host, "claude-sdk", 1, _drain_facts())
    assert done.value.code == "not_drained"


def test_release_refuses_each_condition_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    _usage_grant(uri)
    host = uuid.uuid4().hex
    feed.append(
        "alice",
        _sealed("a", host_id=host, harness="claude-sdk", occurred_at="2025-12-31T23:58:59Z"),
    )
    store = UsageCutoverStore(uri)
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    store.create_hold("alice", host, "claude-sdk", 1)
    drained = store.submit_drain("alice", host, "claude-sdk", 1, _drain_facts())
    first_page = feed.page("alice")
    feed.page("alice", cursor=first_page["next_cursor"], grant_id="grant-1")
    good = _consumer_receipt(drained)
    with pytest.raises(UsageExportError) as wrong_generation:
        store.release("alice", host, "claude-sdk", 2, good, "2026-01-01T00:00:00Z", _rescan())
    assert wrong_generation.value.code == "generation_mismatch"
    for change, code in (
        ({"producer_receipt_digest": "b" * 64}, "consumer_receipt_mismatch"),
        ({"acked_through_sequence": 0}, "consumer_not_acked"),
        ({"legacy_frozen": False}, "consumer_not_acked"),
        ({"authority_published": False}, "consumer_not_acked"),
    ):
        with pytest.raises(UsageExportError) as refused:
            store.release(
                "alice",
                host,
                "claude-sdk",
                1,
                _consumer_receipt(drained, **change),
                "2026-01-01T00:00:00Z",
                _rescan(),
            )
        assert refused.value.code == code
    with pytest.raises(UsageExportError) as bad_time:
        store.release(
            "alice",
            host,
            "claude-sdk",
            1,
            {**good, "cutover_at": "2026-01-01T00:30:00Z"},
            "2026-01-01T00:30:00Z",
            _rescan(),
        )
    assert bad_time.value.code == "cutover_time_invalid"
    with pytest.raises(UsageExportError) as invalidated:
        store.release(
            "alice",
            host,
            "claude-sdk",
            1,
            good,
            "2026-01-01T00:00:00Z",
            {**_rescan(), "calls_unadmitted_after_hold": 1},
        )
    assert invalidated.value.code == "drain_invalidated"
    assert store.hold_status("alice", host, "claude-sdk")["state"] == "invalidated"
    with pytest.raises(UsageExportError) as stuck:
        store.submit_drain("alice", host, "claude-sdk", 1, _drain_facts())
    assert stuck.value.code == "not_drained"


def test_abandon_requires_a_consumer_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    store = UsageCutoverStore(f"sqlite:///{tmp_path / 'usage.db'}")
    host = uuid.uuid4().hex
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    store.create_hold("alice", host, "codex-sdk", 5)
    pins = UsageEventStore(f"sqlite:///{tmp_path / 'usage.db'}").page("alice")
    receipt = {
        **{key: pins[key] for key in ("owner_scope_id", "feed_id", "feed_epoch")},
        "kind": "abandon",
        "domain": {"host_id": host, "harness": "codex-sdk"},
        "generation": 5,
        "authority_published": False,
    }
    with pytest.raises(UsageExportError) as mismatch:
        store.abandon("alice", host, "codex-sdk", 5, {**receipt, "kind": "drained"}, "no")
    assert mismatch.value.code == "consumer_receipt_mismatch"
    with pytest.raises(UsageExportError) as published:
        store.abandon(
            "alice",
            host,
            "codex-sdk",
            5,
            {**receipt, "authority_published": True},
            "no",
        )
    assert published.value.code == "consumer_receipt_mismatch"
    abandoned = store.abandon("alice", host, "codex-sdk", 5, receipt, "clock")
    assert abandoned["kind"] == "abandon"
    assert abandoned["result"] == "abandoned"
    assert store.active_hold(host, "codex-sdk") is None
    with pytest.raises(UsageExportError) as burned:
        store.create_hold("alice", host, "codex-sdk", 5)
    assert burned.value.code == "hold_generation_conflict"


def _linked_observation(unit: str, admission_id: str, **changes: object) -> UsageObservation:
    fields: dict[str, object] = {
        "producer_unit_id": unit,
        "admission_id": admission_id,
        "occurred_at": "2026-01-01T00:00:00Z",
        "turn_id": f"turn-{unit}",
        "thread_kind": "main",
    }
    fields.update(changes)
    return _sealed(unit, **fields)


def _call_entry(
    observation: UsageObservation,
    event: dict[str, object] | None,
    *,
    linked: bool,
    admission_id: str | None,
    state: str = "terminal_acked",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "call_key": {
            "meter_epoch": observation.meter_epoch,
            "producer_unit_id": observation.producer_unit_id,
        },
        "server": None
        if event is None
        else {
            "original_event_id": event["original_event_id"],
            "latest_event_id": event["event_id"],
            "sequence": event["sequence"],
            "revision": event["revision"],
        },
        "admission": "linked" if linked else "unadmitted",
        "admission_id": admission_id,
        "occurred_at": observation.occurred_at,
        "ended_at": None if event is None else event["ended_at"],
        "state": state,
    }
    return entry


def test_drain_calls_name_the_server_held_association(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    cutover = UsageCutoverStore(uri)
    host = uuid.uuid4().hex
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    admission = cutover.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id="session-1",
        host_id=host,
        harness="codex-native",
        source="web",
    )
    linked = _linked_observation("unit-linked", admission["admission_id"], host_id=host)
    linked_event = feed.append("alice", linked)
    unadmitted = _sealed("unit-unadmitted", host_id=host, occurred_at="2025-12-31T23:58:59Z")
    unadmitted_event = feed.append("alice", unadmitted)
    cutover.create_hold("alice", host, "codex-native", 1)
    drained = cutover.submit_drain(
        "alice",
        host,
        "codex-native",
        1,
        _drain_facts(
            admissions=[
                {
                    "admission_id": admission["admission_id"],
                    "threads": [
                        {
                            "physical_session_id": "phys-main",
                            "physical_subagent_id": None,
                            "evidence": [{"kind": "terminal_record", "source_ref": "r1"}],
                        }
                    ],
                }
            ],
            calls=[
                _call_entry(
                    linked,
                    linked_event,
                    linked=True,
                    admission_id=admission["admission_id"],
                ),
                _call_entry(
                    unadmitted,
                    unadmitted_event,
                    linked=False,
                    admission_id=None,
                    state="unadmitted_before_hold",
                ),
            ],
        ),
    )
    assert drained["result"] == "drained"
    by_unit = {entry["call_key"]["producer_unit_id"]: entry for entry in drained["calls"]}
    assert by_unit[linked.producer_unit_id]["admission_id"] == admission["admission_id"]
    assert by_unit[unadmitted.producer_unit_id]["admission_id"] is None
    assert set(by_unit[unadmitted.producer_unit_id]) >= {"admission_id", "state"}


def test_drain_calls_fail_when_the_named_admission_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    cutover = UsageCutoverStore(uri)
    host = uuid.uuid4().hex
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    first = cutover.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id="session-1",
        host_id=host,
        harness="codex-native",
        source="web",
    )
    second = cutover.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id="session-1",
        host_id=host,
        harness="codex-native",
        source="peer",
    )
    observation = _linked_observation("unit-linked", first["admission_id"], host_id=host)
    event = feed.append("alice", observation)
    cutover.create_hold("alice", host, "codex-native", 1)
    facts = _drain_facts(
        admissions=[
            {"admission_id": first["admission_id"], "threads": []},
            {"admission_id": second["admission_id"], "threads": []},
        ],
        calls=[
            _call_entry(
                observation,
                event,
                linked=True,
                admission_id=second["admission_id"],
            )
        ],
    )
    failed = cutover.submit_drain("alice", host, "codex-native", 1, facts)
    assert failed["result"] == "failed"
    assert "host_facts_mismatch" in failed["failure_codes"]
    # An unrecorded admission name fails the same way.
    unknown = cutover.submit_drain(
        "alice",
        host,
        "codex-native",
        1,
        _drain_facts(
            admissions=[{"admission_id": first["admission_id"], "threads": []}],
            calls=[
                _call_entry(
                    observation,
                    event,
                    linked=True,
                    admission_id=uuid.uuid4().hex,
                )
            ],
        ),
    )
    assert "host_facts_mismatch" in unknown["failure_codes"]


def test_zero_thread_admission_matches_through_call_admission_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    cutover = UsageCutoverStore(uri)
    host = uuid.uuid4().hex
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    admission = cutover.admit_turn(
        "alice",
        admission_id=uuid.uuid4().hex,
        session_id="session-1",
        host_id=host,
        harness="codex-native",
        source="web",
    )
    unadmitted = _sealed("unit-unadmitted", host_id=host, occurred_at="2025-12-31T23:58:59Z")
    unadmitted_event = feed.append("alice", unadmitted)
    cutover.create_hold("alice", host, "codex-native", 1)
    zero_thread_facts = _drain_facts(
        admissions=[
            {
                "admission_id": admission["admission_id"],
                "threads": [],
                "turn_ended_at": "2026-01-01T00:00:00Z",
            }
        ],
        calls=[
            _call_entry(
                unadmitted,
                unadmitted_event,
                linked=False,
                admission_id=None,
                state="unadmitted_before_hold",
            )
        ],
    )
    drained = cutover.submit_drain("alice", host, "codex-native", 1, zero_thread_facts)
    assert drained["result"] == "drained"
    entry = next(
        item for item in drained["admissions"] if item["admission_id"] == admission["admission_id"]
    )
    assert entry["terminal"] is True
    assert entry["terminal_state"] == ["executor_end"]

    hold = cutover.hold_status("alice", host, "codex-native")
    assert hold["state"] == "drained"
    linked_call = _call_entry(
        unadmitted,
        unadmitted_event,
        linked=True,
        admission_id=admission["admission_id"],
        state="terminal_acked",
    )
    blocked = cutover.submit_drain(
        "alice",
        host,
        "codex-native",
        1,
        _drain_facts(
            admissions=[
                {
                    "admission_id": admission["admission_id"],
                    "threads": [],
                    "turn_ended_at": "2026-01-01T00:00:00Z",
                }
            ],
            calls=[{**linked_call, "server": None}],
        ),
    )
    assert blocked["result"] == "failed"
    entry = next(
        item for item in blocked["admissions"] if item["admission_id"] == admission["admission_id"]
    )
    assert entry["terminal"] is False
    assert entry["terminal_state"] == []


def test_drain_rejects_a_terminal_claim_without_a_server_commit(tmp_path: Path) -> None:
    store = _cutover(tmp_path)
    host = uuid.uuid4().hex
    store.create_hold("alice", host, "codex-native", 1)
    receipt = store.submit_drain(
        "alice",
        host,
        "codex-native",
        1,
        _drain_facts(
            calls=[
                {
                    "call_key": {"meter_epoch": "meter", "producer_unit_id": "missing"},
                    "server": None,
                    "admission": "unadmitted",
                    "admission_id": None,
                    "occurred_at": "2020-01-01T00:00:00Z",
                    "ended_at": None,
                    "state": "terminal_acked",
                }
            ]
        ),
    )
    assert receipt["result"] == "failed"
    assert "host_facts_mismatch" in receipt["failure_codes"]


def test_release_invalidates_an_empty_boundary_when_the_first_event_arrives(
    tmp_path: Path,
) -> None:
    store = _cutover(tmp_path)
    host = uuid.uuid4().hex
    store.create_hold("alice", host, "codex-native", 1)
    drained = store.submit_drain("alice", host, "codex-native", 1, _drain_facts())
    assert drained["last_committed_sequence"] is None
    feed = UsageEventStore(f"sqlite:///{tmp_path / 'usage.db'}")
    feed.append("alice", _sealed("late", host_id=host, occurred_at="2020-01-01T00:00:00Z"))
    with pytest.raises(UsageExportError) as refused:
        store.release(
            "alice",
            host,
            "codex-native",
            1,
            _consumer_receipt(drained),
            "2026-01-01T00:00:00Z",
            _rescan(),
        )
    assert refused.value.code == "drain_invalidated"
    assert store.hold_status("alice", host, "codex-native")["state"] == "invalidated"


def test_goal_reservation_and_hold_serialize_in_both_orders(tmp_path: Path) -> None:
    store = _cutover(tmp_path)
    host = uuid.uuid4().hex
    store.admit_turn(
        "alice",
        admission_id="goal",
        session_id="session",
        host_id=host,
        harness="codex-native",
        source="goal",
    )
    with pytest.raises(UsageExportError) as active:
        store.create_hold("alice", host, "codex-native", 1)
    assert active.value.code == "codex_goal_active"
    store.end_goal_admissions("alice", "session")
    store.create_hold("alice", host, "codex-native", 1)
    with pytest.raises(UsageExportError) as held:
        store.admit_turn(
            "alice",
            admission_id="new-goal",
            session_id="session",
            host_id=host,
            harness="codex-native",
            source="goal",
        )
    assert held.value.code == "usage_cutover_hold"


def test_cutover_migration_roundtrip(tmp_path: Path) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    from omnigent.db.utils import _build_alembic_config

    uri = f"sqlite:///{tmp_path / 'cutover.db'}"
    config = _build_alembic_config(uri)
    assert ScriptDirectory.from_config(config).get_heads() == ["a43c20261021"]
    command.upgrade(config, "a44c20261021")
    command.upgrade(config, "a43c20261021")
    engine = create_engine(uri)
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(usage_events)"))}
    assert {
        "usage_cutover_holds",
        "usage_turn_admissions",
        "usage_cutover_receipts",
        "usage_account_binding_revisions",
        "usage_account_binding_activation",
        "usage_route_attestations",
    } <= tables
    assert {"host_id", "harness"} <= columns
    command.downgrade(config, "a44c20261021")
    with engine.connect() as connection:
        after = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "usage_cutover_holds" not in after
    command.upgrade(config, "a43c20261021")


def test_additive_migration_roundtrip(tmp_path: Path) -> None:
    from alembic import command

    from omnigent.db.enum_codecs import encode_device_grant_status
    from omnigent.db.utils import _build_alembic_config

    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "a31c20261019")

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


def test_drain_rejects_omitted_committed_open_call(tmp_path: Path) -> None:
    feed = _store(tmp_path)
    host = uuid.uuid4().hex
    feed.append("alice", _observation(host_id=host))
    cutover = _cutover(tmp_path)
    cutover.create_hold("alice", host, "codex-native", 1)
    receipt = cutover.submit_drain("alice", host, "codex-native", 1, _drain_facts())
    assert receipt["result"] == "failed"
    assert "host_facts_mismatch" in receipt["failure_codes"]
