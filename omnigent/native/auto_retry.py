"""Bridge-local admission for automatic inputs and native prompt hooks."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.native.windows_processes import process_birth
from omnigent.util.atomic_json import atomic_write_json

_STATE_FILE = "auto-retry.json"
_guard: ContextVar[AutoRetryGuard | None] = ContextVar("auto_retry_guard", default=None)


class AutoRetryGuard(BaseModel):
    """Trusted transport fields; the server still owns its admission generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    chain_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_turn_id: str = Field(min_length=1)
    expected_input_generation: int = Field(strict=True, ge=0)
    expected_native_generation: int = Field(strict=True, ge=0)
    admission_deadline: float = Field(allow_inf_nan=False)
    attempt: int = Field(strict=True, ge=1)
    of: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def bounded_attempt(self) -> AutoRetryGuard:
        if self.attempt > self.of:
            raise ValueError("Retry attempt exceeds its limit")
        return self

    def marker(self) -> dict[str, str | int]:
        return {key: getattr(self, key) for key in ("chain_id", "input_id", "attempt", "of")}


class NativeSnapshot(BaseModel):
    session_id: str
    generation: int = Field(default=0, strict=True, ge=0)
    turn_id: str | None = None
    idle: bool = False
    intervention_id: str | None = None


class NativeRetryReceipt(BaseModel):
    guard: AutoRetryGuard
    state: Literal["prepared", "admitted", "superseded", "rejected"] = "prepared"
    reason: str | None = None
    turn_id: str | None = None
    hook_can_block: bool = True
    awaiting_hook: bool = True
    owner_pid: int = Field(default_factory=os.getpid)
    owner_birth: float | None = Field(default_factory=lambda: process_birth(os.getpid()))


class PromptAdmission(BaseModel):
    block: bool = False
    reason: str | None = None
    turn_id: str
    native_generation: int
    auto_retry: dict[str, str | int] | None = None


class _BridgeState(BaseModel):
    snapshot: NativeSnapshot
    receipt: NativeRetryReceipt | None = None


class NativeRetryRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@contextlib.contextmanager
def auto_retry_scope(guard: AutoRetryGuard | None) -> Iterator[None]:
    token = _guard.set(guard)
    try:
        yield
    finally:
        _guard.reset(token)


def current_guard() -> AutoRetryGuard | None:
    return _guard.get()


def invalidate_for_human(bridge_dir: Path, *, reason: str = "superseded_by_human_input") -> None:
    """Invalidate pending automation independently of the state lock."""
    path = bridge_dir / "auto-retry-intervention.json"
    if path.is_symlink():
        raise OSError("Refusing symlinked native intervention state")
    value: dict[str, object] = {"id": uuid.uuid4().hex, "reason": reason}
    try:
        atomic_write_json(path, value)
    except OSError:
        # A prepared attempt already has this inode. In-place invalidation also
        # works when replacement fails (for example a full or read-only directory).
        try:
            with path.open("r+") as handle:
                json.dump(value, handle)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            os.utime(path, None)


def _intervention_id(bridge_dir: Path) -> str | None:
    path = bridge_dir / "auto-retry-intervention.json"
    if path.is_symlink():
        raise OSError("Refusing symlinked native intervention state")
    try:
        value = json.loads(path.read_text())["id"]
    except FileNotFoundError:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid native intervention identity")
    stat = path.stat()
    return f"{value}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def _invalidation_reason(bridge_dir: Path, previous_id: str | None) -> str:
    current = _intervention_id(bridge_dir)
    if previous_id and current and previous_id.split(":")[0] == current.split(":")[0]:
        # Metadata changed without a new record: the cause could not be written.
        return "input_admission_unavailable"
    record = json.loads((bridge_dir / "auto-retry-intervention.json").read_text())
    return (
        "input_abandoned"
        if record.get("reason") == "input_abandoned"
        else "superseded_by_human_input"
    )


def abandon_submission(bridge_dir: Path, reason: str = "input_abandoned") -> None:
    guard = current_guard()
    if guard is None:
        return
    with _lock(bridge_dir):
        state = _read(bridge_dir, guard.session_id)
        if state.receipt and state.receipt.guard == guard and state.receipt.state == "prepared":
            state.receipt.state, state.receipt.reason = "rejected", reason
            _write(bridge_dir, state)


@contextlib.contextmanager
def submission_scope(bridge_dir: Path) -> Iterator[None]:
    try:
        yield
    except BaseException:
        if current_guard() is not None:
            with contextlib.suppress(Exception):
                invalidate_for_human(bridge_dir, reason="input_abandoned")
                abandon_submission(bridge_dir)
        raise


def _lock(bridge_dir: Path) -> FileLock:
    bridge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return FileLock(str(bridge_dir / "auto-retry.lock"), timeout=2, mode=0o600)


def _read(bridge_dir: Path, session_id: str) -> _BridgeState:
    path = bridge_dir / _STATE_FILE
    if path.is_symlink():
        raise OSError("Refusing symlinked native retry state")
    try:
        state = _BridgeState.model_validate_json(path.read_text())
    except FileNotFoundError:
        return _BridgeState(snapshot=NativeSnapshot(session_id=session_id))
    return state


def _write(bridge_dir: Path, state: _BridgeState) -> None:
    atomic_write_json(bridge_dir / _STATE_FILE, state.model_dump(mode="json"))


def get_snapshot(bridge_dir: Path, session_id: str) -> NativeSnapshot:
    with _lock(bridge_dir):
        snapshot = _read(bridge_dir, session_id).snapshot
        if snapshot.intervention_id != _intervention_id(bridge_dir):
            snapshot.idle = False
        return (
            snapshot
            if snapshot.session_id == session_id
            else NativeSnapshot(session_id=session_id)
        )


def read_receipt(bridge_dir: Path, input_id: str) -> NativeRetryReceipt | None:
    with _lock(bridge_dir):
        path = bridge_dir / _STATE_FILE
        if path.is_symlink():
            raise OSError("Refusing symlinked native retry state")
        if not path.exists():
            return None
        state = _BridgeState.model_validate_json(path.read_text())
        if (
            state.receipt
            and state.receipt.state in {"prepared", "admitted"}
            and state.snapshot.intervention_id != _intervention_id(bridge_dir)
        ):
            reason = _invalidation_reason(bridge_dir, state.snapshot.intervention_id)
            state.receipt.state = (
                "superseded" if reason == "superseded_by_human_input" else "rejected"
            )
            state.receipt.reason = reason
            _write(bridge_dir, state)
        return (
            state.receipt if state.receipt and state.receipt.guard.input_id == input_id else None
        )


def observe_turn(bridge_dir: Path, session_id: str, turn_id: str, *, idle: bool) -> NativeSnapshot:
    """Retain the predecessor after completion; ignore another turn's late terminal."""
    with _lock(bridge_dir):
        state = _read(bridge_dir, session_id)
        if state.snapshot.session_id != session_id:
            state = _BridgeState(snapshot=NativeSnapshot(session_id=session_id))
        if not idle or state.snapshot.turn_id in {None, turn_id}:
            state.snapshot.turn_id, state.snapshot.idle = turn_id, idle
            if _intervention_id(bridge_dir) is None:
                invalidate_for_human(bridge_dir)
            state.snapshot.intervention_id = _intervention_id(bridge_dir)
            _write(bridge_dir, state)
        return state.snapshot


def prepare_submission(bridge_dir: Path, *, native_idle: bool | None, text: str = "retry") -> None:
    """Call under the executor injection lock immediately before native submission."""
    guard = current_guard()
    if guard is None:
        return
    reason = None
    with _lock(bridge_dir):
        state = _read(bridge_dir, guard.session_id)
        if state.snapshot.session_id != guard.session_id:
            raise NativeRetryRejected("session_changed")
        previous = state.receipt
        if previous and previous.guard.input_id == guard.input_id:
            if previous.guard != guard:
                raise ValueError("Retry input reused with different guard")
            if previous.state != "prepared":
                raise NativeRetryRejected(previous.reason or "input_already_admitted")
        if time.time() >= guard.admission_deadline:
            reason = "input_expired"
        elif state.snapshot.intervention_id != _intervention_id(bridge_dir):
            reason = _invalidation_reason(bridge_dir, state.snapshot.intervention_id)
        elif text != "retry":
            reason = "invalid_retry_input"
        elif (state.snapshot.turn_id, state.snapshot.generation) != (
            guard.expected_turn_id,
            guard.expected_native_generation,
        ):
            reason = "superseded_by_human_input"
        elif native_idle is not True or not state.snapshot.idle:
            reason = "native_not_idle"
        state.receipt = NativeRetryReceipt(
            guard=guard,
            state="rejected" if reason else "prepared",
            reason=reason,
            awaiting_hook=reason is None,
        )
        _write(bridge_dir, state)
    if reason:
        raise NativeRetryRejected(reason)


def admit_prompt(
    bridge_dir: Path,
    session_id: str,
    text: str,
    *,
    turn_id: str | None = None,
    can_block: bool = True,
) -> PromptAdmission:
    """D5a: exact text can claim a pending input; every other input supersedes it."""
    turn_id = turn_id or f"native_prompt_{uuid.uuid4().hex}"
    if text != "retry":
        invalidate_for_human(bridge_dir)
    with _lock(bridge_dir):
        state = _read(bridge_dir, session_id)
        if state.snapshot.session_id != session_id:
            state = _BridgeState(snapshot=NativeSnapshot(session_id=session_id))
        receipt = state.receipt
        result = PromptAdmission(turn_id=turn_id, native_generation=state.snapshot.generation)
        if (
            text == "retry"
            and receipt
            and receipt.awaiting_hook
            and receipt.state in {"prepared", "superseded", "rejected"}
        ):
            receipt.hook_can_block = can_block
            receipt.awaiting_hook = False
            if receipt.state == "rejected":
                result.block, result.reason = can_block, receipt.reason
            elif time.time() >= receipt.guard.admission_deadline or (
                receipt.owner_birth is None
                or process_birth(receipt.owner_pid) != receipt.owner_birth
            ):
                receipt.state, receipt.reason = "rejected", "input_abandoned"
                result.block, result.reason = can_block, receipt.reason
            elif (
                receipt.state == "superseded"
                or state.snapshot.generation != receipt.guard.expected_native_generation
                or state.snapshot.intervention_id != _intervention_id(bridge_dir)
            ):
                reason = (
                    receipt.reason
                    if receipt.state == "superseded"
                    else _invalidation_reason(bridge_dir, state.snapshot.intervention_id)
                )
                receipt.state = (
                    "superseded" if reason == "superseded_by_human_input" else "rejected"
                )
                receipt.reason = reason
                result.block, result.reason = can_block, receipt.reason
            else:
                receipt.state, receipt.turn_id = "admitted", turn_id
                result.auto_retry = receipt.guard.marker()
        elif receipt and receipt.state in {"prepared", "admitted"}:
            receipt.state, receipt.reason = "superseded", "superseded_by_human_input"
        if not result.block:
            state.snapshot.generation += 1
            state.snapshot.turn_id, state.snapshot.idle = turn_id, False
            result.native_generation = state.snapshot.generation
            state.snapshot.intervention_id = _intervention_id(bridge_dir)
        _write(bridge_dir, state)
        return result


def main() -> None:
    """Let the resident Pi extension use the same cross-process state implementation."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", type=Path, required=True)
    args = parser.parse_args()
    request = json.load(sys.stdin)
    if request["action"] == "prompt":
        result = admit_prompt(
            args.bridge_dir, request["session_id"], request["text"], turn_id=request.get("turn_id")
        )
    else:
        result = observe_turn(
            args.bridge_dir, request["session_id"], request["turn_id"], idle=request["idle"]
        )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
