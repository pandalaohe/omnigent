"""Native routing observations remain unsupported canonical-context evidence."""

from unittest.mock import AsyncMock

import pytest

from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.cursor_native_executor import CursorNativeExecutor
from omnigent.inner.devin_native_executor import DevinNativeExecutor
from omnigent.inner.executor import MockExecutor
from omnigent.inner.goose_native_executor import GooseNativeExecutor
from omnigent.inner.hermes_native_executor import HermesNativeExecutor
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.inner.kiro_native_executor import KiroNativeExecutor
from omnigent.inner.pi_native_executor import PiNativeExecutor
from omnigent.inner.qwen_native_executor import QwenNativeExecutor

NATIVE_CONTEXT_CASES = [
    ("claude-native", ClaudeNativeExecutor, "claude_native_primary_alias_lifecycle_unsupported"),
    ("pi-native", PiNativeExecutor, "pi_native_terminal_generation_binding_unsupported"),
    ("cursor-native", CursorNativeExecutor, "cursor_native_chat_owner_lifecycle_unsupported"),
    ("devin-native", DevinNativeExecutor, "devin_native_hook_context_membership_unsupported"),
    ("goose-native", GooseNativeExecutor, "goose_native_history_occupancy_unsupported"),
    ("hermes-native", HermesNativeExecutor, "hermes_native_history_alias_ownership_unsupported"),
    ("kimi-native", KimiNativeExecutor, "kimi_native_workdir_context_liveness_unsupported"),
    ("kiro-native", KiroNativeExecutor, "kiro_native_forward_cursor_owner_lifecycle_unsupported"),
    ("qwen-native", QwenNativeExecutor, "qwen_native_requested_id_receipt_unsupported"),
    (
        "antigravity-native",
        AntigravityNativeExecutor,
        "antigravity_native_conversation_owner_lifecycle_unsupported",
    ),
]


@pytest.mark.parametrize("family, factory, reason", NATIVE_CONTEXT_CASES)
async def test_native_context_reader_never_probes_bridge_or_injects_work(
    tmp_path, monkeypatch, family, factory, reason
):
    executor = factory(bridge_dir=tmp_path)
    # A reader that tried a bridge file, path or transcript operation would fail.
    executor._bridge_dir = object()
    for name in ("run_turn", "enqueue_session_message", "interrupt_session", "close_session"):
        monkeypatch.setattr(
            executor,
            name,
            AsyncMock(side_effect=AssertionError("no native work or lifecycle action")),
        )
    read = await executor.prepare_context_inventory_v2("session", "read")
    for _ in range(2):
        evidence = read()
        (source,) = evidence.sources
        assert evidence.read_id == "read" and evidence.authority.session_id == "session"
        assert source.coverage == "unsupported" and source.contexts == ()
        assert source.unresolved[0].reason == reason and reason.startswith(
            family.replace("-", "_")
        )
        assert source.producer_incarnation is source.work_bindings is None
        assert (
            source.membership_revision is source.context_revision is source.work_revision is None
        )
        assert source.age_upper_bound_s is None and source.revision_authority == "observation_only"
    for name in ("run_turn", "enqueue_session_message", "interrupt_session", "close_session"):
        getattr(executor, name).assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


async def test_legacy_default_reason_and_invalid_budget_remain_unknown():
    executor = MockExecutor()
    read = await executor.prepare_context_inventory_v2("session", "read")
    (source,) = read().sources
    assert source.coverage == "unsupported" and source.contexts == ()
    assert source.unresolved[0].reason == "context_source_unimplemented"
    for budget in (0, 6, float("nan")):
        invalid = await executor.prepare_context_inventory_v2("session", "read", timeout_s=budget)
        (source,) = invalid().sources
        assert source.coverage == "partial" and source.contexts == ()
        assert source.unresolved[0].reason == "invalid_read_budget"
