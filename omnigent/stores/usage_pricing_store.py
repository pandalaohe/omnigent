"""Atomic owner/workspace baseline edits with optimistic revision checks."""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlUsagePricingCard,
    SqlUsagePricingDefaults,
    current_workspace_id,
)
from omnigent.db.utils import run_write_transaction
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.pricing import PricingBaseline, PricingDefaults


class PricingConflict(ValueError):
    """Refresh saved state and reapply the draft."""


class UsagePricingStore:
    def __init__(self, storage_location: str) -> None:
        self.sessions = UsageSnapshotStore(storage_location)

    def read(self, owner: str) -> PricingBaseline:
        with self.sessions._read_session()("read_pricing_baseline") as session:
            return self._read(session, owner)

    @staticmethod
    def _read(session: Session, owner: str) -> PricingBaseline:
        key = (current_workspace_id(), owner)
        rows = session.execute(
            select(SqlUsagePricingDefaults, SqlUsagePricingCard)
            .outerjoin(
                SqlUsagePricingCard,
                (SqlUsagePricingCard.workspace_id == SqlUsagePricingDefaults.workspace_id)
                & (SqlUsagePricingCard.owner_id == SqlUsagePricingDefaults.owner_id),
            )
            .where(
                SqlUsagePricingDefaults.workspace_id == key[0],
                SqlUsagePricingDefaults.owner_id == owner,
            )
            .order_by(SqlUsagePricingCard.card_id)
        ).all()
        row = rows[0][0] if rows else None
        return PricingBaseline(
            revision=0 if row is None else row.revision,
            defaults=PricingDefaults()
            if row is None
            else PricingDefaults.model_validate_json(row.payload),
            cards=[json.loads(card.payload) for _, card in rows if card is not None],
        )

    def save(
        self,
        owner: str,
        edit: PricingBaseline,
        *,
        known_accounts: set[str],
        pools: set[tuple[str, str]],
    ) -> PricingBaseline:
        def apply(session: Session) -> PricingBaseline:
            require_active_account(session, owner)
            # The upsert serializes even the first save without requiring a users row.
            UsageSnapshotStore.write_in_session(
                session, owner, "usage.pricing.lock", {"schema_version": 1}
            )
            old = self._read(session, owner)
            if old.revision != edit.revision:
                raise PricingConflict("stale revision; refresh and reapply draft")
            old_cards = {card.id: card for card in old.cards}
            new_cards = dict(old_cards)
            new_cards.update({card.id: card for card in edit.cards})
            merged = PricingBaseline(
                revision=old.revision + 1, defaults=edit.defaults, cards=list(new_cards.values())
            )
            if old.defaults.display_currency != edit.defaults.display_currency:
                merged.defaults = PricingDefaults(
                    default_discount=edit.defaults.default_discount,
                    display_currency=edit.defaults.display_currency,
                )
            for card in edit.cards:
                previous = old_cards.get(card.id)
                retained_target = previous is not None and (
                    previous.target_kind,
                    previous.target_id,
                    previous.parent_account_id,
                ) == (card.target_kind, card.target_id, card.parent_account_id)
                if not retained_target and (
                    card.parent not in known_accounts
                    or (card.target_kind == "pool" and (card.parent, card.target_id) not in pools)
                ):
                    raise ValueError("target must belong to this owner/workspace")
                rates = card.rates_usd_per_million
                session.merge(
                    SqlUsagePricingCard(
                        workspace_id=current_workspace_id(),
                        owner_id=owner,
                        card_id=card.id,
                        retail_amount=Decimal(card.retail_amount),
                        allowance_usd=Decimal(card.allowance_usd),
                        discount_override=None
                        if card.discount_override is None
                        else Decimal(card.discount_override),
                        **{
                            name + "_rate": None
                            if rates is None
                            else Decimal(getattr(rates, name))
                            for name in ("read", "write", "cache_read", "cache_write")
                        },
                        payload=card.model_dump_json(),
                    )
                )
            session.merge(
                SqlUsagePricingDefaults(
                    workspace_id=current_workspace_id(),
                    owner_id=owner,
                    revision=merged.revision,
                    default_discount=Decimal(merged.defaults.default_discount),
                    cny_fx=Decimal(merged.defaults.fx_to_display["CNY"])
                    if "CNY" in merged.defaults.fx_to_display
                    else None,
                    usd_fx=Decimal(merged.defaults.fx_to_display["USD"])
                    if "USD" in merged.defaults.fx_to_display
                    else None,
                    hkd_fx=Decimal(merged.defaults.fx_to_display["HKD"])
                    if "HKD" in merged.defaults.fx_to_display
                    else None,
                    payload=merged.defaults.model_dump_json(),
                )
            )
            return merged

        return run_write_transaction(
            self.sessions._write_session(), "save_pricing_baseline", apply
        )
