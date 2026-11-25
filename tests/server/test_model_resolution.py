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
    assert _arg_value(["--model=custom/pi-model", "--thinking", "high"], "--model") == (
        "custom/pi-model"
    )
    assert _arg_value(["--model", "first-model", "--model=second-model"], "--model") == (
        "second-model"
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

    # Unchanged or empty-to-empty launches skip the HTTP PATCH altogether.
    client.patch.reset_mock()
    await _record_launched_model("conv_1", client, None)
    await _record_launched_model("conv_1", client, "fallback-b", current_launched="fallback-b")
    client.patch.assert_not_awaited()

    # Read-only snapshot clients without .patch are a safe no-op.
    await _record_launched_model("conv_1", object(), "fallback-b")  # type: ignore[arg-type]

    # Launching without a model clears a stale prior launched_model label.
    await _record_launched_model("conv_1", client, None, current_launched="fallback-b")
    client.patch.assert_awaited_once_with(
        "/v1/sessions/conv_1",
        json={"labels": {LAUNCHED_MODEL_LABEL: ""}},
        params={"include_usage": "false"},
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_sdk_turn_delivery_acknowledges_even_when_launched_model_write_fails_or_stalls(
    monkeypatch,
):
    """P1: delivery returns and publishes input.consumed if launched_model write fails/stalls."""
    import asyncio
    import threading
    from unittest.mock import MagicMock

    import httpx

    from omnigent.entities import Conversation, ConversationItem
    from omnigent.runtime import session_stream
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.schemas import SessionEventInput

    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        session_stream, "publish", lambda _sid, payload: published.append(dict(payload))
    )
    monkeypatch.setattr(orchestration, "_is_native_terminal_session", lambda _c: False)

    async def _noop_async(*_a, **_kw):
        return None

    monkeypatch.setattr(orchestration, "claim_retry_forward", _noop_async)

    def _make_store(set_labels_fn):
        store = MagicMock()

        def _append(_cid, items):
            new_item = items[0]
            return [
                ConversationItem(
                    id="item_ack_1",
                    type=new_item.type,
                    response_id=new_item.response_id,
                    data=new_item.data,
                    status="completed",
                    created_at=100,
                    created_by=new_item.created_by,
                )
            ]

        store.append.side_effect = _append
        store.set_labels.side_effect = set_labels_fn
        store.get_conversation.return_value = None
        return store

    runner_client = MagicMock()
    runner_client.post = MagicMock(
        side_effect=lambda *_a, **_kw: asyncio.sleep(0, result=httpx.Response(202, json={}))
    )

    # Case 1: set_labels raises ConnectionError — must NOT enter runner-unreachable branch.
    conv_err = Conversation(
        id="conv_p1_err",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_p1_err",
        agent_id="ag_test",
        model_override="model-a",
        labels={},
    )
    store_err = _make_store(lambda *_a, **_kw: (_ for _ in ()).throw(ConnectionError("db down")))
    msg_event = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )
    item_id = await orchestration._forward_event_to_runner(
        conv_err.id,
        conv_err,
        msg_event,
        store_err,
        runner_client,
    )
    assert item_id == "item_ack_1"
    assert any(evt.get("type") == "session.input.consumed" for evt in published)
    if orchestration._LAUNCHED_MODEL_TASKS:
        await asyncio.gather(*list(orchestration._LAUNCHED_MODEL_TASKS), return_exceptions=True)

    # Case 2: set_labels stalls on an event — delivery returns immediately before unblock.
    unblock = threading.Event()
    started = threading.Event()

    def _blocking_set_labels(*_a, **_kw):
        started.set()
        unblock.wait(timeout=5.0)

    conv_stall = Conversation(
        id="conv_p1_stall",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_p1_stall",
        agent_id="ag_test",
        model_override="model-b",
        labels={},
    )
    store_stall = _make_store(_blocking_set_labels)
    try:
        item_id_2 = await asyncio.wait_for(
            orchestration._forward_event_to_runner(
                conv_stall.id,
                conv_stall,
                msg_event,
                store_stall,
                runner_client,
            ),
            timeout=1.0,
        )
        assert item_id_2 == "item_ack_1"
    finally:
        unblock.set()
        if orchestration._LAUNCHED_MODEL_TASKS:
            await asyncio.gather(
                *list(orchestration._LAUNCHED_MODEL_TASKS), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_fenced_sdk_launched_model_write_prevents_stale_dispatch_from_overwriting_newer(
    monkeypatch,
):
    """P1: a delayed older launched_model write cannot overwrite a newer dispatch."""
    import asyncio
    import threading
    from unittest.mock import MagicMock

    from omnigent.entities import Conversation
    from omnigent.server.model_resolution import LAUNCHED_MODEL_LABEL
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_is_native_terminal_session", lambda _c: False)

    written: list[str] = []
    release_first = threading.Event()
    first_entered = threading.Event()

    def _set_labels(_sid, labels):
        val = labels[LAUNCHED_MODEL_LABEL]
        if val == "model-a":
            first_entered.set()
            release_first.wait(timeout=5.0)
        written.append(val)

    store = MagicMock()
    store.set_labels.side_effect = _set_labels

    conv_a = Conversation(
        id="conv_fence",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_fence",
        agent_id="ag_test",
        model_override="model-a",
        labels={},
    )
    conv_b = Conversation(
        id="conv_fence",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_fence",
        agent_id="ag_test",
        model_override="model-b",
        labels={},
    )

    t1 = orchestration._schedule_sdk_launched_model_record(conv_a.id, conv_a, "model-a", store)
    assert t1 is not None
    await asyncio.to_thread(first_entered.wait, 2.0)
    t2 = orchestration._schedule_sdk_launched_model_record(conv_b.id, conv_b, "model-b", store)
    assert t2 is not None
    release_first.set()
    await asyncio.gather(t1, t2)
    assert written[-1] == "model-b"


def test_native_patch_confirmation_rejects_queued_pi_and_noop_clear():
    """P2: only harness-confirmed live model switches promote launched_model."""
    from omnigent.server.routes._sessions.helpers import _RunnerForwardResult
    from omnigent.server.routes.sessions.routes_core import _native_model_change_confirmed

    # Pi queued bridge 204 is NOT confirmation.
    assert (
        _native_model_change_confirmed(
            _RunnerForwardResult(status_code=204, body=""),
            model_override="omnigent/k2.5",
            clear_model=False,
            codex_native=False,
            agy_native=False,
            unconfirmed=False,
            unsupported=None,
            forward_failed=False,
        )
        is False
    )
    # No-op clear 204 keeps the last launch record.
    assert (
        _native_model_change_confirmed(
            _RunnerForwardResult(status_code=204, body="", model_applied=True),
            model_override=None,
            clear_model=True,
            codex_native=True,
            agy_native=False,
            unconfirmed=False,
            unsupported=None,
            forward_failed=False,
        )
        is False
    )
    # Confirmed Claude switch (204 + model_applied=True) promotes launched.
    assert (
        _native_model_change_confirmed(
            _RunnerForwardResult(status_code=204, body="", model_applied=True),
            model_override="opus",
            clear_model=False,
            codex_native=False,
            agy_native=False,
            unconfirmed=False,
            unsupported=None,
            forward_failed=False,
        )
        is True
    )
    # Confirmed Codex switch (200/204 non-deferred) promotes launched; 202 deferred does not.
    assert (
        _native_model_change_confirmed(
            _RunnerForwardResult(status_code=204, body="", model_applied=True),
            model_override="gpt-6.2-sol",
            clear_model=False,
            codex_native=True,
            agy_native=False,
            unconfirmed=False,
            unsupported=None,
            forward_failed=False,
        )
        is True
    )
    assert (
        _native_model_change_confirmed(
            _RunnerForwardResult(status_code=202, body='{"deferred": true}'),
            model_override="gpt-6.2-sol",
            clear_model=False,
            codex_native=True,
            agy_native=False,
            unconfirmed=False,
            unsupported=None,
            forward_failed=False,
        )
        is False
    )


def test_launched_model_label_rejected_for_generic_clients():
    """P2: generic client label seeding rejects omnigent.launched_model and collation variants."""
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions.helpers import _reject_server_reserved_label_seed

    for key in ("omnigent.launched_model", "OMNIGENT.LAUNCHED_MODEL", "omnigent.launched_model "):
        with pytest.raises(OmnigentError):
            _reject_server_reserved_label_seed({key: "fake-model"})


def test_mutate_session_resolution_notes_is_atomic_on_group():
    """P2: note cleanup runs through mutate_label_group, preserving concurrent member notes."""
    import json

    from omnigent.server.model_resolution import (
        RESOLUTION_LABEL,
        mutate_session_resolution_notes,
        notes_from_labels,
        resolution_labels,
    )
    from omnigent.stores.conversation_store import LabelGroupChange

    # Multi-chunk record containing one session note and one concurrent member note.
    member_note = {
        "setting": "member:worker",
        "saved": "old/" + ("y" * 120),
        "model": "new/" + ("y" * 120),
        "outcome": "renamed",
        "host": "host-a",
    }
    session_note = {
        "setting": "session",
        "saved": "old/" + ("x" * 120),
        "model": "new/" + ("x" * 120),
        "outcome": "renamed",
        "host": "host-a",
    }
    live_group = resolution_labels([session_note, member_note])
    assert len(live_group) > 1  # spans multiple chunks

    applied_changes: list[LabelGroupChange] = []

    class FakeStore:
        def mutate_label_group(self, _cid, prefix, change_fn):
            assert prefix == RESOLUTION_LABEL
            change = change_fn(dict(live_group))
            applied_changes.append(change)
            for k in change.deletes:
                live_group.pop(k, None)
            live_group.update(change.upserts)
            return dict(live_group)

    result = mutate_session_resolution_notes(
        FakeStore(),
        "conv_atomic",
        lambda notes: [n for n in notes if str(n.get("setting", "")).startswith("member:")],
    )
    assert len(applied_changes) == 1
    assert applied_changes[0].deletes  # excess chunk deleted atomically alongside upsert
    assert notes_from_labels(result) == [member_note]
    assert json.loads(result[RESOLUTION_LABEL])  # still decodable after atomic mutation
