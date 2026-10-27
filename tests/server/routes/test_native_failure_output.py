"""Failed or cancelled native turns must not reuse a previous assistant reply."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from omnigent.entities import ConversationItem, MessageData, PagedList
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.orchestration import (
    TERMINAL_STATUS_FALLBACK_OUTPUTS,
    _append_pending_deferred_approvals,
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.stores.conversation_store import ConversationStore, DetachedCard

# Fork mod: a failed/stopped/killed terminal edge always carries an output.
# Where upstream leaves the key absent, this fork substitutes a status-shaped
# placeholder — so "did not report an old success" is asserted as "reported the
# placeholder", except for a cancelled idle, which has no idle fallback and
# reports nothing.
_FAILED_FALLBACK = TERMINAL_STATUS_FALLBACK_OUTPUTS["failed"]


def _message(role: str, text: str, response_id: str, *, is_meta: bool = False) -> ConversationItem:
    return ConversationItem(
        id=f"{response_id}_{role}",
        type="message",
        status="completed",
        response_id=response_id,
        created_at=1,
        data=MessageData(
            role=role,
            agent="worker" if role == "assistant" else None,
            is_meta=is_meta,
            content=[{"type": "text", "text": text}],
        ),
    )


async def _enrich(
    data: dict, items: list[ConversationItem], *, cards: tuple[DetachedCard, ...] = ()
) -> dict:
    store = Mock(spec=ConversationStore)
    store.list_items.return_value = PagedList(data=items)
    store.list_detached_cards.return_value = list(cards)
    enriched = await _enrich_terminal_status_with_subagent_output(
        data, data["status"], "conv_test", store
    )
    return await _append_pending_deferred_approvals(enriched, "conv_test", store)


@pytest.fixture
def approval_card() -> DetachedCard:
    return DetachedCard(
        elicitation_id="elicit_first",
        session_id="conv_test",
        kind="approval",
        state="pending",
        mirror=True,
        params={},
        payload={
            "aid": "a111111",
            "action": "Bash(pnpm test)",
            "origin": "claude-permission-prompt",
        },
        grant_key=None,
        verdict=None,
        delivery_text=None,
        created_at=100,
        expires_at=300,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["idle", "completed", "failed", "stopped", "killed"])
@pytest.mark.parametrize("source", ["forwarder", "store"])
async def test_terminal_delivery_appends_one_pending_approval_list(
    status: str, source: str, approval_card: DetachedCard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "now_epoch", lambda: 200)
    data = {"status": status}
    if source == "forwarder":
        data["output"] = "Child result"
    result = await _enrich(
        data,
        [_message("assistant", "Child result" if source == "store" else "Old reply", "resp")],
        cards=(
            approval_card,
            replace(
                approval_card,
                elicitation_id="elicit_second",
                payload={
                    "aid": "a222222",
                    "action": "Edit(file.py)",
                    "origin": "omnigent-policy-unavailable",
                },
            ),
        ),
    )
    assert result["output"] == (
        "Child result\n\nPending deferred approvals:\n"
        "- a111111: Bash(pnpm test) (origin: claude-permission-prompt)\n"
        "- a222222: Edit(file.py) (origin: omnigent-policy-unavailable)"
    )
    if status == "failed":
        assert result["failure_context"]["detail_source"] == (
            "external_status_output" if source == "forwarder" else "assistant_output_fallback"
        )


@pytest.mark.asyncio
async def test_terminal_delivery_filters_cards_and_labels_legacy_origin(
    approval_card: DetachedCard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "now_epoch", lambda: 200)
    result = await _enrich(
        {"status": "completed", "output": "Child result"},
        [],
        cards=(
            replace(approval_card, state="answered"),
            replace(approval_card, state="settled"),
            replace(approval_card, expires_at=200),
            replace(approval_card, session_id="conv_other"),
            replace(approval_card, kind="question"),
            replace(approval_card, payload={"aid": "alegacy", "action": "Bash(pnpm lint)"}),
        ),
    )
    assert result["output"] == (
        "Child result\n\nPending deferred approvals:\n- alegacy: Bash(pnpm lint) (origin: unknown)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["forwarder", "store", "none"])
async def test_terminal_delivery_without_cards_keeps_output_unchanged(source: str) -> None:
    data = {"status": "idle"}
    if source == "forwarder":
        data["output"] = " Child result "
    items = [_message("assistant", "Child result", "resp")] if source == "store" else []
    result = await _enrich(data, items)
    assert result == ({**data, "output": "Child result"} if source == "store" else data)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("database unavailable"), ValueError("bad JSON")])
async def test_pending_approval_lookup_failure_keeps_terminal_output(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    store = Mock(spec=ConversationStore)
    store.list_detached_cards.side_effect = error
    data = {"status": "failed", "output": "Original failure"}
    assert await _append_pending_deferred_approvals(data, "conv_test", store) == data
    assert "Could not read pending approvals" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["output", "failure_detail"])
async def test_pending_approvals_preserve_wire_failure_precedence(
    source: str, approval_card: DetachedCard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "now_epoch", lambda: 200)
    result = await _enrich(
        {"status": "failed", source: "Actual failure"},
        [_message("assistant", "Old reply", "resp")],
        cards=(approval_card,),
    )
    assert result["output"] == (
        "Actual failure\n\nPending deferred approvals:\n"
        "- a111111: Bash(pnpm test) (origin: claude-permission-prompt)"
    )
    assert result["failure_context"]["detail_source"] == f"external_status_{source}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
@pytest.mark.parametrize("response_id", [None, "resp_new"])
async def test_failed_turn_does_not_report_an_old_success(
    response_id: str | None, status: dict
) -> None:
    data = dict(status)
    if response_id is not None:
        data["response_id"] = response_id
    result = await _enrich(
        data,
        [
            _message("user", "Next task", "resp_new"),
            _message("assistant", "The previous task succeeded.", "resp_old"),
        ],
    )
    if status.get("turn_outcome") == "cancelled":
        # Cancelled-idle carries no fallback: the turn stopped before output.
        assert "output" not in result
    else:
        assert result["output"] == _FAILED_FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
async def test_response_id_prevents_reusing_an_old_reply_without_a_user_item(status: dict) -> None:
    result = await _enrich(
        {**status, "response_id": "resp_new"},
        [_message("assistant", "Previous reply", "resp_old")],
    )
    if status.get("turn_outcome") == "cancelled":
        # Cancelled-idle carries no fallback: the turn stopped before output.
        assert "output" not in result
    else:
        assert result["output"] == _FAILED_FALLBACK


@pytest.mark.asyncio
async def test_failed_turn_selects_its_own_detail_after_a_later_reply() -> None:
    result = await _enrich(
        {"status": "failed", "response_id": "resp_failed"},
        [
            _message("assistant", "Later turn succeeded", "resp_later"),
            _message("assistant", "Provider rejected this turn", "resp_failed"),
        ],
    )
    assert result["output"] == "Provider rejected this turn"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [{"status": "failed"}, {"status": "idle", "turn_outcome": "cancelled"}]
)
async def test_legacy_failure_keeps_current_reply_and_ignores_meta_messages(status: dict) -> None:
    result = await _enrich(
        status,
        [
            _message("user", "Internal notice", "resp_meta", is_meta=True),
            _message("assistant", "Provider rejected this turn", "resp_current"),
            _message("user", "Current task", "resp_current"),
        ],
    )
    assert result["output"] == "Provider rejected this turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["output", "failure_detail"])
@pytest.mark.parametrize("structured", [False, True])
async def test_wire_failure_reason_wins_over_stored_text(source: str, structured: bool) -> None:
    reason = "Actual failure"
    if structured:
        reason = json.dumps(
            {
                "error_code": "BAD_REQUEST",
                "message": json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "array_above_max_length",
                            "param": "input[3].content",
                            "message": "Invalid input content count.",
                        }
                    }
                ),
            }
        )
    data = {"status": "failed", "response_id": "resp_new", source: reason}
    result = await _enrich(data, [_message("assistant", "Old reply", "resp_old")])
    context = result.pop("failure_context")
    assert context["detail_source"] == f"external_status_{source}"
    if structured:
        assert context["provider_error_type"] == "invalid_request_error"
        assert context["provider_error_code"] == "array_above_max_length"
        assert context["provider_error_param"] == "input[3].content"
    else:
        assert "provider_error_code" not in context
    assert result == {**data, "output": reason}


@pytest.mark.asyncio
async def test_harness_failure_detail_wins_over_stored_prose() -> None:
    result = await _enrich(
        {
            "status": "failed",
            "response_id": "resp_failed",
            "failure_detail": " API Error: 500 Overloaded ",
        },
        [_message("assistant", "Now add the block on the new branch:", "resp_failed")],
    )
    assert result["output"] == "API Error: 500 Overloaded"
    assert result["failure_context"]["detail_source"] == "external_status_failure_detail"


@pytest.mark.asyncio
async def test_structured_source_evidence_wins_over_parsed_wire_text() -> None:
    result = await _enrich(
        {
            "status": "failed",
            "output": json.dumps({"error": {"code": "weaker-text-code", "param": "input"}}),
            "failure_context": {"provider_error_code": "source-code"},
        },
        [],
    )
    assert result["failure_context"]["provider_error_code"] == "source-code"
    assert result["failure_context"]["provider_error_param"] == "input"


@pytest.mark.asyncio
async def test_blank_failure_detail_falls_back_to_the_store() -> None:
    result = await _enrich(
        {"status": "failed", "response_id": "resp_failed", "failure_detail": "  "},
        [_message("assistant", "Provider rejected this turn", "resp_failed")],
    )
    assert result["output"] == "Provider rejected this turn"
    assert result["failure_context"]["detail_source"] == "assistant_output_fallback"


@pytest.mark.asyncio
async def test_failure_without_output_marks_missing_detail_and_keeps_category() -> None:
    result = await _enrich(
        {"status": "failed", "failure_context": {"native_error_category": "server_error"}}, []
    )
    assert result["output"] == _FAILED_FALLBACK
    assert result["failure_context"]["native_error_category"] == "server_error"
    assert result["failure_context"]["detail_source"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_prose", [False, True])
async def test_store_fallback_overrides_stale_claimed_provenance(structured_prose: bool) -> None:
    text = "I am waiting for a background task."
    if structured_prose:
        text = json.dumps({"error": {"code": "example-from-assistant-prose"}})
    result = await _enrich(
        {
            "status": "failed",
            "response_id": "resp_failed",
            "failure_context": {
                "native_error_category": "server_error",
                "detail_source": "missing",
            },
        },
        [_message("assistant", text, "resp_failed")],
    )
    assert result["failure_context"]["native_error_category"] == "server_error"
    assert result["failure_context"]["detail_source"] == "assistant_output_fallback"
    assert "provider_error_code" not in result["failure_context"]


@pytest.mark.asyncio
async def test_idle_enrichment_retains_its_existing_behavior() -> None:
    result = await _enrich(
        {"status": "idle"},
        [
            _message("user", "Follow-up", "resp_new"),
            _message("assistant", "Completed output", "resp_old"),
        ],
    )
    assert result["output"] == "Completed output"
