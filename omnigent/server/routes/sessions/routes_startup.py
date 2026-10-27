"""Owner controls for a durable startup, separate from stopping a shared runner."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from omnigent.db.account_authority import AccountAuthority, account_generation
from omnigent.entities.session_startup import (
    SessionStartupStatus,
    StartupReleasePreview,
    StartupReleasePreviewItem,
    StartupReleaseReceipt,
    StartupReleaseRequest,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import (
    get_user_id,
    require_access_and_level,
    require_user,
)
from omnigent.server.schemas import CancelStartupRequest, SessionStartupEvent
from omnigent.server.startup_observations import accept_observation
from omnigent.startup_protocol import STARTUP_QUEUE_V1, StartupBoundaryClaim, StartupObservation
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_startup_store import SessionStartupStore


def register_startup_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> None:
    @router.get("/hosts/{host_id}/startup-releases", response_model=StartupReleasePreview)
    async def preview_startup_releases(request: Request, host_id: str) -> StartupReleasePreview:
        user_id = require_user(request, auth_provider)
        admission = getattr(request.app.state, "startup_admission", None)
        store = getattr(request.app.state, "startup_store", None)
        if admission is None or store is None or admission.policy.mode != "startup_only":
            raise HTTPException(409, "Startup-only queue is not enabled")
        generation, targets = await asyncio.to_thread(
            store.release_candidates,
            host_id,
            actor_user_id=user_id,
            actor_account_generation=account_generation(user_id) if user_id is not None else None,
        )
        from omnigent.server.resident_inventory import read_host_session_work_inventory_v2

        hosts = request.app.state.host_registry
        conn = hosts.get(host_id)
        inventory = (
            await read_host_session_work_inventory_v2(
                hosts,
                conn,
                conversation_store,
                getattr(request.app.state, "runner_router", None),
                getattr(request.app.state, "runner_session_initializer", None),
            )
            if conn is not None and targets
            else None
        )
        working = set()
        if inventory is not None:
            for session in inventory.sessions:
                if session.runner is not None and any(
                    row.live_turn
                    or row.live_tool
                    or row.background_job
                    or row.protected_interaction
                    for row in session.runner.inventory.observations
                ):
                    working.add(session.session_id)
        # Re-read ownership after the optional, bounded observation; POST still checks exact targets.
        current_generation, current_targets = await asyncio.to_thread(
            store.release_candidates,
            host_id,
            actor_user_id=user_id,
            actor_account_generation=account_generation(user_id) if user_id is not None else None,
        )
        items = []
        for target in targets if generation == current_generation else []:
            if target not in current_targets:
                continue
            conversation = await asyncio.to_thread(
                conversation_store.get_conversation, target.session_id
            )
            if conversation is None or conversation.archived:
                continue
            items.append(
                StartupReleasePreviewItem(
                    target=target,
                    title=conversation.title,
                    work_hint="working" if target.session_id in working else "unknown",
                )
            )
        return StartupReleasePreview(current_host_generation=current_generation, targets=items)

    @router.post("/hosts/{host_id}/startup-releases", response_model=StartupReleaseReceipt)
    async def release_startups(
        request: Request, host_id: str, body: StartupReleaseRequest
    ) -> StartupReleaseReceipt:
        user_id = get_user_id(request, auth_provider)
        for target in body.targets:
            await require_access_and_level(
                user_id, target.session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            conversation = await asyncio.to_thread(
                conversation_store.get_conversation, target.session_id
            )
            if conversation is None:
                raise HTTPException(404, "Session not found")
            if conversation.archived:
                raise HTTPException(409, "Session is archived; refresh before release")
        admission = getattr(request.app.state, "startup_admission", None)
        store = getattr(request.app.state, "startup_store", None)
        if admission is None or store is None or admission.policy.mode != "startup_only":
            raise HTTPException(409, "Startup-only queue is not enabled")
        receipt = await asyncio.to_thread(
            store.release_startups,
            host_id,
            body,
            actor_user_id=user_id,
            actor_account_generation=account_generation(user_id) if user_id is not None else None,
        )
        for target in receipt.targets:
            status = await asyncio.to_thread(store.presentation, target.session_id)
            session_stream.publish(
                target.session_id,
                SessionStartupEvent(
                    type="session.startup", conversation_id=target.session_id, startup=status
                ).model_dump(),
            )
        await admission.advance(host_id)
        return receipt

    @router.post("/sessions/{session_id}/startup/recreate-exemption", include_in_schema=False)
    async def recreate_exemption(request: Request, session_id: str) -> dict[str, bool]:
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if (
            not token
            or conv is None
            or not conv.runner_id
            or token_bound_runner_id(token) != conv.runner_id
        ):
            raise HTTPException(403, "Recreate exemption requires the bound runner")
        admission = getattr(request.app.state, "startup_admission", None)
        hosts = getattr(request.app.state, "host_registry", None)
        conn = hosts.get(conv.host_id) if hosts is not None and conv.host_id else None
        if admission is None or conn is None:
            return {"exempt": False}
        try:
            return {
                "exempt": await admission.recreate_exempt(
                    conn, session_id, expected_runner_id=token_bound_runner_id(token)
                )
            }
        except OmnigentError:
            return {"exempt": False}

    @router.post("/sessions/{session_id}/startup/claims", include_in_schema=False)
    async def claim_startup_boundary(
        request: Request, session_id: str, body: StartupBoundaryClaim
    ) -> dict[str, bool]:
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if (
            not token
            or conv is None
            or not conv.runner_id
            or token_bound_runner_id(token) != conv.runner_id
        ):
            raise HTTPException(403, "Startup claims require the bound runner")
        store: SessionStartupStore | None = getattr(request.app.state, "startup_store", None)
        hosts = getattr(request.app.state, "host_registry", None)
        conn = hosts.get(conv.host_id) if hosts is not None and conv.host_id else None
        current = await asyncio.to_thread(store.get, body.attempt.startup_id) if store else None
        if (
            store is None
            or hosts is None
            or conn is None
            or current is None
            or conv.archived
            or current.session_id != session_id
            or current.host_id != conv.host_id
            or current.attempt_id != body.attempt.attempt_id
            or current.runtime_id != body.attempt.runtime_id
            or current.input_id != body.attempt.input_id
            or conn.startup_generation != body.attempt.host_generation
            or current.admission_mode != body.attempt.mode
            or (
                body.attempt.mode == "startup_only"
                and current.policy_revision != body.attempt.policy_revision
            )
            or (
                STARTUP_QUEUE_V1
                if current.admission_mode == "startup_only"
                else "startup_admission_v1"
            )
            not in conn.hello.capabilities
            or hosts.launch_authorizer is None
        ):
            return {"granted": False}
        account = (
            AccountAuthority(conn.owner, conn.account_generation, conn.workspace_id)
            if conn.owner is not None
            else None
        )
        try:
            await hosts.authorize_launch(conn, session_id)
        except (OmnigentError, HTTPException):
            return {"granted": False}
        conv_after = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if (
            hosts.get(current.host_id) is not conn
            or conv_after is None
            or conv_after.archived
            or conv_after.host_id != current.host_id
            or conv_after.runner_id != conv.runner_id
        ):
            return {"granted": False}
        try:
            granted = await asyncio.to_thread(
                store.claim_boundary,
                current.id,
                body.attempt.attempt_id,
                runtime_id=body.attempt.runtime_id,
                host_generation=body.attempt.host_generation,
                boundary=body.boundary,
                claim_id=body.claim_id,
                expected_runner_id=conv.runner_id,
                account=account,
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.UNAUTHORIZED:
                return {"granted": False}
            raise
        return {"granted": granted and hosts.get(current.host_id) is conn}

    @router.post("/sessions/{session_id}/startup/receipts", include_in_schema=False)
    async def observe_startup(
        request: Request, session_id: str, body: StartupObservation
    ) -> dict[str, bool]:
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if (
            not token
            or conv is None
            or not conv.runner_id
            or token_bound_runner_id(token) != conv.runner_id
        ):
            raise HTTPException(403, "Startup receipts require the bound runner")
        return {
            "accepted": await accept_observation(
                request.app, conversation_store, session_id, conv.runner_id, body
            )
        }

    @router.post(
        "/sessions/{session_id}/startup/cancel",
        response_model=SessionStartupStatus,
        responses={
            403: {"description": "Owner access required"},
            404: {"description": "Session startup not found"},
            409: {"description": "Startup attempt changed or already completed"},
        },
    )
    async def cancel_startup(
        request: Request, session_id: str, body: CancelStartupRequest
    ) -> SessionStartupStatus:
        user_id = get_user_id(request, auth_provider)
        await require_access_and_level(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        store: SessionStartupStore | None = getattr(request.app.state, "startup_store", None)
        current = await asyncio.to_thread(store.get, body.startup_id) if store else None
        if current is None or current.session_id != session_id:
            raise HTTPException(404, "startup not found")
        assert store is not None
        cancelled = await asyncio.to_thread(
            store.cancel_current, current.id, expected_attempt_id=body.attempt_id
        )
        if cancelled is None:
            raise HTTPException(409, "startup attempt changed; refresh before cancelling")
        inputs = getattr(request.app.state, "startup_inputs", None)
        if inputs is not None:
            await inputs.cancel(session_id)
        status = await asyncio.to_thread(store.presentation, session_id)
        assert status is not None
        session_stream.publish(
            session_id,
            SessionStartupEvent(
                type="session.startup", conversation_id=session_id, startup=status
            ).model_dump(),
        )
        return status
