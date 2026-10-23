"""Permission state creation, recovery and exact approval identities."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    PERMISSION_STATE_LABEL_KEY,
    BoundToolCall,
    ConfirmedPermissionState,
    creation_permission_tier,
    is_permission_label_key,
    legacy_permission_tier,
    read_permission_state,
    require_permission_protocol,
    tier_label_key,
)


@pytest.mark.parametrize("harness", ["pi", "pi-native", "antigravity-native"])
def test_full_can_be_selected_explicitly_or_as_a_default(harness: str) -> None:
    assert creation_permission_tier(harness=harness) == "auto"
    assert creation_permission_tier(harness=harness, default="full") == "full"
    assert creation_permission_tier(harness=harness, inherited="full") == "auto"
    assert creation_permission_tier(harness=harness, inherited="plan") == "plan"
    assert creation_permission_tier(harness=harness, explicit="full", inherited="plan") == "full"
    assert creation_permission_tier(harness=harness, explicit="ask", default="auto") == "ask"


@pytest.mark.parametrize("raw", ["auto-strict", "auto-standard", "auto-relaxed"])
def test_ag_legacy_auto_values_share_relaxed_auto(raw: str) -> None:
    assert creation_permission_tier(harness="antigravity-native", default=raw) == "auto"
    assert (
        legacy_permission_tier(
            harness="antigravity-native",
            labels={tier_label_key("antigravity-native"): raw},
            launch_args=[],
        )
        == "auto"
    )


@pytest.mark.parametrize("raw", ["auto-strict", "auto-standard", "auto-relaxed"])
def test_request_schemas_keep_legacy_auto_aliases_for_normalization(raw):
    from omnigent.server.schemas import (
        SessionCreateMetadata,
        SessionCreateRequest,
        SessionForkRequest,
    )

    for schema, extras in (
        (SessionCreateMetadata, {}),
        (SessionCreateRequest, {"agent_id": "agent"}),
        (SessionForkRequest, {}),
    ):
        assert schema.model_validate({**extras, "permission_tier": raw}).permission_tier == raw


@pytest.mark.parametrize("harness", ["pi", "pi-native", "antigravity-native"])
def test_missing_and_invalid_legacy_tiers_are_not_inferred_full(harness: str) -> None:
    assert legacy_permission_tier(harness=harness, labels={}, launch_args=[]) == "ask"
    args = ["--dangerously-skip-permissions"]
    assert legacy_permission_tier(harness=harness, labels={}, launch_args=args) == (
        "full" if harness == "antigravity-native" else "ask"
    )
    with pytest.raises(ValueError):
        legacy_permission_tier(
            harness=harness, labels={tier_label_key(harness): "broken"}, launch_args=args
        )


def test_ready_state_roundtrip_and_incomplete_state_blocks() -> None:
    state = ConfirmedPermissionState("auto", 4, "ready")
    labels = state.labels("pi")
    with pytest.raises(ValueError, match="confirmed runner capability"):
        read_permission_state(labels, harness="pi")
    labels[PERMISSION_CAPABILITY_LABEL_KEY] = PERMISSION_CAPABILITY_VALUE
    assert read_permission_state(labels, harness="pi") == state
    assert (
        read_permission_state(replace(state, status="blocked").labels("pi"), harness="pi").status
        == "blocked"
    )
    with pytest.raises(ValueError, match="controlled recovery"):
        read_permission_state({tier_label_key("pi"): "full"}, harness="pi")


def test_protocol_requires_v1_for_tiered_sessions_and_sse_query() -> None:
    old = SimpleNamespace(headers={}, query_params={})
    require_permission_protocol(old, harness="claude-native")
    with pytest.raises(HTTPException) as error:
        require_permission_protocol(old, harness="pi-native")
    assert error.value.status_code == 426
    require_permission_protocol(
        SimpleNamespace(headers={"X-Omnigent-Permission-Tiers": "1"}, query_params={}),
        harness="pi-native",
    )
    query = SimpleNamespace(headers={}, query_params={"permission_tiers": "1"})
    require_permission_protocol(query, harness="pi-native", allow_query=True)
    with pytest.raises(HTTPException):
        require_permission_protocol(query, harness="pi-native")


def test_permission_label_reservation_includes_collation_lookalikes() -> None:
    assert is_permission_label_key("omnigent.pi.permission_tier")
    assert is_permission_label_key("OMNIGENT.PI.PERMISSION_TIER")
    assert is_permission_label_key("omni\ufeffgent.pi.permission_tier")
    assert not is_permission_label_key("team")


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        "broken",
        "{}",
        '{"revision":true,"status":"ready"}',
        '{"revision":0,"status":"ready"}',
        '{"revision":1,"status":"missing"}',
        '{"revision":1,"status":"switching"}',
        '{"revision":1,"status":[],"transaction_id":"t"}',
    ],
)
def test_malformed_state_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError):
        read_permission_state(
            {tier_label_key("pi"): "auto", PERMISSION_STATE_LABEL_KEY: raw}, harness="pi"
        )


def test_approval_identity_binds_every_execution_field() -> None:
    call = BoundToolCall.bind(
        session_id="session",
        host_id="host",
        call_id="call",
        name="write",
        arguments={"path": "/opt/work/project/a", "content": "one"},
        tier_revision=2,
    )
    reordered = BoundToolCall.bind(
        session_id="session",
        host_id="host",
        call_id="call",
        name="write",
        arguments={"content": "one", "path": "/opt/work/project/a"},
        tier_revision=2,
    )
    assert call.approval_id() == reordered.approval_id()
    for changed in [
        replace(call, session_id="other"),
        replace(call, host_id="other"),
        replace(call, call_id="next"),
        replace(call, name="read"),
        replace(call, arguments_digest="different"),
        replace(call, tier_revision=3),
    ]:
        assert changed.approval_id() != call.approval_id()
    with pytest.raises(ValueError):
        BoundToolCall.bind(
            session_id="s",
            host_id="h",
            call_id="c",
            name="write",
            arguments={"value": float("nan")},
            tier_revision=2,
        )
