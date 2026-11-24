"""Field-level account/card overrides and recorded valuation fallback."""

from decimal import Decimal
from fractions import Fraction

from omnigent.llms.context_window import fetch_model_pricing
from omnigent.llms.price_source import selected_price, selected_source
from omnigent.usage.contract import AnalysisRowView
from omnigent.usage.pricing import PricingBaseline, PurchasedCard
from omnigent.usage.value import Coverage, PriceSource, Value, exact

FIELDS = ("read", "write", "cache_read", "cache_write")
COUNTS = (
    "uncached_input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
PRICE_FIELDS = (
    "input_per_token",
    "output_per_token",
    "cache_read_per_token",
    "cache_write_per_token",
)


def current_model_prices(models: set[str]) -> dict:
    """Current editor hints; never relabel or replace recorded valuations."""
    result = {}
    for model in sorted(models):
        sources = {}
        for name, price in (
            ("imported", selected_price(model)),
            ("builtin", fetch_model_pricing(model, cached_only=True)),
        ):
            sources[name] = (
                {
                    field: None
                    if getattr(price, attribute) is None
                    else format(Decimal(str(getattr(price, attribute))) * 1_000_000, "f")
                    for field, attribute in zip(FIELDS, PRICE_FIELDS, strict=True)
                }
                if price is not None
                else None
            )
        result[model] = sources
    return result


def recorded_source(ref: str | None, native: bool) -> PriceSource:
    if ref and ref.startswith("import:"):
        return PriceSource(layer="import", name="Recorded imported price", version=ref[7:])
    names = {
        "provider": "Provider estimate",
        "builtin": "Built-in price",
        "mixed": "Recorded estimate, mixed sources",
    }
    return PriceSource(
        layer=ref or "recorded",
        name=names.get(
            ref or "",
            "Recorded estimate, source not recorded"
            if native
            else "Provider estimate, version not reported",
        ),
    )


def row_value(
    row: AnalysisRowView,
    baseline: PricingBaseline,
    *,
    card: PurchasedCard | None = None,
    card_models: set[str] | None = None,
    recorded: AnalysisRowView | None = None,
) -> Value:
    """Resolve one actual row without changing its recorded valuation."""
    metadata = {"unit": "money", "currency": "USD", "cohort_id": "row", "as_of": row.hour}
    unknown = Value(unit="money", currency="USD", cohort_id="row", as_of=row.hour)
    card_rates = card.rates_usd_per_million if card else None
    if card_rates and any(getattr(card_rates, field) is not None for field in FIELDS):
        if card_rates.model is None and len(card_models or set()) > 1:
            return unknown.model_copy(update={"reason": "card rates name no model"})
    if card_rates and card_rates.model is not None and card_rates.model != row.model:
        card_rates = None
    # PriceMap's matching also rejects ambiguous bare model ids.
    overrides = {
        item.model: item
        for item in baseline.price_overrides
        if item.account_id == row.parent_account_id
    }
    key = row.model.lower()
    overrides = {name.lower(): rates for name, rates in overrides.items()}
    bare = key.rsplit("/", 1)[-1]
    account = overrides.get(key) or overrides.get(bare)
    if account is None:
        candidates = [
            rates for name, rates in overrides.items() if name.rsplit("/", 1)[-1] == bare
        ]
        if (
            candidates
            and len(
                {
                    tuple(
                        None if getattr(rates, field) is None else Fraction(getattr(rates, field))
                        for field in FIELDS
                    )
                    for rates in candidates
                }
            )
            == 1
        ):
            account = candidates[0]
    touched = any(
        getattr(rates, name) is not None
        for rates in (card_rates, account)
        if rates
        for name in FIELDS
    )
    if not touched:
        estimate = row.price_estimate
        if estimate is not None and "ambiguous_hour_cohort" in estimate.missing:
            return unknown.model_copy(update={"reason": "ambiguous_hour_cohort"})
        if estimate is not None and estimate.low_usd is not None and estimate.state != "unknown":
            return Value(
                **metadata,
                low=estimate.low_usd,
                high=estimate.high_usd,
                state="partial"
                if estimate.state == "partial" or estimate.high_usd is None
                else "exact"
                if estimate.low_usd == estimate.high_usd
                else "range",
                coverage=Coverage(
                    priced_units=estimate.priced_units
                    if estimate.priced_units is not None
                    else (row.tokens or 0)
                    if estimate.state in {"exact", "range"}
                    else 0,
                    usage_units=estimate.usage_units,
                ),
                price_sources=[
                    PriceSource(
                        layer="gateway",
                        name=estimate.price_source or "Provider estimate, version not reported",
                        version=estimate.price_version,
                        fetched_at=estimate.fetched_at,
                    )
                ],
            )
        if recorded and recorded.priced and recorded.cost_usd is not None:
            value = exact(
                Fraction(str(recorded.cost_usd)),
                coverage=Coverage(priced_units=row.tokens or 0, usage_units=row.tokens),
                price_sources=[
                    recorded_source(recorded.price_ref, row.account_id.startswith("native:"))
                ],
                **metadata,
            )
            if recorded.coverage == "partial":
                return value.model_copy(update={"state": "partial", "high": None})
            return value
    imported = selected_price(row.model)
    builtin = fetch_model_pricing(row.model, cached_only=True)
    import_source = selected_source()
    if imported is not None and imported.price_ref is not None:
        version = imported.price_ref.removeprefix("import:")
        if import_source["version"] != version:
            import_source["fetched_at"] = None
        import_source["version"] = version
    sources = []
    rates = []
    for name, price_field in zip(FIELDS, PRICE_FIELDS, strict=True):
        rate = None
        source = None
        for manual, layer, label in (
            (card_rates, "card", card.name if card else ""),
            (account, "account", row.parent_account_id),
        ):
            if manual is not None and getattr(manual, name) is not None:
                rate = Fraction(getattr(manual, name)) / 1_000_000
                source = PriceSource(layer=layer, name=label)
                break
        if rate is None:
            for price, fallback_source in (
                (imported, PriceSource(**import_source)),
                (builtin, PriceSource(layer="builtin", name="Built-in price")),
            ):
                if price is not None and getattr(price, price_field) is not None:
                    source = fallback_source
                    rate = Fraction(str(getattr(price, price_field)))
                    break
        if rate is None:
            return unknown.model_copy(update={"reason": "unresolved_price_field:" + name})
        rates.append(rate)
        if source is not None and source not in sources:
            sources.append(source)
    counts = [getattr(row, field) for field in COUNTS]
    amount = sum(
        (Fraction(count or 0) * rate for count, rate in zip(counts, rates, strict=True)),
        Fraction(0),
    )
    value = exact(
        amount,
        price_sources=sources,
        coverage=Coverage(
            priced_units=sum(count or 0 for count in counts), usage_units=row.tokens
        ),
        **metadata,
    )
    if any(count is None for count in counts) or row.coverage == "partial":
        return value.model_copy(
            update={"state": "partial", "high": None, "reason": "missing_token_partitions"}
        )
    return value
