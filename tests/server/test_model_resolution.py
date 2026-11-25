"""Every reference consumer uses the same usable-catalog boundary."""

import pytest

from omnigent.models.model_reference import provider_groups
from omnigent.server.model_resolution import ResolutionContext, notes_from_labels, usable_catalog


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"models": []},
        {"models": [{"id": "p/x"}], "error": "probe failed"},
        {"models": [{"id": ""}, {"model": None}, "invalid"]},
        {"models": None},
    ],
)
def test_unusable_catalog_is_unknown(record):
    assert usable_catalog(record) is None


def test_usable_catalog_includes_both_advertised_identifiers():
    assert usable_catalog(
        {"models": [{"id": "p/x", "model": "x"}, {"model": "p/y"}], "error": None}
    ) == frozenset({"p/x", "x", "p/y"})


def test_launch_preserves_qualified_model_and_accepts_explicit_provider_rename():
    groups = provider_groups([("old", "krill")])
    context = ResolutionContext("host-a", {"codex-native": {"models": [{"id": "x"}]}}, groups)
    result = context.resolve("codex-native", "old/x", "explicit")
    assert (result.model, result.row_id, result.outcome) == ("old/x", None, "missing")
    context.records = {"codex-native": {"models": [{"id": "old/x"}, {"id": "x"}]}}
    reverse = context.resolve("codex-native", "krill/x", "resume")
    assert (reverse.model, reverse.row_id, reverse.outcome) == ("old/x", "old/x", "renamed")


def test_missing_is_a_note_not_a_changed_launch_and_notes_fit_label_columns():
    context = ResolutionContext("host-a", {"pi-native": {"models": [{"id": "other/y"}]}})
    for index in range(4):
        result = context.resolve("pi-native", f"old/x-{index}", f"member:{index}")
        assert result.model == result.saved and result.outcome == "missing"
    labels = context.labels()
    assert all(len(value) <= 256 for value in labels.values())
    assert notes_from_labels(labels) == [note.payload() for note in context.notes]
    assert all("passing it to the host unchanged" in warning for warning in context.warnings())


def test_session_model_display_per_harness():
    import json

    from omnigent.server.model_resolution import (
        LAUNCHED_MODEL_LABEL,
        RESOLUTION_LABEL,
        session_model_display,
    )

    # T12: codex-native requested, launched, and running all match.
    codex = session_model_display(
        {LAUNCHED_MODEL_LABEL: "gpt-6.2-sol"},
        "gpt-6.2-sol",
        "gpt-6.2-sol",
        harness="codex-native",
    )
    assert (codex.requested, codex.launched, codex.running) == (
        "gpt-6.2-sol",
        "gpt-6.2-sol",
        "gpt-6.2-sol",
    )

    # T13: provider rename keeps saved as requested and post-resolution as launched.
    rename_labels = {
        LAUNCHED_MODEL_LABEL: "krill/gpt-6.2-sol",
        RESOLUTION_LABEL: json.dumps(
            [
                {
                    "setting": "session",
                    "saved": "ccs-krill/gpt-6.2-sol",
                    "model": "krill/gpt-6.2-sol",
                    "outcome": "renamed",
                    "host": "host-a",
                }
            ]
        ),
    }
    renamed = session_model_display(
        rename_labels,
        "krill/gpt-6.2-sol",
        "krill/gpt-6.2-sol",
        harness="codex-native",
    )
    assert (renamed.requested, renamed.launched, renamed.running) == (
        "ccs-krill/gpt-6.2-sol",
        "krill/gpt-6.2-sol",
        "krill/gpt-6.2-sol",
    )

    # Mid-session PATCH to another override ignores stale rename notes and keeps the last launch.
    patched = session_model_display(
        rename_labels,
        "gpt-6.2-astra",
        "gpt-6.2-astra",
        harness="codex-native",
    )
    assert (patched.requested, patched.launched, patched.running) == (
        "gpt-6.2-astra",
        "krill/gpt-6.2-sol",
        "gpt-6.2-astra",
    )

    # Member notes on a parent session do not override the session's own requested model.
    member_only_labels = {
        LAUNCHED_MODEL_LABEL: "krill/gpt-6.2-sol",
        RESOLUTION_LABEL: json.dumps(
            [
                {
                    "setting": "member:researcher",
                    "saved": "ccs-krill/gpt-6.2-sol",
                    "model": "krill/gpt-6.2-sol",
                    "outcome": "renamed",
                    "host": "host-a",
                }
            ]
        ),
    }
    parent = session_model_display(
        member_only_labels,
        "krill/gpt-6.2-sol",
        None,
        harness="codex-native",
    )
    assert (parent.requested, parent.launched, parent.running) == (
        "krill/gpt-6.2-sol",
        "krill/gpt-6.2-sol",
        None,
    )

    # claude-native alias vs concrete reported model; <synthetic> omitted.
    claude = session_model_display(
        {LAUNCHED_MODEL_LABEL: "opus"},
        "opus",
        "claude-opus-4-8[1m]",
        harness="claude-native",
    )
    assert (claude.requested, claude.launched, claude.running) == (
        "opus",
        "opus",
        "claude-opus-4-8[1m]",
    )
    claude_synthetic = session_model_display(
        {LAUNCHED_MODEL_LABEL: "opus"},
        "opus",
        "<synthetic>",
        harness="claude-native",
    )
    assert (claude_synthetic.requested, claude_synthetic.launched, claude_synthetic.running) == (
        "opus",
        "opus",
        None,
    )

    # pi-native: dropped pick omits launched while keeping requested vs reported default visible.
    pi = session_model_display(
        {},
        "omnigent/moonshotai/kimi-k2.5",
        "anthropic/claude-sonnet-4-6",
        harness="pi-native",
    )
    assert (pi.requested, pi.launched, pi.running) == (
        "omnigent/moonshotai/kimi-k2.5",
        None,
        "anthropic/claude-sonnet-4-6",
    )

    # antigravity-native: override is only a request; launched is omitted rather than shown wrong.
    agy = session_model_display(
        {},
        "gemini-4-neon",
        "Gemini 4 Neon (High)",
        harness="antigravity-native",
    )
    assert (agy.requested, agy.launched, agy.running) == (
        "gemini-4-neon",
        None,
        "Gemini 4 Neon (High)",
    )

    # ACP: requested, launched once dispatched, and running when reported (None before turn 1).
    acp_before = session_model_display(
        {LAUNCHED_MODEL_LABEL: "claude-sonnet-4-6"},
        "claude-sonnet-4-6",
        None,
        harness="acp:custom",
    )
    assert (acp_before.requested, acp_before.launched, acp_before.running) == (
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
        None,
    )
    acp_after = session_model_display(
        {LAUNCHED_MODEL_LABEL: "claude-sonnet-4-6"},
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
        harness="acp:custom",
    )
    assert (acp_after.requested, acp_after.launched, acp_after.running) == (
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
    )

    # Old / default sessions omit unset fields rather than guessing.
    default_session = session_model_display({}, None, None, harness="codex-native")
    assert (default_session.requested, default_session.launched, default_session.running) == (
        None,
        None,
        None,
    )


def test_session_response_exposes_requested_launched_running():
    import json

    from omnigent.entities import Conversation
    from omnigent.server.model_resolution import LAUNCHED_MODEL_LABEL, RESOLUTION_LABEL
    from omnigent.server.routes._sessions.orchestration import _build_session_response

    conv = Conversation(
        id="conv_s3",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_s3",
        agent_id="ag_test",
        harness_override="codex-native",
        model_override="krill/gpt-6.2-sol",
        reported_model="gpt-6.2-sol",
        labels={
            LAUNCHED_MODEL_LABEL: "krill/gpt-6.2-sol",
            RESOLUTION_LABEL: json.dumps(
                [
                    {
                        "setting": "session",
                        "saved": "ccs-krill/gpt-6.2-sol",
                        "model": "krill/gpt-6.2-sol",
                        "outcome": "renamed",
                        "host": "host-a",
                    }
                ]
            ),
        },
    )
    resp = _build_session_response(conv, [], "idle")
    assert resp.requested == "ccs-krill/gpt-6.2-sol"
    assert resp.launched == "krill/gpt-6.2-sol"
    assert resp.running == "gpt-6.2-sol"

    agy_conv = Conversation(
        id="conv_s3_agy",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_s3_agy",
        agent_id="ag_test",
        harness_override="antigravity-native",
        model_override="gemini-4-neon",
        reported_model="Gemini 4 Neon (High)",
        labels={},
    )
    agy_resp = _build_session_response(agy_conv, [], "idle")
    assert agy_resp.requested == "gemini-4-neon"
    assert agy_resp.launched is None
    assert agy_resp.running == "Gemini 4 Neon (High)"


def test_launched_model_requires_recorded_launch_not_stored_override():
    """Stored overrides before launch or before the next turn never masquerade as launched."""
    from omnigent.server.model_resolution import (
        LAUNCHED_MODEL_LABEL,
        session_model_display,
    )

    # Unlaunched session with a stored override omits launched.
    pending = session_model_display({}, "model-b", None, harness="codex-native")
    assert (pending.requested, pending.launched, pending.running) == ("model-b", None, None)

    # Session launched on A then PATCHed to B before its next turn still shows launched=A.
    patched = session_model_display(
        {LAUNCHED_MODEL_LABEL: "model-a"},
        "model-b",
        "model-a",
        harness="codex-native",
    )
    assert (patched.requested, patched.launched, patched.running) == (
        "model-b",
        "model-a",
        "model-a",
    )

    # Codex fallback: mid-launch with rejected A in override omits launched;
    # after fallback launch on B and reset of override, launched reports B.
    mid_fallback = session_model_display({}, "rejected-a", None, harness="codex-native")
    assert mid_fallback.launched is None
    after_fallback = session_model_display(
        {LAUNCHED_MODEL_LABEL: "fallback-b"},
        None,
        "fallback-b",
        harness="codex-native",
    )
    assert (after_fallback.requested, after_fallback.launched, after_fallback.running) == (
        None,
        "fallback-b",
        "fallback-b",
    )

    # Antigravity never exposes launched even if a label is present.
    agy = session_model_display(
        {LAUNCHED_MODEL_LABEL: "gemini-4-neon"},
        "gemini-4-neon",
        "Gemini 4 Neon (High)",
        harness="antigravity-native",
    )
    assert agy.launched is None


@pytest.mark.asyncio
async def test_session_model_event_marks_only_confirmed_harness_reports(monkeypatch):
    """Speculative router picks omit reported=True while harness reports carry it."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from omnigent.entities import Conversation
    from omnigent.runtime import session_stream
    from omnigent.server.routes._sessions import helpers, orchestration

    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        session_stream, "publish", lambda _sid, payload: published.append(dict(payload))
    )

    orchestration._publish_routed_model("conv_route", "gpt-6.2-sol")
    assert len(published) == 1
    assert published[0].get("reported") is not True

    conv = Conversation(
        id="conv_ext",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_ext",
        agent_id="ag_test",
        labels={},
    )
    store = MagicMock()
    store.get_conversation.return_value = conv
    await helpers._persist_external_model_change(
        "conv_ext",
        conv,
        SimpleNamespace(type="external_model_change", data={"model": "gpt-6.2-sol"}),
        store,
    )
    assert published[-1].get("reported") is True

    await orchestration._persist_relay_reported_model(
        {"usage": {"model": "claude-opus-4-8"}},
        "conv_ext",
        store,
    )
    assert published[-1].get("reported") is True


def test_clear_session_resolution_labels_keeps_member_notes_and_drops_session_notes():
    import json

    from omnigent.server.model_resolution import (
        RESOLUTION_LABEL,
        clear_session_resolution_labels,
        notes_from_labels,
    )

    mixed = {
        RESOLUTION_LABEL: json.dumps(
            [
                {
                    "setting": "session",
                    "saved": "old/x",
                    "model": "new/x",
                    "outcome": "renamed",
                    "host": "host-a",
                },
                {
                    "setting": "member:worker",
                    "saved": "old/y",
                    "model": "new/y",
                    "outcome": "renamed",
                    "host": "host-a",
                },
            ]
        )
    }
    to_set, to_delete = clear_session_resolution_labels(mixed)
    assert to_delete == []
    assert notes_from_labels(to_set) == [
        {
            "setting": "member:worker",
            "saved": "old/y",
            "model": "new/y",
            "outcome": "renamed",
            "host": "host-a",
        }
    ]

    session_only = {
        RESOLUTION_LABEL: json.dumps(
            [
                {
                    "setting": "session",
                    "saved": "old/x",
                    "model": "new/x",
                    "outcome": "renamed",
                    "host": "host-a",
                }
            ]
        )
    }
    to_set_only, to_delete_only = clear_session_resolution_labels(session_only)
    assert to_set_only == {}
    assert to_delete_only == [RESOLUTION_LABEL]


@pytest.mark.asyncio
async def test_record_launched_model_patches_label_and_arg_value_extracts_model():
    from unittest.mock import AsyncMock, MagicMock

    from omnigent.runner.native.orchestration import _arg_value, _record_launched_model
    from omnigent.server.model_resolution import LAUNCHED_MODEL_LABEL

    assert _arg_value(["--provider", "omnigent", "--model", "omnigent/k2.5"], "--model") == (
        "omnigent/k2.5"
    )
    assert _arg_value(["--thinking", "high"], "--model") is None

    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client.patch = AsyncMock(return_value=resp)

    await _record_launched_model("conv_1", client, "fallback-b")
    client.patch.assert_awaited_once_with(
        "/v1/sessions/conv_1",
        json={"labels": {LAUNCHED_MODEL_LABEL: "fallback-b"}},
        params={"include_usage": "false"},
        timeout=10.0,
    )

    client.patch.reset_mock()
    await _record_launched_model("conv_1", client, None)
    client.patch.assert_awaited_once_with(
        "/v1/sessions/conv_1",
        json={"labels": {LAUNCHED_MODEL_LABEL: ""}},
        params={"include_usage": "false"},
        timeout=10.0,
    )
