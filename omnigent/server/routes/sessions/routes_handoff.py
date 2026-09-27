"""Durable project hand-offs between top-level sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.db.db_models import uuid_to_bytes
from omnigent.db.utils import now_epoch
from omnigent.entities import SessionHandoff
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native.native_coding_agents import public_agent_name
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.server.project_placement import (
    bindings_apply,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
    load_entries,
    root_on_host,
    same_canonical_path,
)
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    read_workspace_from_host,
)
from omnigent.server.routes._host_worktree import (
    WorktreeHostUnavailableError,
    WorktreeProxyError,
    list_worktrees_on_host,
)
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.routes._sessions.orchestration import _create_session_from_existing_agent
from omnigent.server.routes.sessions.routes_peer import (
    _PEER_INBOUND_LABEL,
    _PEER_INBOUND_REFUSE,
    PEER_QUEUE_LIFETIME,
    PeerRoutes,
    _runner_authorized_for_sender,
    effective_owner_id,
)
from omnigent.server.schemas import ProjectSessionCreateRequest, SessionGitOptions
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

_logger = logging.getLogger(__name__)
_OWNER_LOCKS: dict[str, asyncio.Lock] = {}
_UNFINISHED = ("creating", "open", "delivered", "cancel_requested")
_REPORTABLE = (*_UNFINISHED, "expired")
_HANDOFF_LEASE_S = 300
_BRIEF_WORKSPACE_ALLOWANCE = 1024


class HandoffStartRequest(BaseModel):
    project: str = Field(min_length=1)
    task: str = Field(min_length=1, max_length=8000)
    constraints: str | None = None
    expected_outcome: str | None = None
    artifacts: list[str] = Field(default_factory=list, max_length=20)
    branch: str | None = None
    existing_branch: bool = False
    base_branch: str | None = None
    agent: str | None = None
    session: str | None = None
    host: str | None = None
    lifetime_minutes: int = Field(default=1440, ge=5, le=4320)
    allow_onward: bool = False


class HandoffReportRequest(BaseModel):
    status: Literal["completed", "incomplete", "failed"]
    summary: str = Field(max_length=4000)
    done: list[str] = Field(default_factory=list, max_length=50)
    not_done: list[str] = Field(default_factory=list, max_length=50)
    artifacts: list[str] = Field(default_factory=list, max_length=50)


def derived_handoff_session_id(handoff_id: str) -> str:
    return hashlib.sha256(f"handoff:{handoff_id}".encode()).hexdigest()[:32]


def _store_id(value: str) -> bool:
    try:
        uuid_to_bytes(value)
    except ValueError:
        return False
    return True


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def format_handoff_brief(
    record: SessionHandoff,
    project_name: str,
    workspace: str,
    branch: str | None,
    dirty_paths: int | None,
    *,
    worktree: str | None = None,
) -> str:
    plan = record.git_plan or {}
    source = plan.get("source", {})
    branch_part = f" · branch {branch}" if branch else ""
    dirty_part = (
        f" · {dirty_paths} uncommitted paths present — leave them untouched unless the task names them"
        if dirty_paths
        else ""
    )
    location = (
        f"Worktree: {worktree}. Make every change and commit there."
        if worktree
        else f"Workspace: {workspace}"
    )
    lines = [
        f'[Hand-off {record.id} · project "{project_name}" · until {_utc(record.expires_at)}]',
        f'When done, call sys_handoff_report(handoff_id="{record.id}", status="completed"|"incomplete"|"failed", summary=…, done=[…], not_done=[…], artifacts=[…]) once. For progress or questions use sys_session_send to the sender with correlation_id="{record.id}". Onward hand-off: {"permitted within this deadline" if record.allow_onward else "not permitted"}.',
        f"{location}{branch_part}{dirty_part}",
        "",
        f"Task: {source.get('task', '')}",
    ]
    extras = []
    for key, label in (
        ("constraints", "Constraints"),
        ("artifacts", "Artifacts"),
        ("expected_outcome", "Expected outcome"),
    ):
        value = source.get(key)
        if value:
            extras.append(f"[{label}: {', '.join(value) if isinstance(value, list) else value}]")
    if extras:
        lines.append(" ".join(extras))
    return "\n".join(lines)


def format_handoff_result(record: SessionHandoff, project_name: str) -> str:
    outcome = record.outcome or {}

    def items(key: str) -> str:
        return ", ".join(outcome.get(key) or []) or "—"

    return (
        f'[Hand-off result {record.id} · project "{project_name}" · {record.state}]\n'
        f"Summary: {outcome.get('summary') or '—'} / Done: {items('done')} / "
        f"Not done: {items('not_done')} / Artifacts: {items('artifacts')}"
    )


def format_handoff_stop(record: SessionHandoff, why: str) -> str:
    return (
        f"[Hand-off {record.id} · stop requested ({why})] Stop the work, then call "
        "sys_handoff_report with what is done and not done."
    )


def _problem(
    disposition: str, reason: str, candidates: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "handoff_id": None,
        "state": None,
        "reason": reason,
        "disposition": disposition,
        "session": None,
        "brief_delivery": None,
        "result_state": None,
        "stop_state": None,
        "disclosure": None,
        "expires_at": None,
    }
    if candidates is not None:
        result["candidates"] = candidates[:20]
    return result


def _lock_for(owner: str) -> asyncio.Lock:
    lock = _OWNER_LOCKS.get(owner)
    if lock is None:
        lock = asyncio.Lock()
        _OWNER_LOCKS[owner] = lock
    return lock


def register_handoff_routes(
    router: APIRouter,
    *,
    peer: PeerRoutes,
    handoff_store: Any,
    project_store: Any,
    conversation_store: Any,
    agent_store: Any,
    runner_router: Any,
    permission_store: Any,
    auth_provider: Any,
    runner_tunnel_tokens: frozenset[str] | None,
    feature_flags: Any,
    host_registry: Any,
    agent_cache: Any,
    file_store: Any,
    artifact_store: Any,
    background_title_coordinator: Any,
    app_state: Any,
) -> None:
    flags = feature_flags if feature_flags is not None else resolve_feature_flags()
    enabled = flags.enabled(Feature.SESSION_PEER_MESSAGING) and handoff_store is not None
    if not enabled:

        async def unavailable() -> None:
            raise HTTPException(status_code=404, detail="Not found")

        for path, method in (
            ("/sessions/{sender_id}/handoffs", "POST"),
            ("/sessions/{sender_id}/handoffs", "GET"),
            ("/handoffs/{hid}", "GET"),
            ("/handoffs/{hid}/cancel", "POST"),
            ("/handoffs/{hid}/report", "POST"),
        ):
            router.add_api_route(path, unavailable, methods=[method], include_in_schema=False)
        return
    store = cast(Any, handoff_store)
    peer_store = getattr(app_state, "peer_message_store", None)
    host_store = getattr(app_state, "host_store", None)
    binding_store = getattr(app_state, "project_host_binding_store", None)
    sweeper = getattr(app_state, "peer_sweeper", None)

    async def get_record(hid: str) -> SessionHandoff:
        if not _store_id(hid):
            raise HTTPException(status_code=404, detail="Not found")
        record = await asyncio.to_thread(store.get, hid)
        if record is None:
            raise HTTPException(status_code=404, detail="Not found")
        return record

    async def view(record: SessionHandoff, disposition: str = "started") -> dict[str, Any]:
        receiver = await asyncio.to_thread(
            conversation_store.get_conversation, record.receiver_session_id
        )
        plan = record.git_plan or {}
        project = (
            await asyncio.to_thread(
                project_store.get,
                record.project_id,
                user_id=plan.get("project_owner", record.owner_user_id),
            )
            if project_store
            else None
        )
        delivery = (
            await asyncio.to_thread(peer_store.get, record.brief_peer_id) if peer_store else None
        )
        disclosure = record.disclosure or {}
        result = {
            "handoff_id": record.id,
            "state": record.state,
            "reason": record.reason,
            "disposition": disposition,
            "checkout": record.checkout,
            "session": {
                "id": record.receiver_session_id,
                "title": title_without_closed_marker(receiver.title)
                if receiver
                else plan.get("title"),
                "project": project.name if project else plan.get("project_name"),
                "host_id": record.host_id,
                "workspace": receiver.workspace
                if receiver
                else plan.get("workspace", record.root),
                "worktree": receiver.worktree if receiver else record.worktree,
                "git_branch": record.git_branch,
                "agent": plan.get("agent_name"),
                "created": record.create_session,
            },
            "brief_delivery": delivery.state if delivery else None,
            "result_state": record.result_state,
            "stop_state": record.stop_state,
            "disclosure": {
                key: disclosure.get(key)
                for key in ("reused", "branch", "base_branch", "branch_generated", "dirty_paths")
            },
            "expires_at": record.expires_at,
        }
        if record.outcome is not None:
            result["outcome"] = record.outcome
        return result

    async def notice(record: SessionHandoff) -> None:
        if sweeper is None:
            return
        receiver = await asyncio.to_thread(
            conversation_store.get_conversation, record.receiver_session_id
        )
        title = (
            title_without_closed_marker(receiver.title)
            if receiver
            else (record.git_plan or {}).get("title")
        )
        title = (title or record.receiver_session_id).replace('"', "'")
        suffix = f" ({record.reason})" if record.reason else ""
        line = f'[System: hand-off {record.id} to session {record.receiver_session_id} "{title}" {record.state}{suffix}]'
        try:
            await sweeper.notify_line(record.sender_session_id, line)
        except Exception:
            _logger.warning(
                "Hand-off back-notice failed", exc_info=True, extra={"handoff_id": record.id}
            )

    async def transition(
        record: SessionHandoff, state: str, reason: str | None, **fields: Any
    ) -> bool:
        return await asyncio.to_thread(
            store.transition, record.id, state, reason, (record.state,), **fields
        )

    async def send_state(
        record: SessionHandoff,
        *,
        kind: str,
        sender_id: str,
        receiver_id: str,
        text: str,
        peer_id: str,
        request: Request | None,
    ) -> tuple[str, str | None]:
        sender = await asyncio.to_thread(conversation_store.get_conversation, sender_id)
        if sender is None:
            return "failed", "closed"
        if request is None:
            from omnigent.server.peer_sweeper import PeerSweeper

            app = getattr(sweeper, "_app", None) or SimpleNamespace(state=app_state)
            request = PeerSweeper._synthetic_request(receiver_id, app)
        options: dict[str, Any] = {
            "deferred_until": record.expires_at
            if kind == "brief"
            else now_epoch() + PEER_QUEUE_LIFETIME,
            "require_init_success": kind == "brief",
        }
        if kind == "brief":
            options["acting_user_id"] = record.owner_user_id
        response = await peer.send(
            sender=sender,
            receiver_id=receiver_id,
            text=text,
            correlation_id=record.id,
            peer_id=peer_id,
            system=True,
            request=request,
            **options,
        )
        state = (
            response.get("state")
            if response.get("disposition") == "existing"
            else response.get("disposition")
        )
        if state == "uncertain":
            state = "delivering"
        return str(state or "failed"), response.get("reason")

    async def advance(
        hid: str,
        *,
        holds_lease: bool = False,
        request: Request | None = None,
        back_notice: bool = False,
    ) -> SessionHandoff:
        if not holds_lease and not await asyncio.to_thread(
            store.claim, hid, now_epoch(), _HANDOFF_LEASE_S
        ):
            return await get_record(hid)
        try:
            for _ in range(30):
                record = await get_record(hid)
                now = now_epoch()
                if record.state in _UNFINISHED and now >= record.expires_at:
                    brief = (
                        await asyncio.to_thread(peer_store.get, record.brief_peer_id)
                        if peer_store
                        else None
                    )
                    if record.state in ("creating", "open") and (
                        brief is None or brief.state == "expired"
                    ):
                        won = await transition(record, "expired", "not_delivered")
                    elif (
                        record.state in ("creating", "open")
                        and brief is not None
                        and brief.state in ("pending", "queued", "held")
                    ):
                        if peer_store is None or not await asyncio.to_thread(
                            peer_store.transition,
                            brief.id,
                            "expired",
                            "handoff_expired",
                            (brief.state,),
                        ):
                            continue
                        won = await transition(record, "expired", "not_delivered")
                    elif (
                        record.state == "open"
                        and brief is not None
                        and brief.state in ("failed", "refused", "refused_by_user")
                    ):
                        won = await transition(
                            record, "failed", (brief.reason or brief.state)[:128]
                        )
                    else:
                        won = await transition(
                            record,
                            "expired",
                            "no_report",
                            stop_state="pending",
                            stop_peer_id=record.stop_peer_id or uuid.uuid4().hex,
                        )
                    if won and back_notice:
                        await notice(await get_record(hid))
                    continue
                if record.state == "delivered":
                    receiver = await asyncio.to_thread(
                        conversation_store.get_conversation, record.receiver_session_id
                    )
                    owner = (
                        await asyncio.to_thread(
                            effective_owner_id, receiver, conversation_store, permission_store
                        )
                        if receiver and permission_store
                        else record.owner_user_id
                    )
                    if receiver and (
                        (receiver.labels or {}).get(_PEER_INBOUND_LABEL) == _PEER_INBOUND_REFUSE
                        or owner != record.owner_user_id
                    ):
                        if await transition(
                            record,
                            "cancel_requested",
                            "revoked",
                            cancel_requested_at=now,
                            stop_state="pending",
                            stop_peer_id=record.stop_peer_id or uuid.uuid4().hex,
                        ):
                            if back_notice:
                                await notice(await get_record(hid))
                        continue
                if record.state == "creating":
                    receiver = await asyncio.to_thread(
                        conversation_store.get_conversation, record.receiver_session_id
                    )
                    created = False
                    if receiver is None:
                        plan = record.git_plan or {}
                        body = ProjectSessionCreateRequest(
                            project_id=record.project_id,
                            host_id=record.host_id,
                            workspace=plan.get("workspace", record.root),
                            agent_id=plan.get("agent_id"),
                            git=SessionGitOptions(**plan["git"]) if plan.get("git") else None,
                            title=plan.get("title"),
                        )
                        try:
                            from omnigent.server.peer_sweeper import PeerSweeper

                            app = getattr(sweeper, "_app", None) or SimpleNamespace(
                                state=app_state
                            )
                            req = request or PeerSweeper._synthetic_request(
                                record.receiver_session_id, app
                            )
                            await _create_session_from_existing_agent(
                                conversation_store,
                                agent_store,
                                runner_router,
                                body,
                                req,
                                agent_cache=agent_cache,
                                user_id=plan.get("project_owner", record.owner_user_id),
                                permission_store=permission_store,
                                liveness_lookup=None,
                                file_store=file_store,
                                artifact_store=artifact_store,
                                background_title_coordinator=background_title_coordinator,
                                project_store=project_store,
                                conversation_id=record.receiver_session_id,
                            )
                            created = True
                        except Exception as exc:
                            receiver = await asyncio.to_thread(
                                conversation_store.get_conversation, record.receiver_session_id
                            )
                            if receiver is None:
                                detail = str(exc)
                                if plan.get("git") and "already exists" in detail.lower():
                                    detail = f"branch_exists: {detail}"
                                if (
                                    await transition(
                                        record, "failed", f"create_failed: {detail}"[:128]
                                    )
                                    and back_notice
                                ):
                                    await notice(await get_record(hid))
                                continue
                    # Idempotent: an adopted row from a crashed create may lack the grant.
                    if permission_store is not None:
                        await asyncio.to_thread(permission_store.ensure_user, record.owner_user_id)
                        await asyncio.to_thread(
                            permission_store.grant,
                            record.owner_user_id,
                            record.receiver_session_id,
                            LEVEL_OWNER,
                        )
                    if created:
                        _announce_session_added(record.owner_user_id, record.receiver_session_id)
                    await asyncio.to_thread(
                        conversation_store.set_labels,
                        record.receiver_session_id,
                        {
                            "omnigent.handoff.id": record.id,
                            "omnigent.handoff.from": record.sender_session_id,
                        },
                    )
                    receiver = await asyncio.to_thread(
                        conversation_store.get_conversation, record.receiver_session_id
                    )
                    plan = record.git_plan or {}
                    brief = format_handoff_brief(
                        record,
                        plan.get("project_name", record.project_id),
                        receiver.workspace,
                        receiver.git_branch,
                        (record.disclosure or {}).get("dirty_paths"),
                        worktree=receiver.worktree,
                    )
                    if (
                        await transition(
                            record, "open", None, brief=brief, worktree=receiver.worktree
                        )
                        and back_notice
                    ):
                        await notice(await get_record(hid))
                    continue
                if record.state == "open":
                    try:
                        state, reason = await send_state(
                            record,
                            kind="brief",
                            sender_id=record.sender_session_id,
                            receiver_id=record.receiver_session_id,
                            text=record.brief,
                            peer_id=record.brief_peer_id,
                            request=request,
                        )
                    except Exception as exc:
                        brief = (
                            await asyncio.to_thread(peer_store.get, record.brief_peer_id)
                            if peer_store
                            else None
                        )
                        state = brief.state if brief else "failed"
                        reason = brief.reason if brief else str(exc)
                    else:
                        brief = (
                            await asyncio.to_thread(peer_store.get, record.brief_peer_id)
                            if peer_store
                            else None
                        )
                        reason = brief.reason if brief else reason
                    if state == "delivered":
                        if await transition(record, "delivered", None) and back_notice:
                            await notice(await get_record(hid))
                        continue
                    if state in ("pending", "queued", "held", "delivering"):
                        break
                    target = "expired" if state == "expired" else "failed"
                    reason = "not_delivered" if state == "expired" else (reason or state)
                    if await transition(record, target, reason) and back_notice:
                        await notice(await get_record(hid))
                    continue
                if record.result_state == "pending" and record.result_peer_id:
                    project_name = (record.git_plan or {}).get("project_name", record.project_id)
                    try:
                        state, _ = await send_state(
                            record,
                            kind="result",
                            sender_id=record.receiver_session_id,
                            receiver_id=record.sender_session_id,
                            text=format_handoff_result(record, project_name),
                            peer_id=record.result_peer_id,
                            request=request,
                        )
                    except Exception:
                        _logger.warning(
                            "Hand-off result send failed", exc_info=True, extra={"handoff_id": hid}
                        )
                        break
                    await asyncio.to_thread(
                        store.set_fields,
                        record.id,
                        (record.state,),
                        result_state="failed"
                        if state in ("failed", "rejected", "refused", "refused_by_user", "expired")
                        else "sent",
                    )
                    continue
                if record.stop_state == "pending" and record.stop_peer_id:
                    why = (
                        "revoked"
                        if record.reason == "revoked"
                        else "expired"
                        if record.state == "expired"
                        else "cancelled"
                    )
                    try:
                        state, _ = await send_state(
                            record,
                            kind="stop",
                            sender_id=record.sender_session_id,
                            receiver_id=record.receiver_session_id,
                            text=format_handoff_stop(record, why),
                            peer_id=record.stop_peer_id,
                            request=request,
                        )
                    except Exception:
                        _logger.warning(
                            "Hand-off stop send failed", exc_info=True, extra={"handoff_id": hid}
                        )
                        break
                    await asyncio.to_thread(
                        store.set_fields,
                        record.id,
                        (record.state,),
                        stop_state="failed"
                        if state in ("failed", "rejected", "refused", "refused_by_user", "expired")
                        else "sent",
                    )
                    continue
                break
            return await get_record(hid)
        finally:
            if not holds_lease:
                await asyncio.to_thread(store.release, hid)

    async def _pass(now: int) -> None:
        records = await asyncio.to_thread(store.list_needing_work, now, 50)
        for record in records:
            try:
                expired_lease = (
                    record.state == "creating"
                    and record.lease_until is not None
                    and record.lease_until < now
                )
                if not await asyncio.to_thread(store.claim, record.id, now, _HANDOFF_LEASE_S):
                    continue
                try:
                    git_options = (record.git_plan or {}).get("git") or {}
                    if (
                        expired_lease
                        and git_options
                        and not git_options.get("existing_worktree")
                        and await asyncio.to_thread(
                            conversation_store.get_conversation, record.receiver_session_id
                        )
                        is None
                    ):
                        location = f"{record.git_branch or ''} {(record.git_plan or {}).get('workspace') or record.root or ''}".strip()
                        if await transition(
                            record, "failed", f"create_interrupted: {location}"[:128]
                        ):
                            await notice(await get_record(record.id))
                    else:
                        await advance(record.id, holds_lease=True, back_notice=True)
                finally:
                    await asyncio.to_thread(store.release, record.id)
            except Exception:
                _logger.exception("Hand-off sweep failed", extra={"handoff_id": record.id})

    if sweeper is not None:
        sweeper.set_handoff_pass(_pass)
        sweeper.set_handoff_brief_check(
            lambda message: (
                _store_id(message.ref)
                and (handoff := store.get(message.ref)) is not None
                and handoff.brief_peer_id == message.id
            )
        )

    async def runner_auth(request: Request, session_id: str) -> Any:
        if not _store_id(session_id):
            raise HTTPException(status_code=404, detail="Not found")
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not _runner_authorized_for_sender(request, conv, runner_tunnel_tokens):
            raise HTTPException(status_code=403, detail="Runner token does not match session")
        return conv

    async def user_auth(request: Request, session_id: str, level: int) -> bool:
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, level, permission_store, conversation_store
        )
        return True

    async def sender_or_user(request: Request, sender_id: str, level: int) -> None:
        if not _store_id(sender_id):
            raise HTTPException(status_code=404, detail="Not found")
        sender = await asyncio.to_thread(conversation_store.get_conversation, sender_id)
        if sender is None:
            raise HTTPException(status_code=404, detail="Not found")
        if _runner_authorized_for_sender(request, sender, runner_tunnel_tokens):
            return
        await user_auth(request, sender_id, level)

    async def list_sessions(**filters: Any) -> list[Any]:
        rows: list[Any] = []
        after = None
        while True:
            page = await asyncio.to_thread(
                conversation_store.list_conversations, limit=200, after=after, **filters
            )
            rows.extend(page.data)
            if not page.has_more or page.last_id is None:
                return rows
            after = page.last_id

    @router.post("/sessions/{sender_id}/handoffs", include_in_schema=False, response_model=None)
    async def start_handoff(
        request: Request, sender_id: str, body: HandoffStartRequest
    ) -> dict[str, Any]:
        sender = await runner_auth(request, sender_id)
        if sender.parent_conversation_id is not None:
            return _problem("refused", "is_subagent")
        for item in body.artifacts:
            if len(item) > 500:
                raise OmnigentError(
                    "artifact exceeds 500 characters", code=ErrorCode.INVALID_INPUT
                )
        request_user_id = _get_user_id(request, auth_provider)
        owner = (
            await asyncio.to_thread(
                effective_owner_id, sender, conversation_store, permission_store
            )
            if permission_store
            else request_user_id or RESERVED_USER_LOCAL
        )
        if owner is None:
            return _problem("refused", "not_same_owner")
        project_owner = owner if permission_store else request_user_id
        source = body.model_dump()
        digest = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        now = now_epoch()
        async with _lock_for(owner):
            duplicate = await asyncio.to_thread(store.find_unfinished_duplicate, sender_id, digest)
            if duplicate:
                return await view(duplicate, "duplicate")
            if await asyncio.to_thread(store.count_unfinished, owner) >= 5:
                return _problem("refused", "cap")
            if await asyncio.to_thread(store.count_recent, sender_id, now - 600) >= 30:
                return _problem("refused", "burst")
            parent = await asyncio.to_thread(store.find_binding_for_receiver, sender_id)
            expires_at = now + body.lifetime_minutes * 60
            if parent:
                if not parent.allow_onward or parent.expires_at <= now:
                    return _problem("refused", "onward_not_permitted")
                expires_at = min(expires_at, parent.expires_at)
            project = (
                await asyncio.to_thread(project_store.get, body.project, user_id=project_owner)
                if project_store and _store_id(body.project)
                else None
            )
            if project is None:
                projects = (
                    await asyncio.to_thread(project_store.list, user_id=project_owner)
                    if project_store
                    else []
                )
                matches = [p for p in projects if p.name.casefold() == body.project.casefold()]
                if len(matches) != 1:
                    return _problem(
                        "needs_input",
                        "project_ambiguous" if matches else "project_not_found",
                        [{"id": p.id, "name": p.name} for p in (matches or projects)[:20]],
                    )
                project = matches[0]
            bindings = await load_bindings(binding_store, project.id)
            entries = await load_entries(binding_store, project.id)
            gates_on = bindings_apply(project, flags)
            roots = host_roots(project, bindings, gates_on=gates_on, entries=entries)
            eligible = await load_eligible_host_ids(host_store, owner, (r.host_id for r in roots))
            candidates = []
            for candidate_root in roots:
                if eligible is not None and candidate_root.host_id not in eligible:
                    continue
                host_row = (
                    await asyncio.to_thread(host_store.get_host, candidate_root.host_id)
                    if host_store
                    else None
                )
                candidates.append(
                    {
                        "id": candidate_root.host_id,
                        "name": host_row.name if host_row else candidate_root.host_id,
                    }
                )
            host_id = None
            if body.host:
                hosts = await asyncio.to_thread(host_store.list_hosts, owner) if host_store else []
                host = next((h for h in hosts if h.host_id == body.host), None)
                if host is None:
                    named = [h for h in hosts if h.name.casefold() == body.host.casefold()]
                    host = named[0] if len(named) == 1 else None
                if host is None:
                    return _problem("needs_input", "host_required", candidates)
                host_id = host.host_id
            else:
                host_id = default_host(project, roots, eligible_host_ids=eligible).host_id
                if host_id is None:
                    return _problem("needs_input", "host_required", candidates)
            root = root_on_host(project, bindings, host_id, gates_on=gates_on, entries=entries)
            if root is None:
                return _problem("needs_input", "no_root", candidates)
            repo = root.checkout if root.source == "entry" and root.checkout else root.workspace
            agent_key = body.agent or project.config.get("agent_id")
            if not isinstance(agent_key, str) or not agent_key:
                return _problem("needs_input", "agent_required")
            agent = (
                await asyncio.to_thread(agent_store.get, agent_key)
                if agent_store and _store_id(agent_key)
                else None
            )
            if agent is None and agent_store:
                agent = await asyncio.to_thread(agent_store.get_by_name, agent_key)
            if agent is None and agent_store:
                matches = []
                after = None
                while True:
                    agents = await asyncio.to_thread(agent_store.list, limit=200, after=after)
                    for item in agents.data:
                        if (public_agent_name(item.name) or "").casefold() != agent_key.casefold():
                            continue
                        try:
                            visible = await validate_session_agent(
                                user_id=project_owner,
                                agent_id=item.id,
                                agent_store=agent_store,
                                permission_store=permission_store,
                                conversation_store=conversation_store,
                            )
                        except OmnigentError:
                            continue
                        matches.append(visible)
                    if not agents.has_more or agents.last_id is None:
                        break
                    after = agents.last_id
                if len(matches) != 1:
                    return _problem(
                        "needs_input",
                        "agent_required",
                        [
                            {"id": item.id, "name": public_agent_name(item.name) or item.name}
                            for item in matches[:20]
                        ],
                    )
                agent = matches[0]
            if agent is None:
                return _problem("needs_input", "agent_required")
            try:
                agent = await validate_session_agent(
                    user_id=project_owner,
                    agent_id=agent.id,
                    agent_store=agent_store,
                    permission_store=permission_store,
                    conversation_store=conversation_store,
                )
            except OmnigentError:
                return _problem("needs_input", "agent_required")
            hid = uuid.uuid4().hex
            branch = body.branch
            generated = False
            root_workspace = root.workspace
            workspace = root_workspace
            git: dict[str, Any] | None = None
            worktrees: list[dict[str, Any]] | None = None
            host_conn = host_registry.get(host_id) if host_registry else None
            need_list = bool(branch) or bool(project.config.get("use_worktree"))
            if not branch and project.config.get("use_worktree") and host_conn is None:
                raise OmnigentError(
                    "host unavailable for worktree lookup", code=ErrorCode.CONFLICT
                )
            if need_list and host_conn:
                try:
                    worktrees = await list_worktrees_on_host(
                        host_registry=host_registry, host_conn=host_conn, repo_path=repo
                    )
                except WorktreeProxyError as exc:
                    if not body.branch and "not a git" in str(exc).lower():
                        worktrees = None
                    elif isinstance(exc, WorktreeHostUnavailableError):
                        raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
                    else:
                        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            matched_worktree: str | None = None
            if branch and body.existing_branch:
                match = next((w for w in (worktrees or []) if w.get("branch") == branch), None)
                if match:
                    matched_worktree = str(match["path"])
                    workspace = matched_worktree
                    git = {"branch_name": branch, "existing_worktree": True}
                else:
                    git = {"branch_name": branch, "existing_branch": True}
            elif branch or (
                not branch and project.config.get("use_worktree") and worktrees is not None
            ):
                if not branch:
                    branch = f"handoff-{hid[:8]}"
                    generated = True
                base = body.base_branch or project.config.get("base_branch")
                if not base:
                    return _problem("needs_input", "base_branch_required")
                if any(w.get("branch") == branch for w in (worktrees or [])):
                    return _problem("needs_input", "branch_exists")
                git = {"branch_name": branch, "base_branch": base}
            if branch:
                reservation = await asyncio.to_thread(
                    store.find_branch_reservation, host_id, repo, branch
                )
                if reservation:
                    return _problem(
                        "needs_input",
                        "branch_in_use",
                        [
                            {
                                "id": reservation.receiver_session_id,
                                "name": reservation.receiver_session_id,
                            }
                        ],
                    )
            conversations = await list_sessions(
                owned_by=owner if permission_store else None, project=project.name, host_id=host_id
            )
            live = [
                c
                for c in conversations
                if not c.archived
                and not is_session_closed(c.labels, c.title)
                and c.parent_conversation_id is None
                and SIDE_CHAT_LABEL_KEY not in (c.labels or {})
            ]
            branch_users = []
            if branch:
                all_on_host = await list_sessions(
                    owned_by=owner if permission_store else None, host_id=host_id
                )
                branch_users = [
                    c
                    for c in all_on_host
                    if c.git_branch == branch
                    and not c.archived
                    and not is_session_closed(c.labels, c.title)
                ]

            async def fits(conv: Any, *, explicit: bool = False) -> bool:
                if conv.id == sender_id or conv.host_id != host_id or conv.agent_id != agent.id:
                    return False
                if conv.project_id != project.id or await asyncio.to_thread(
                    store.find_binding_for_receiver, conv.id
                ):
                    return False
                if branch:
                    if (
                        not body.existing_branch
                        or matched_worktree is None
                        or conv.git_branch != branch
                        or not same_canonical_path(
                            conv.worktree or conv.workspace or "", matched_worktree
                        )
                    ):
                        return False
                elif (
                    conv.git_branch
                    or conv.worktree
                    or not same_canonical_path(conv.workspace or "", root_workspace)
                ):
                    return False
                if not explicit and (await peer.true_state(conv))[0] != "idle":
                    return False
                return True

            selected = None
            if body.session and body.session != "new":
                target = (
                    await asyncio.to_thread(conversation_store.get_conversation, body.session)
                    if _store_id(body.session)
                    else None
                )
                if target and SIDE_CHAT_LABEL_KEY in (target.labels or {}):
                    return _problem("refused", "side_chat")
                if target is None or target not in live or not await fits(target, explicit=True):
                    return _problem("refused", "session_not_in_project")
                selected = target
            elif not body.session or body.session != "new":
                if not branch or body.existing_branch:
                    choices = [c for c in live if await fits(c)]
                    if len(choices) == 1:
                        selected = choices[0]
            if branch_users and (selected is None or len(branch_users) != 1):
                return _problem(
                    "needs_input",
                    "branch_in_use",
                    [{"id": c.id, "name": c.title or c.id} for c in branch_users[:20]],
                )
            dirty: int | None = None
            if git is None and host_conn:
                try:
                    changes = await read_workspace_from_host(
                        host_registry=host_registry,
                        host_conn=host_conn,
                        op="changes",
                        workspace=root.workspace,
                        session_id=sender_id,
                        params={},
                    )
                    dirty = len(changes.get("data", []))
                except (HostFsError, HostFsUnavailableError):
                    dirty = None
            disclosure = {
                "reused": selected is not None,
                "branch": branch,
                "base_branch": git.get("base_branch") if git else None,
                "branch_generated": generated,
                "dirty_paths": dirty,
            }
            plan = {
                "agent_id": agent.id,
                "agent_name": public_agent_name(agent.name),
                "project_name": project.name,
                "project_owner": project_owner,
                "workspace": workspace,
                "title": f"Hand-off: {body.task[:60]}",
                "git": git,
                "source": source,
            }
            record = SessionHandoff(
                id=hid,
                owner_user_id=owner,
                sender_session_id=sender_id,
                receiver_session_id=selected.id if selected else derived_handoff_session_id(hid),
                create_session=selected is None,
                project_id=project.id,
                state="open" if selected else "creating",
                brief_hash=digest,
                brief="",
                allow_onward=body.allow_onward,
                brief_peer_id=uuid.uuid4().hex,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
                host_id=host_id,
                root=root.workspace,
                checkout=repo,
                worktree=selected.worktree if selected else None,
                git_branch=branch,
                git_plan=plan,
                parent_handoff_id=parent.id if parent else None,
                disclosure=disclosure,
                lease_until=now_epoch() + _HANDOFF_LEASE_S,
            )
            record.brief = format_handoff_brief(
                record,
                project.name,
                selected.workspace if selected else workspace,
                selected.git_branch if selected else branch,
                dirty,
                worktree=selected.worktree if selected else None,
            )
            brief_limit = 16000 - _BRIEF_WORKSPACE_ALLOWANCE
            if len(record.brief) > brief_limit:
                raise OmnigentError(
                    f"hand-off brief exceeds {brief_limit} characters",
                    code=ErrorCode.INVALID_INPUT,
                )
            await asyncio.to_thread(store.create, record)
        try:
            result = await advance(hid, holds_lease=True, request=request)
        finally:
            await asyncio.to_thread(store.release, hid)
        if (
            result.state == "failed"
            and result.reason
            and (result.git_plan or {}).get("git")
            and result.reason.startswith("create_failed: branch_exists:")
        ):
            return await view(result, "needs_input") | {"reason": "branch_exists"}
        return await view(result, "failed" if result.state == "failed" else "started")

    @router.get("/sessions/{sender_id}/handoffs", include_in_schema=False, response_model=None)
    async def list_handoffs(request: Request, sender_id: str) -> list[dict[str, Any]]:
        await sender_or_user(request, sender_id, LEVEL_READ)
        records = await asyncio.to_thread(
            store.list_for_sender, sender_id, now_epoch() - 86400, 20
        )
        return [await view(record) for record in records]

    @router.get("/handoffs/{hid}", include_in_schema=False, response_model=None)
    async def get_handoff(request: Request, hid: str) -> dict[str, Any]:
        record = await get_record(hid)
        sender = await asyncio.to_thread(
            conversation_store.get_conversation, record.sender_session_id
        )
        receiver = await asyncio.to_thread(
            conversation_store.get_conversation, record.receiver_session_id
        )
        if not (
            (sender and _runner_authorized_for_sender(request, sender, runner_tunnel_tokens))
            or (
                receiver and _runner_authorized_for_sender(request, receiver, runner_tunnel_tokens)
            )
        ):
            try:
                await user_auth(request, record.sender_session_id, LEVEL_READ)
            except (OmnigentError, HTTPException):
                await user_auth(request, record.receiver_session_id, LEVEL_READ)
        return await view(record)

    @router.post("/handoffs/{hid}/cancel", include_in_schema=False, response_model=None)
    async def cancel_handoff(request: Request, hid: str) -> dict[str, Any]:
        for _ in range(5):
            record = await get_record(hid)
            await sender_or_user(request, record.sender_session_id, LEVEL_EDIT)
            if record.state not in _UNFINISHED:
                return await view(record)
            if record.state == "creating":
                won = await transition(
                    record, "cancelled", "cancelled", cancel_requested_at=now_epoch()
                )
            else:
                brief = (
                    await asyncio.to_thread(peer_store.get, record.brief_peer_id)
                    if peer_store
                    else None
                )
                stopped = (
                    brief is not None
                    and peer_store is not None
                    and brief.state in ("pending", "queued", "held")
                    and await asyncio.to_thread(
                        peer_store.transition,
                        brief.id,
                        "expired",
                        "handoff_cancelled",
                        (brief.state,),
                    )
                )
                unsent = (
                    record.state == "open"
                    and brief is None
                    and await asyncio.to_thread(
                        store.claim, record.id, now_epoch(), _HANDOFF_LEASE_S
                    )
                )
                if unsent:
                    try:
                        still_unsent = (
                            await asyncio.to_thread(peer_store.get, record.brief_peer_id)
                            if peer_store
                            else None
                        ) is None
                        won = still_unsent and await transition(
                            record, "cancelled", "cancelled", cancel_requested_at=now_epoch()
                        )
                    finally:
                        await asyncio.to_thread(store.release, record.id)
                    if not won:
                        continue
                elif record.state == "open" and stopped:
                    won = await transition(
                        record, "cancelled", "cancelled", cancel_requested_at=now_epoch()
                    )
                else:
                    won = await transition(
                        record,
                        "cancel_requested",
                        "cancelled",
                        cancel_requested_at=now_epoch(),
                        stop_state="pending",
                        stop_peer_id=record.stop_peer_id or uuid.uuid4().hex,
                    )
            if won:
                result = await advance(hid, request=request)
                return await view(result)
        return await view(await get_record(hid))

    @router.post("/handoffs/{hid}/report", include_in_schema=False, response_model=None)
    async def report_handoff(
        request: Request, hid: str, body: HandoffReportRequest
    ) -> dict[str, Any] | JSONResponse:
        for value in [*body.done, *body.not_done, *body.artifacts]:
            if len(value) > 500:
                raise OmnigentError(
                    "report item exceeds 500 characters", code=ErrorCode.INVALID_INPUT
                )
        for _ in range(5):
            record = await get_record(hid)
            await runner_auth(request, record.receiver_session_id)
            if (
                record.reported_at is not None
                or record.state not in _REPORTABLE
                or (record.state == "expired" and record.reason != "no_report")
            ):
                return JSONResponse(status_code=409, content=await view(record))
            outcome = body.model_dump()
            target = "cancelled" if record.cancel_requested_at is not None else body.status
            project_name = (record.git_plan or {}).get("project_name", record.project_id)
            if (
                len(
                    format_handoff_result(
                        replace(record, state=target, outcome=outcome), project_name
                    )
                )
                > 16000
            ):
                raise OmnigentError(
                    "hand-off result exceeds 16000 characters", code=ErrorCode.INVALID_INPUT
                )
            if await transition(
                record,
                target,
                record.reason if target == "cancelled" else None,
                outcome=outcome,
                reported_at=now_epoch(),
                result_state="pending",
                result_peer_id=uuid.uuid4().hex,
            ):
                result = await advance(hid, request=request)
                await notice(result)
                return await view(result)
        return JSONResponse(status_code=409, content=await view(await get_record(hid)))


__all__ = [
    "HandoffReportRequest",
    "HandoffStartRequest",
    "derived_handoff_session_id",
    "format_handoff_brief",
    "format_handoff_result",
    "format_handoff_stop",
    "register_handoff_routes",
]
