"""Native prompt admission shares one pending input across hook processes."""

import time
from pathlib import Path

import pytest

from omnigent.native.auto_retry import (
    AutoRetryGuard,
    NativeRetryRejected,
    admit_prompt,
    auto_retry_scope,
    get_snapshot,
    observe_turn,
    prepare_submission,
    read_receipt,
)


def _guard(bridge: Path) -> AutoRetryGuard:
    snapshot = observe_turn(bridge, "child", "failed-turn", idle=True)
    return AutoRetryGuard(
        session_id="child",
        chain_id="c" * 32,
        input_id="a" * 32,
        expected_turn_id="failed-turn",
        expected_input_generation=8,
        expected_native_generation=snapshot.generation,
        admission_deadline=time.time() + 60,
        attempt=1,
        of=3,
    )


@pytest.mark.parametrize("human_first", [True, False])
def test_non_retry_human_input_supersedes_automatic_identity(tmp_path, human_first):
    guard = _guard(tmp_path)
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
    if not human_first:
        automatic = admit_prompt(tmp_path, "child", "retry", turn_id="auto-turn")
        assert automatic.auto_retry == {
            "chain_id": "c" * 32,
            "input_id": "a" * 32,
            "attempt": 1,
            "of": 3,
        }
    human = admit_prompt(tmp_path, "child", "work on the new request", turn_id="human-turn")
    assert human.block is False and human.auto_retry is None
    if human_first:
        automatic = admit_prompt(tmp_path, "child", "retry", turn_id="auto-turn")
        assert automatic.block is True and automatic.auto_retry is None
    assert read_receipt(tmp_path, guard.input_id).reason == "superseded_by_human_input"
    assert get_snapshot(tmp_path, "child").turn_id == "human-turn"


def test_human_retry_is_plain_unless_it_races_a_pending_automatic_input(tmp_path):
    guard = _guard(tmp_path)
    human = admit_prompt(tmp_path, "child", "retry", turn_id="plain-turn")
    assert human.auto_retry is None and not human.block
    observe_turn(tmp_path, "child", "plain-turn", idle=True)
    snapshot = get_snapshot(tmp_path, "child")
    guard = guard.model_copy(
        update={
            "expected_turn_id": "plain-turn",
            "expected_native_generation": snapshot.generation,
        }
    )
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
    # D5a accepts ambiguity when a human types the exact automatic text.
    raced = admit_prompt(tmp_path, "child", "retry", turn_id="raced-turn")
    assert raced.auto_retry["input_id"] == guard.input_id and not raced.block
    extra = admit_prompt(tmp_path, "child", "retry", turn_id="extra-turn")
    assert extra.auto_retry is None and not extra.block


@pytest.mark.parametrize("native_idle", [False, None])
def test_executor_guard_rejects_busy_or_unreadable_native_state(tmp_path, native_idle):
    guard = _guard(tmp_path)
    with auto_retry_scope(guard), pytest.raises(NativeRetryRejected) as error:
        prepare_submission(tmp_path, native_idle=native_idle)
    assert error.value.reason == "native_not_idle"
    assert read_receipt(tmp_path, guard.input_id).state == "rejected"


def test_human_wins_before_executor_guard_even_after_its_turn_finishes(tmp_path):
    guard = _guard(tmp_path)
    admit_prompt(tmp_path, "child", "replace it", turn_id="human-turn")
    observe_turn(tmp_path, "child", "human-turn", idle=True)
    with auto_retry_scope(guard), pytest.raises(NativeRetryRejected) as error:
        prepare_submission(tmp_path, native_idle=True)
    assert error.value.reason == "superseded_by_human_input"


def test_hook_without_blocking_support_returns_explicit_residual_receipt(tmp_path):
    guard = _guard(tmp_path)
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
    admit_prompt(tmp_path, "child", "new task", turn_id="human-turn")
    result = admit_prompt(tmp_path, "child", "retry", turn_id="auto-turn", can_block=False)
    assert result.block is False and result.auto_retry is None
    assert result.reason == "superseded_by_human_input"
    assert read_receipt(tmp_path, guard.input_id).hook_can_block is False


def test_late_terminal_cannot_make_a_new_native_turn_idle(tmp_path):
    _guard(tmp_path)
    admit_prompt(tmp_path, "child", "new task", turn_id="human-turn")
    observe_turn(tmp_path, "child", "failed-turn", idle=True)
    snapshot = get_snapshot(tmp_path, "child")
    assert snapshot.turn_id == "human-turn" and snapshot.idle is False


def test_preparation_is_idempotent_but_not_after_hook_admission(tmp_path):
    guard = _guard(tmp_path)
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
        prepare_submission(tmp_path, native_idle=True)
    admit_prompt(tmp_path, "child", "retry", turn_id="auto-turn")
    with auto_retry_scope(guard), pytest.raises(NativeRetryRejected):
        prepare_submission(tmp_path, native_idle=True)
    assert read_receipt(tmp_path, guard.input_id).state == "admitted"


@pytest.mark.parametrize(
    "abandonment", ["cancelled", "cancel_lock_error", "expired", "dead_owner"]
)
def test_abandoned_input_cannot_be_claimed_as_a_later_human_retry(
    tmp_path, monkeypatch, abandonment
):
    from omnigent.native import auto_retry

    guard = _guard(tmp_path)
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
        if abandonment in {"cancelled", "cancel_lock_error"}:
            with monkeypatch.context() as failure:
                if abandonment == "cancel_lock_error":

                    def locked(*args):
                        raise OSError("state lock unavailable during abandonment")

                    failure.setattr(auto_retry, "_lock", locked)
                with pytest.raises(RuntimeError), auto_retry.submission_scope(tmp_path):
                    raise RuntimeError("delivery cancelled after preparation")
    if abandonment == "expired":
        monkeypatch.setattr(auto_retry.time, "time", lambda: guard.admission_deadline + 1)
    elif abandonment == "dead_owner":
        monkeypatch.setattr(auto_retry, "process_birth", lambda pid: None)
    first = admit_prompt(tmp_path, "child", "retry")
    assert first.block and first.auto_retry is None
    assert read_receipt(tmp_path, guard.input_id).reason == "input_abandoned"
    plain = admit_prompt(tmp_path, "child", "retry")
    assert not plain.block and plain.auto_retry is None
