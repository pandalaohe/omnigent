"""Atomic saves, retained purchases and owner/workspace isolation on every backend."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.usage_pricing_store import PricingConflict, UsagePricingStore
from omnigent.usage.pricing import PricingBaseline, PricingDefaults
from tests.server.test_usage_pricing_math import card

OWNER = "owner@example.test"


def save(store, state):
    return store.save(OWNER, state, known_accounts={"account-a"}, pools=set())


def test_roundtrip_retroactive_and_scoping(db_uri):
    store = UsagePricingStore(db_uri)
    state = PricingBaseline(cards=[card(retail_amount="12345678901234567890.123456789012345678")])
    saved = save(store, state)
    assert saved.revision == 1
    assert store.read(OWNER).cards[0].retail_amount == state.cards[0].retail_amount
    assert store.read("other@example.test").cards == []
    with workspace_scope(7):
        assert store.read(OWNER).revision == 0
    changed = save(
        store, PricingBaseline(revision=1, defaults=PricingDefaults(default_discount="0.6"))
    )
    assert changed.cards[0].retail_amount == state.cards[0].retail_amount
    assert changed.defaults.default_discount == "0.6"
    with pytest.raises(PricingConflict):
        save(store, state)
    assert store.read(OWNER).revision == 2


def test_conflicting_target_fx_and_overlap_leave_state_unchanged(db_uri):
    store = UsagePricingStore(db_uri)
    save(store, PricingBaseline(cards=[card()]))
    for state in [
        PricingBaseline(revision=1, cards=[card(id="wrong", target_id="other")]),
        PricingBaseline(revision=1, cards=[card(id="overlap")]),
    ]:
        with pytest.raises(ValueError):
            save(store, state)
        assert store.read(OWNER).revision == 1
    # A retained, formerly-known target remains editable.
    stored = store.read(OWNER)
    stored.cards[0].discount_override = "0.6"
    store.save(OWNER, stored, known_accounts=set(), pools=set())
    assert store.read(OWNER).cards[0].discount_override == "0.6"
    changed = save(
        store,
        PricingBaseline(
            revision=2,
            defaults=PricingDefaults(display_currency="HKD", fx_to_display={"USD": "7.8"}),
        ),
    )
    assert changed.defaults.fx_to_display == {"HKD": "1"}


def test_concurrent_first_save_has_one_revision_winner(db_uri):
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=15)
        try:
            save(UsagePricingStore(db_uri), PricingBaseline(cards=[card()]))
            return "saved"
        except PricingConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(attempt) for _ in range(2)]
        assert sorted(result.result(timeout=40) for result in results) == ["conflict", "saved"]
    assert UsagePricingStore(db_uri).read(OWNER).revision == 1
