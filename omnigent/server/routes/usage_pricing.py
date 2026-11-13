"""Native settings authority for saved purchase baselines."""

from __future__ import annotations

import asyncio
from fractions import Fraction

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
from omnigent.usage.value import exact


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
        history = []
        for card in saved.cards:
            count = saved.past_period_count(card, view.generated_at)
            amount = Fraction(card.retail_amount) * Fraction(
                card.discount_override
                if card.discount_override is not None
                else saved.defaults.default_discount
            )
            payment = exact(
                amount,
                unit="money",
                currency=card.purchase_currency,
                cohort_id=card.id,
                as_of=int(view.generated_at),
            )
            total = exact(
                amount * count,
                unit="money",
                currency=card.purchase_currency,
                cohort_id=card.id,
                as_of=int(view.generated_at),
            )
            history.append(
                {
                    "card_id": card.id,
                    "periods": count,
                    "starts_on": card.starts_on,
                    "payment": payment,
                    "total": total,
                }
            )
        return JSONResponse(
            jsonable_encoder(
                dict(
                    saved.model_dump(mode="json"),
                    account_class_defaults=view.account_cost_classes,
                    renewals={
                        card.id: saved.next_renewal(card, view.generated_at)
                        for card in saved.cards
                    },
                    preview=value_usage(view, saved),
                    history=history,
                )
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
                default_classes=view.account_cost_classes,
            )
        except PricingConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return await asyncio.to_thread(response, saved, view)

    @router.post("/me/usage/pricing-baseline/preview")
    async def preview_baseline(request: Request, body: PricingBaseline) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        pricing = store()
        old = await asyncio.to_thread(pricing.read, owner)
        cards = {card.id: card for card in old.cards}
        cards.update({card.id: card for card in body.cards})
        try:
            classes = {**old.account_classes, **body.account_classes}
            merged = PricingBaseline(
                defaults=body.defaults,
                cards=list(cards.values()),
                account_classes={
                    key: value for key, value in classes.items() if value is not None
                },
            )
            if old.defaults.display_currency != body.defaults.display_currency:
                merged.defaults = body.defaults.model_copy(
                    update={"fx_to_display": {body.defaults.display_currency: "1"}}
                )
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        view = await asyncio.to_thread(service.analysis, owner, range_key="d90")
        for card in merged.cards:
            if (
                card.repeats
                and (
                    merged.account_classes.get(card.parent)
                    or view.account_cost_classes.get(card.parent)
                )
                != "subscription"
            ):
                raise HTTPException(422, "automatic renewal requires subscription cost class")
        return await asyncio.to_thread(response, merged, view)

    return router
