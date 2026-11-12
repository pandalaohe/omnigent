"""Native settings authority for saved purchase baselines."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.usage_pricing_store import PricingConflict
from omnigent.usage.contract import UsageAnalysisView
from omnigent.usage.pricing import PricingBaseline
from omnigent.usage.pricing_projection import value_usage


def create_usage_pricing_router(
    service: AgentQuotaService, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    def store():
        if not service.usage_enabled:
            raise HTTPException(404, "Usage is disabled")
        if service.pricing_store is None:
            raise HTTPException(503, "Pricing storage unavailable")
        return service.pricing_store

    def response(saved: PricingBaseline, view: UsageAnalysisView) -> JSONResponse:
        return JSONResponse(
            jsonable_encoder(
                dict(saved.model_dump(mode="json"), preview=value_usage(view, saved))
            ),
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/me/usage/pricing-baseline")
    async def read_baseline(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        saved = await asyncio.to_thread(store().read, owner)
        view = await asyncio.to_thread(service.analysis, owner)
        return await asyncio.to_thread(response, saved, view)

    @router.put("/me/usage/pricing-baseline")
    async def save_baseline(request: Request, body: PricingBaseline) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if getattr(request.state, "usage_read_grant_id", None) is not None:
            raise HTTPException(403, "Pricing requires settings authority")
        pricing = store()
        overview = await asyncio.to_thread(service.overview, owner)
        known = {account.id for account in overview.accounts}
        relations = (
            await asyncio.to_thread(service.pool_store.read, owner) if service.pool_store else []
        )
        view = await asyncio.to_thread(service.analysis, owner)
        try:
            saved = await asyncio.to_thread(
                pricing.save,
                owner,
                body,
                known_accounts=known,
                pools={(row.parent_account_id, row.pool_id) for row in relations},
            )
        except PricingConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return await asyncio.to_thread(response, saved, view)

    return router
