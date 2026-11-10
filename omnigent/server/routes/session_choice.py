"""The single write API for the owner's session selection document."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, UnifiedAuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.session_choice import (
    ChoiceRevisionConflict,
    SessionChoice,
    TileLimits,
    active_on,
    floor_off_active,
    parse_session_choice,
    state_for,
    tile_line,
)
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
)


class SessionChoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] | None
    expected_revision: StrictStr


class ChoicePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str
    work_type: str | None = None
    caller_id: str | None = None
    pin: str | None = None
    exclude: list[str] = []
    differ_from: str | None = None


class QuotaFloorChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tile: StrictStr
    off: bool
    keep: bool = False


def create_session_choice_router(
    store: SqlAlchemyUserPreferencesStore | None, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    def preferences() -> SqlAlchemyUserPreferencesStore:
        if store is None:
            raise HTTPException(503, "Synced user preferences are unavailable")
        return store

    async def owned_host(request: Request, host_id: str) -> str:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        hosts = getattr(request.app.state, "host_store", None)
        host = await asyncio.to_thread(hosts.get_host, host_id) if hosts else None
        if host is None or (auth_provider is not None and host.user_id != owner):
            raise HTTPException(404, "Host not found")
        return owner

    @router.get("/me/session-choice/meters")
    async def meters(request: Request, host_id: str) -> dict[str, Any]:
        from dataclasses import asdict

        from omnigent.runtime.instruction_layers import instruction_agents
        from omnigent.server.session_choice_runtime import context_request, inputs
        from omnigent.server.user_preferences_store import read_collab_settings

        owner = await owned_host(request, host_id)
        settings = (
            await asyncio.to_thread(read_collab_settings, preferences(), owner)
        ).session_choice
        context = await context_request(request.app, owner, host_id, settings, None, preview=True)
        readings, running, floors = await asyncio.to_thread(
            inputs, request.app, settings, context, time.time()
        )
        return {
            "products": {
                harness: agent.key for agent in instruction_agents() for harness in agent.harnesses
            },
            "meters": [reading.model_dump(mode="json") for reading in readings.values()],
            "running": running,
            "floors": {
                pool: {
                    **asdict(entry),
                    "active": floor_off_active(entry, readings.get(pool), time.time()),
                }
                for pool, entry in floors.items()
            },
        }

    @router.get("/me/session-choice/floor")
    async def read_floors(request: Request) -> dict[str, Any]:
        from dataclasses import asdict

        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        entries = await asyncio.to_thread(preferences().choice_floor_off, owner)
        return {pool: asdict(entry) for pool, entry in entries.items()}

    @router.post("/me/session-choice/floor", dependencies=[Depends(require_json_content_type)])
    async def change_floor(request: Request, body: QuotaFloorChange) -> dict[str, Any]:
        from omnigent.server.agent_quota import QuotaContext
        from omnigent.server.session_choice import FloorOff

        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        entry = None
        if body.off:
            if body.keep:
                entry = FloorOff(until="manual")
            else:
                readings = await asyncio.to_thread(
                    request.app.state.agent_quota_service.pool_readings, QuotaContext(owner, None)
                )
                reading = readings.get(body.tile)
                windows = (
                    {
                        window.id: (window.resets_at, 100 - window.remaining_pct)
                        for window in reading.windows
                        if window.resets_at is not None
                        and window.remaining_pct is not None
                        and not window.past_reset
                    }
                    if reading and reading.freshness == "fresh"
                    else {}
                )
                if not windows:
                    raise HTTPException(409, "No fresh reading: there is no reset to wait for.")
                entry = FloorOff(windows=windows)
        accounts_mode = (
            isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts"
        )
        try:
            await asyncio.to_thread(
                preferences().set_choice_floor_off,
                owner,
                body.tile,
                entry,
                create_if_missing=not accounts_mode,
            )
        except UserPreferencesUserNotFoundError as exc:
            raise HTTPException(401, "Account no longer exists") from exc
        return await read_floors(request)

    @router.post("/me/session-choice/preview")
    async def preview(request: Request, body: ChoicePreview) -> dict[str, Any]:
        from omnigent.server.session_choice_runtime import (
            authorize_agents,
            context_request,
            inputs,
        )
        from omnigent.server.session_collab import collab_owner_for
        from omnigent.server.user_preferences_store import read_collab_settings

        owner = await owned_host(request, body.host_id)
        settings = (
            await asyncio.to_thread(read_collab_settings, preferences(), owner)
        ).session_choice
        if not active_on(settings, body.host_id):
            return {"enabled": False}
        caller = (
            await asyncio.to_thread(
                request.app.state.conversation_store.get_conversation, body.caller_id
            )
            if body.caller_id
            else None
        )
        if (
            caller
            and await asyncio.to_thread(
                collab_owner_for,
                caller,
                request.app.state.conversation_store,
                request.app.state.permission_store,
            )
            != owner
        ):
            raise HTTPException(404, "Session not found")
        choice_request = await context_request(
            request.app,
            owner,
            body.host_id,
            settings,
            caller,
            preview=True,
            pin=body.pin,
            exclude=tuple(body.exclude),
            differ_from=body.differ_from,
        )
        now = time.time()
        choice_request = await authorize_agents(request.app, choice_request)
        readings, running, floors = await asyncio.to_thread(
            inputs, request.app, settings, choice_request, now
        )
        return (
            state_for(request.app)
            .select(settings, body.work_type, choice_request, readings, running, floors, now=now)
            .payload()
        )

    @router.get("/me/session-choice/preview")
    async def preview_query(
        request: Request, host_id: str, work_type: str | None = None
    ) -> dict[str, Any]:
        return await preview(request, ChoicePreview(host_id=host_id, work_type=work_type))

    @router.get("/me/session-choice/notice")
    async def notice(
        request: Request, host_id: str, agent_id: str, model: str | None = None
    ) -> dict[str, Any]:
        from omnigent.entities import Conversation
        from omnigent.server.session_choice import pool_of_session
        from omnigent.server.session_choice_runtime import context_request, inputs
        from omnigent.server.user_preferences_store import read_collab_settings

        owner = await owned_host(request, host_id)
        settings = (
            await asyncio.to_thread(read_collab_settings, preferences(), owner)
        ).session_choice
        if not active_on(settings, host_id):
            return {"notice": None}
        context = await context_request(request.app, owner, host_id, settings, None, preview=True)
        session = Conversation(
            id="",
            created_at=0,
            updated_at=0,
            root_conversation_id="",
            agent_id=agent_id,
            model_override=model,
        )
        pool = await asyncio.to_thread(
            pool_of_session, session, settings, products=context.products
        )
        now = time.time()
        readings, _, floors = await asyncio.to_thread(inputs, request.app, settings, context, now)
        reading = readings.get(pool)
        if reading is None or reading.freshness != "fresh":
            return {"notice": None}
        limits = settings.limits.get(pool) if pool is not None else None
        reserve = limits.reserve if limits else None
        window = next(
            (
                w
                for w in reading.windows
                if w.id == (reserve.window if reserve else reading.deciding_window)
            ),
            None,
        )
        if reserve:
            line = tile_line(limits or TileLimits(), reading, floors.get(pool), now)
            if line.state not in {"warn", "stop", "exhausted"}:
                return {"notice": None}
        elif reading.state != "low":
            return {"notice": None}
        elif window is None:
            window = next(
                (w for w in reading.windows if not w.past_reset and w.remaining_pct is not None),
                None,
            )
        if window is None or window.past_reset or window.remaining_pct is None:
            return {"notice": None}
        mode = (
            " Use up."
            if pool in floors and floors[pool].until == "manual"
            else " Floor off until reset."
            if pool in floors and floor_off_active(floors[pool], reading, now)
            else ""
        )
        reset = (
            f", resets in {round(max(0, window.resets_at - now) / 86400, 1):g}d"
            if window.resets_at is not None
            else ""
        )
        return {
            "notice": (
                f"{reading.display_name} {window.label or window.id}: "
                f"{window.remaining_pct:g}% left{reset}.{mode}"
            ),
            "pool": pool,
            "remaining_pct": window.remaining_pct,
            "resets_at": window.resets_at,
            "window": window.id,
        }

    @router.get("/me/session-choice")
    async def read(request: Request) -> dict[str, Any]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        return await asyncio.to_thread(preferences().session_choice, owner)

    @router.put("/me/session-choice", dependencies=[Depends(require_json_content_type)])
    async def update(request: Request, body: SessionChoiceUpdate) -> dict[str, Any]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        accounts_mode = (
            isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts"
        )
        try:
            result = await asyncio.to_thread(
                preferences().replace_session_choice,
                owner,
                body.settings,
                body.expected_revision,
                create_if_missing=not accounts_mode,
            )
            state_for(request.app).saved(
                owner,
                parse_session_choice(result["settings"])
                if result["settings"]
                else SessionChoice(),
            )
            return result
        except ChoiceRevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValidationError, UserPreferencesValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except UserPreferencesUserNotFoundError as exc:
            raise HTTPException(401, "Account no longer exists") from exc

    return router
