"""Read-only Host probe and guarded repair for native Claude sub-agents."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server import session_live_state
from omnigent.server.routes._sessions.common import (
    _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY,
    _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY,
    _LAST_TASK_ERROR_CAUSE_LABEL_KEY,
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY,
    _LAST_TASK_ERROR_TITLE_LABEL_KEY,
    _SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY,
    _SUBAGENT_STATUS_GENERATION_LABEL_KEY,
    _SUBAGENT_TERMINAL_STATUS_LABEL_KEY,
    _session_active_response_cache,
    _session_background_task_count_cache,
    _session_background_tasks_cache,
    _session_status_cache,
)
from omnigent.server.routes._sessions.helpers import _publish_child_status_to_parent
from omnigent.server.schemas import SessionStatusEvent
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import NativeSubagentReconcileFingerprint

_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"
_TERMINAL_STATUSES = ("completed", "failed", "stopped", "killed")
_FAILURE_LABEL_KEYS = (
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _LAST_TASK_ERROR_TITLE_LABEL_KEY,
    _LAST_TASK_ERROR_CAUSE_LABEL_KEY,
    _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY,
)
_FINGERPRINT_LABEL_KEYS = (
    _WRAPPER_LABEL_KEY,
    _BRIDGE_ID_LABEL_KEY,
    _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY,
    _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY,
    _SUBAGENT_TERMINAL_STATUS_LABEL_KEY,
    _SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY,
    _SUBAGENT_STATUS_GENERATION_LABEL_KEY,
    *_FAILURE_LABEL_KEYS,
)
_PARENT_RUNTIME_LABEL_KEYS = (_WRAPPER_LABEL_KEY, _BRIDGE_ID_LABEL_KEY)
_logger = logging.getLogger(__name__)


class _ProbeEvidence(BaseModel):
    """Stable, path-free evidence identity returned by the native runner."""

    model_config = ConfigDict(extra="ignore")

    claude_session_id: str
    parent_complete_byte_offset: int
    subagent_id: str
    meta_tool_use_id: str | None = None
    evidence_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ProbeChild(BaseModel):
    """One child verdict from ``native_subagent_status``."""

    model_config = ConfigDict(extra="ignore")

    server_session_id: str
    subagent_id: str
    tool_use_id: str | None = None
    status: Literal["terminal", "unverified"]
    terminal_status: Literal["completed", "failed", "stopped", "killed"] | None = None
    reason: str
    evidence: _ProbeEvidence


class _ProbeResponse(BaseModel):
    """Validated native-runner reconciliation response."""

    model_config = ConfigDict(extra="ignore")

    parent_session_id: str
    bridge_id: str
    claude_session_id: str
    parent_complete_byte_offset: int
    children: list[_ProbeChild]


def _fingerprint_labels(
    fingerprint: NativeSubagentReconcileFingerprint,
) -> dict[str, str | None]:
    """Project the immutable label triples to a key/value mapping."""
    return {key: value for key, value, _updated_at in fingerprint.label_states}


def _desired_terminal_state(
    terminal_status: str,
) -> tuple[str, dict[str, str]]:
    """Return the live status and labels for one reliable terminal verdict."""
    live_status = "failed" if terminal_status == "failed" else "idle"
    updates = {
        _SUBAGENT_TERMINAL_STATUS_LABEL_KEY: terminal_status,
        _SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY: "",
    }
    # A reliable failure keeps its structured failure detail. A successful or
    # user-stopped edge clears only stale failure labels from an earlier false
    # failure; it never touches messages or parent inbox state.
    if terminal_status != "failed":
        updates.update(dict.fromkeys(_FAILURE_LABEL_KEYS, ""))
    return live_status, updates


def _already_matches_terminal(
    fingerprint: NativeSubagentReconcileFingerprint,
    terminal_status: str,
) -> bool:
    """Return whether the frozen Server state already represents the verdict."""
    labels = _fingerprint_labels(fingerprint)
    desired_live, _updates = _desired_terminal_state(terminal_status)
    if fingerprint.live_status != desired_live:
        return False
    if labels.get(_SUBAGENT_TERMINAL_STATUS_LABEL_KEY) != terminal_status:
        return False
    if labels.get(_SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY) == "true":
        return False
    return terminal_status == "failed" or not any(labels.get(key) for key in _FAILURE_LABEL_KEYS)


def _display_fingerprint(session_id: str) -> tuple[Any, ...]:
    """Freeze transient state so a newer display edge is never cleared."""
    return (
        _session_status_cache.get(session_id),
        _session_active_response_cache.get(session_id),
        _session_background_task_count_cache.get(session_id),
        tuple(_session_background_tasks_cache.get(session_id, ())),
    )


def _display_state_matches_terminal(
    display: tuple[Any, ...],
    desired_live: str,
) -> bool:
    """Return whether transient UI state already agrees with durable terminal state."""
    cached_status, active_response, background_count, background_tasks = display
    return (
        cached_status in {None, desired_live}
        and active_response is None
        and background_count in {None, 0}
        and not background_tasks
    )


def _pending_fingerprint(
    parent_session_id: str,
    child_session_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Freeze child prompts and only that child's mirrored parent prompts."""
    observed = [
        (child_session_id, event) for event in pending_elicitations.snapshot_for(child_session_id)
    ]
    for event in pending_elicitations.snapshot_for(parent_session_id):
        params = event.get("params")
        if isinstance(params, dict) and params.get("target_session_id") == child_session_id:
            observed.append((parent_session_id, event))
    return observed


def _silently_clear_pending(
    observed: list[tuple[str, dict[str, Any]]],
) -> None:
    """Remove exact stale prompt generations and flip live cards without notices."""
    for session_id, event in observed:
        try:
            if not pending_elicitations.discard_stale(session_id, event):
                continue
            elicitation_id = event.get("elicitation_id")
            if isinstance(elicitation_id, str) and elicitation_id:
                session_stream.publish(
                    session_id,
                    {
                        "type": "response.elicitation_resolved",
                        "elicitation_id": elicitation_id,
                    },
                    track_pending=False,
                )
        except Exception:  # noqa: BLE001 - stale UI cleanup is best-effort.
            _logger.warning(
                "Could not silently clear stale elicitation for %s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )


async def _restore_repair_after_new_activity(
    *,
    conversation_store: ConversationStore,
    original: NativeSubagentReconcileFingerprint,
    repair_live_status: str,
    repair_label_updates: dict[str, str],
    current_display_status: object,
) -> None:
    """Undo only our still-current CAS when a newer transient edge won."""
    try:
        repaired = await asyncio.to_thread(
            conversation_store.get_native_subagent_reconcile_fingerprint,
            original.conversation_id,
            _FINGERPRINT_LABEL_KEYS,
        )
        if repaired is None or repaired.live_status != repair_live_status:
            return
        repaired_labels = _fingerprint_labels(repaired)
        if any(repaired_labels.get(key) != value for key, value in repair_label_updates.items()):
            return
        original_labels = _fingerprint_labels(original)
        restore_live = (
            current_display_status
            if current_display_status in {"idle", "running", "waiting", "failed"}
            else original.live_status
        )
        if restore_live is None:
            return
        await asyncio.to_thread(
            conversation_store.reconcile_native_subagent_status,
            repaired,
            live_status=restore_live,
            label_updates={key: original_labels.get(key) or "" for key in repair_label_updates},
        )
    except Exception:  # noqa: BLE001 - race compensation is best-effort and CAS guarded.
        _logger.warning(
            "Could not compensate raced native child reconciliation for %s",
            original.conversation_id,
            exc_info=True,
            extra={"session_id": original.conversation_id},
        )


def _runtime_identity(conversation: Any) -> tuple[str | None, ...]:
    """Return the Server binding identity that must survive the Host probe."""
    return (
        conversation.runner_id,
        conversation.host_id,
        conversation.external_session_id,
        conversation.labels.get(_WRAPPER_LABEL_KEY),
        conversation.labels.get(_BRIDGE_ID_LABEL_KEY),
    )


def _runtime_fingerprint_matches(
    conversation: Any,
    fingerprint: NativeSubagentReconcileFingerprint,
) -> bool:
    """Return whether a frozen parent still represents *conversation*."""
    labels = _fingerprint_labels(fingerprint)
    return (
        fingerprint.conversation_id == conversation.id
        and fingerprint.parent_conversation_id == conversation.parent_conversation_id
        and fingerprint.runner_id == conversation.runner_id
        and fingerprint.host_id == conversation.host_id
        and fingerprint.external_session_id == conversation.external_session_id
        and all(
            labels.get(key) == conversation.labels.get(key) for key in _PARENT_RUNTIME_LABEL_KEYS
        )
    )


def _unverified_detail(session_id: str, reason: str) -> dict[str, str]:
    return {"session_id": session_id, "outcome": "unverified", "reason": reason}


def _expected_evidence_key(
    parent_session_id: str,
    probe: _ProbeResponse,
    row: _ProbeChild,
) -> str | None:
    """Recompute the runner's path-free frozen-evidence fingerprint."""
    if row.terminal_status is None:
        return None
    material = "\0".join(
        (
            parent_session_id,
            probe.claude_session_id,
            str(probe.parent_complete_byte_offset),
            row.subagent_id,
            row.evidence.meta_tool_use_id or "",
            row.terminal_status,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def _read_native_subagent_probe(
    runner_client: httpx.AsyncClient,
    parent_session_id: str,
) -> _ProbeResponse:
    """GET frozen evidence without ensuring or waking the runner."""
    try:
        response = await runner_client.get(
            f"/v1/sessions/{parent_session_id}/native_subagent_status",
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        raise OmnigentError(
            "The Host is offline or unreachable; reconnect it and try again.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if response.status_code == 501:
        raise OmnigentError(
            "This Host cannot verify native sub-agent state. "
            "Update the custom Host and try again.",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )
    if response.status_code == 404:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error_code = body.get("error") if isinstance(body, dict) else None
        if error_code == "native_parent_not_found":
            raise OmnigentError(
                "The Host no longer has this native parent's transcript evidence; "
                "no state was changed.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        if error_code == "not_found":
            raise OmnigentError(
                "This session is not a Claude-native parent; no state was changed.",
                code=ErrorCode.INVALID_INPUT,
            )
        raise OmnigentError(
            "This Host cannot verify native sub-agent state. "
            "Update the custom Host and try again.",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )
    if response.status_code == 409:
        raise OmnigentError(
            "The native session changed while its status was being checked; no state was changed.",
            code=ErrorCode.CONFLICT,
        )
    if response.status_code != 200:
        raise OmnigentError(
            "The Host could not verify native sub-agent state; no state was changed.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    try:
        payload = _ProbeResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise OmnigentError(
            "The Host returned an invalid sub-agent status response; no state was changed.",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        ) from exc
    if payload.parent_session_id != parent_session_id:
        raise OmnigentError(
            "The Host returned status for a different native session; no state was changed.",
            code=ErrorCode.CONFLICT,
        )
    return payload


async def reconcile_native_subagents(
    *,
    parent_session_id: str,
    parent: Any,
    children: list[Any],
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Reconcile direct native children from reliable parent-transcript evidence."""
    frozen: dict[str, NativeSubagentReconcileFingerprint] = {}
    observed_display: dict[str, tuple[Any, ...]] = {}
    observed_pending: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    details: list[dict[str, str]] = []
    corrected = unchanged = unverified = unsupported = 0

    for child in children:
        if (
            child.parent_conversation_id != parent_session_id
            or child.labels.get(_WRAPPER_LABEL_KEY) != _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
        ):
            unverified += 1
            unsupported += 1
            details.append(_unverified_detail(child.id, "unsupported_child"))
            continue
        try:
            fingerprint = await asyncio.to_thread(
                conversation_store.get_native_subagent_reconcile_fingerprint,
                child.id,
                _FINGERPRINT_LABEL_KEYS,
            )
        except Exception:  # noqa: BLE001 - one child must not falsify the batch result.
            unverified += 1
            details.append(_unverified_detail(child.id, "server_state_read_failed"))
            continue
        if fingerprint is None or fingerprint.parent_conversation_id != parent_session_id:
            unverified += 1
            details.append(_unverified_detail(child.id, "server_state_changed"))
            continue
        if fingerprint.runner_id != parent.runner_id:
            unverified += 1
            details.append(_unverified_detail(child.id, "child_on_another_runner"))
            continue
        frozen[child.id] = fingerprint
        observed_display[child.id] = _display_fingerprint(child.id)
        observed_pending[child.id] = _pending_fingerprint(parent_session_id, child.id)

    if not frozen:
        return {
            "corrected": corrected,
            "unchanged": unchanged,
            "unverified": unverified,
            "unsupported": unsupported,
            "details": details,
        }

    probe = await _read_native_subagent_probe(runner_client, parent_session_id)
    current_parent = await asyncio.to_thread(
        conversation_store.get_conversation, parent_session_id
    )
    if (
        current_parent is None
        or _runtime_identity(current_parent) != _runtime_identity(parent)
        or parent.external_session_id != probe.claude_session_id
        or probe.bridge_id != (parent.labels.get(_BRIDGE_ID_LABEL_KEY) or parent_session_id)
    ):
        raise OmnigentError(
            "The native session binding changed while its status was being checked; "
            "no state was changed.",
            code=ErrorCode.CONFLICT,
        )
    parent_fingerprint = await asyncio.to_thread(
        conversation_store.get_native_subagent_reconcile_fingerprint,
        parent_session_id,
        _PARENT_RUNTIME_LABEL_KEYS,
    )
    if parent_fingerprint is None or not _runtime_fingerprint_matches(
        current_parent, parent_fingerprint
    ):
        raise OmnigentError(
            "The native session binding changed while its status was being checked; "
            "no state was changed.",
            code=ErrorCode.CONFLICT,
        )
    by_server_id: dict[str, _ProbeChild | None] = {}
    for row in probe.children:
        by_server_id[row.server_session_id] = (
            None if row.server_session_id in by_server_id else row
        )

    for session_id, fingerprint in frozen.items():
        labels = _fingerprint_labels(fingerprint)
        row = by_server_id.get(session_id)
        if row is None:
            unverified += 1
            reason = (
                "duplicate_host_result" if session_id in by_server_id else "not_reported_by_host"
            )
            details.append(_unverified_detail(session_id, reason))
            continue
        expected_subagent_id = labels.get(_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY)
        expected_tool_use_id = labels.get(_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY)
        identity_matches = (
            bool(expected_subagent_id)
            and bool(expected_tool_use_id)
            and row.subagent_id == expected_subagent_id
            and row.tool_use_id == expected_tool_use_id
            and row.evidence.subagent_id == expected_subagent_id
            and row.evidence.meta_tool_use_id == expected_tool_use_id
            and row.evidence.claude_session_id == probe.claude_session_id
            and row.evidence.parent_complete_byte_offset == probe.parent_complete_byte_offset
            and (
                row.status != "terminal"
                or row.evidence.evidence_key
                == _expected_evidence_key(parent_session_id, probe, row)
            )
        )
        if not identity_matches:
            unverified += 1
            details.append(_unverified_detail(session_id, "server_host_identity_mismatch"))
            continue
        if (
            row.status != "terminal"
            or row.terminal_status is None
            or row.terminal_status not in _TERMINAL_STATUSES
        ):
            unverified += 1
            details.append(_unverified_detail(session_id, row.reason))
            continue
        terminal_status = row.terminal_status
        desired_live, label_updates = _desired_terminal_state(terminal_status)
        durable_matches = _already_matches_terminal(fingerprint, terminal_status)
        repair_updates = (
            {}
            if durable_matches
            else {
                **label_updates,
                _SUBAGENT_STATUS_GENERATION_LABEL_KEY: secrets.token_hex(16),
            }
        )
        display_matches = _display_state_matches_terminal(
            observed_display[session_id], desired_live
        )
        pending_changed = (
            _pending_fingerprint(parent_session_id, session_id) != observed_pending[session_id]
        )
        if _display_fingerprint(session_id) != observed_display[session_id] or pending_changed:
            unverified += 1
            details.append(
                _unverified_detail(session_id, "server_display_state_changed_during_recheck")
            )
            continue

        try:
            write_result = await asyncio.to_thread(
                conversation_store.reconcile_native_subagent_status,
                fingerprint,
                expected_parent=parent_fingerprint,
                live_status=desired_live,
                label_updates=repair_updates,
            )
        except Exception:  # noqa: BLE001 - surface partial failure in counts.
            write_result = "stale"
        if write_result == "corrected":
            current_display = _display_fingerprint(session_id)
            pending_changed = (
                _pending_fingerprint(parent_session_id, session_id) != observed_pending[session_id]
            )
            if current_display != observed_display[session_id] or pending_changed:
                current_status = current_display[0]
                await _restore_repair_after_new_activity(
                    conversation_store=conversation_store,
                    original=fingerprint,
                    repair_live_status=desired_live,
                    repair_label_updates=repair_updates,
                    current_display_status=current_status,
                )
                session_live_state.persist_live_status(session_id, desired_live)
                if current_status in {"idle", "running", "waiting", "failed"}:
                    session_live_state.persist_live_status(session_id, current_status)
                unverified += 1
                details.append(
                    _unverified_detail(session_id, "server_display_state_changed_during_recheck")
                )
                continue
            session_live_state.persist_live_status(session_id, desired_live)
            if not (durable_matches and display_matches):
                _session_status_cache[session_id] = desired_live
                _session_active_response_cache.pop(session_id, None)
                _session_background_task_count_cache.pop(session_id, None)
                _session_background_tasks_cache.pop(session_id, None)
                event = SessionStatusEvent(
                    type="session.status",
                    conversation_id=session_id,
                    status=desired_live,  # type: ignore[arg-type]
                    background_task_count=0,
                )
                payload = event.model_dump()
                payload.pop("response_id", None)
                payload.pop("background_tasks", None)
                payload.pop("blocked_on", None)
                session_stream.publish(session_id, payload)
                _publish_child_status_to_parent(session_id, desired_live)
            _silently_clear_pending(observed_pending[session_id])
            outcome = "unchanged" if durable_matches and display_matches else "corrected"
            if outcome == "corrected":
                corrected += 1
                reason = "structured_parent_terminal_evidence"
            else:
                unchanged += 1
                reason = "already_terminal"
            details.append({"session_id": session_id, "outcome": outcome, "reason": reason})
        else:
            unverified += 1
            if write_result == "unsupported":
                unsupported += 1
                reason = "atomic_reconciliation_unsupported"
            else:
                reason = "server_state_changed_during_recheck"
            details.append(_unverified_detail(session_id, reason))

    return {
        "corrected": corrected,
        "unchanged": unchanged,
        "unverified": unverified,
        "unsupported": unsupported,
        "details": details,
    }


async def _invalidate_native_subagents_for_missing_parent_terminal_impl(
    *,
    parent_session_id: str,
    parent: Any,
    conversation_store: ConversationStore,
    observed_runner_id: str | None,
) -> int:
    """Quarantine same-runner child activity when the parent TUI is absent."""
    if (
        parent.parent_conversation_id is not None
        or parent.labels.get(_WRAPPER_LABEL_KEY) != "claude-code-native-ui"
        or parent.runner_id != observed_runner_id
    ):
        return 0

    try:
        current_parent = await asyncio.to_thread(
            conversation_store.get_conversation, parent_session_id
        )
    except Exception:  # noqa: BLE001 - best-effort self-heal must not break its caller.
        _logger.warning(
            "Missing-terminal child reconciliation could not read parent %s",
            parent_session_id,
            exc_info=True,
            extra={"session_id": parent_session_id},
        )
        return 0
    if current_parent is None or _runtime_identity(current_parent) != _runtime_identity(parent):
        return 0
    try:
        parent_fingerprint = await asyncio.to_thread(
            conversation_store.get_native_subagent_reconcile_fingerprint,
            parent_session_id,
            _PARENT_RUNTIME_LABEL_KEYS,
        )
    except Exception:  # noqa: BLE001 - best-effort self-heal must not break its caller.
        return 0
    if parent_fingerprint is None or not _runtime_fingerprint_matches(
        current_parent, parent_fingerprint
    ):
        return 0

    try:
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=1000,
            kind="sub_agent",
            parent_conversation_id=parent_session_id,
            order="desc",
            sort_by="created_at",
            include_archived=False,
        )
    except Exception:  # noqa: BLE001 - best-effort self-heal must not break its caller.
        _logger.warning(
            "Missing-terminal child reconciliation could not list children for %s",
            parent_session_id,
            exc_info=True,
            extra={"session_id": parent_session_id},
        )
        return 0
    if page.has_more:
        return 0

    invalidated = 0
    for child in page.data:
        if (
            child.labels.get(_WRAPPER_LABEL_KEY) != _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
            or child.runner_id != parent.runner_id
            or child.labels.get(_SUBAGENT_TERMINAL_STATUS_LABEL_KEY) in _TERMINAL_STATUSES
            or child.labels.get(_SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY) == "true"
        ):
            continue
        try:
            fingerprint = await asyncio.to_thread(
                conversation_store.get_native_subagent_reconcile_fingerprint,
                child.id,
                _FINGERPRINT_LABEL_KEYS,
            )
        except Exception:  # noqa: BLE001 - one child must not abort the response.
            _logger.warning(
                "Missing-terminal reconciliation could not freeze child %s",
                child.id,
                exc_info=True,
                extra={"session_id": child.id},
            )
            continue
        if (
            fingerprint is None
            or fingerprint.parent_conversation_id != parent_session_id
            or fingerprint.runner_id != parent_fingerprint.runner_id
        ):
            continue
        labels = _fingerprint_labels(fingerprint)
        observed_status = _session_status_cache.get(child.id) or fingerprint.live_status
        # A transient idle display edge is not a terminal verdict; durable
        # running/waiting still needs quarantine after the parent TUI vanished.
        if (
            labels.get(_SUBAGENT_TERMINAL_STATUS_LABEL_KEY) in _TERMINAL_STATUSES
            or labels.get(_SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY) == "true"
            or observed_status not in {"idle", "running", "waiting", "activity_unverified"}
            or fingerprint.live_status not in {"running", "waiting"}
        ):
            continue
        observed_display = _display_fingerprint(child.id)
        observed_pending = _pending_fingerprint(parent_session_id, child.id)
        pending_changed = _pending_fingerprint(parent_session_id, child.id) != observed_pending
        if _display_fingerprint(child.id) != observed_display or pending_changed:
            continue

        label_updates = {
            _SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY: "true",
            _SUBAGENT_STATUS_GENERATION_LABEL_KEY: secrets.token_hex(16),
        }
        try:
            write_result = await asyncio.to_thread(
                conversation_store.reconcile_native_subagent_status,
                fingerprint,
                expected_parent=parent_fingerprint,
                live_status=fingerprint.live_status,
                label_updates=label_updates,
            )
        except Exception:  # noqa: BLE001 - one child must not abort the response.
            _logger.warning(
                "Missing-terminal reconciliation could not update child %s",
                child.id,
                exc_info=True,
                extra={"session_id": child.id},
            )
            continue
        if write_result != "corrected":
            continue

        try:
            settled = await asyncio.to_thread(
                conversation_store.get_native_subagent_reconcile_fingerprint,
                child.id,
                _FINGERPRINT_LABEL_KEYS,
            )
        except Exception:  # noqa: BLE001 - a stale fan-out is worse than no fan-out.
            settled = None
        settled_labels = _fingerprint_labels(settled) if settled is not None else {}
        if (
            settled is None
            or settled_labels.get(_SUBAGENT_TERMINAL_STATUS_LABEL_KEY) in _TERMINAL_STATUSES
            or settled_labels.get(_SUBAGENT_ACTIVITY_UNVERIFIED_LABEL_KEY) != "true"
        ):
            continue

        pending_changed = _pending_fingerprint(parent_session_id, child.id) != observed_pending
        if _display_fingerprint(child.id) != observed_display or pending_changed:
            await _restore_repair_after_new_activity(
                conversation_store=conversation_store,
                original=fingerprint,
                repair_live_status=fingerprint.live_status,
                repair_label_updates=label_updates,
                current_display_status=_display_fingerprint(child.id)[0],
            )
            continue

        _session_active_response_cache.pop(child.id, None)
        _session_background_task_count_cache.pop(child.id, None)
        _session_background_tasks_cache.pop(child.id, None)
        _silently_clear_pending(observed_pending)
        _publish_child_status_to_parent(child.id, None)
        invalidated += 1

    return invalidated


async def invalidate_native_subagents_for_missing_parent_terminal(**kwargs: Any) -> int:
    """Best-effort missing-terminal repair; never fail its GET/SSE caller."""
    try:
        if kwargs.get("parent") is None:
            store = kwargs["conversation_store"]
            kwargs["parent"] = await asyncio.to_thread(
                store.get_conversation, kwargs["parent_session_id"]
            )
            if kwargs["parent"] is None:
                return 0
        return await _invalidate_native_subagents_for_missing_parent_terminal_impl(**kwargs)
    except Exception:  # noqa: BLE001 - this is a non-critical self-heal boundary.
        parent_id = kwargs.get("parent_session_id")
        _logger.warning(
            "Missing-terminal child reconciliation failed for %s",
            parent_id,
            exc_info=True,
            extra={"session_id": parent_id},
        )
        return 0


__all__ = [
    "invalidate_native_subagents_for_missing_parent_terminal",
    "reconcile_native_subagents",
]
