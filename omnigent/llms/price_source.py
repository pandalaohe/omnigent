"""Public price formats and the server's optional pricing slot.

Runners never install a map, so their existing catalog lookup is unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing


def _rate(value: Any, divisor: float = 1) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        rate = float(value) / divisor
    except (ValueError, OverflowError):
        return None
    return rate if math.isfinite(rate) and rate >= 0 else None


def _pricing(
    row: Mapping[str, Any], fields: tuple[str, str, str, str], divisor: float = 1
) -> ModelPricing | None:
    from omnigent.llms.context_window import ModelPricing

    rates = [_rate(row.get(field), divisor) for field in fields]
    if rates[0] is None or rates[1] is None:
        return None
    return ModelPricing(rates[0], rates[1], rates[2], rates[3])


def read_litellm(payload: Any) -> dict[str, ModelPricing]:
    """Read LiteLLM's per-token map, ignoring non-model and invalid entries."""
    result = {}
    if isinstance(payload, dict):
        for model, row in payload.items():
            if isinstance(model, str) and model and isinstance(row, dict):
                pricing = _pricing(
                    row,
                    (
                        "input_cost_per_token",
                        "output_cost_per_token",
                        "cache_read_input_token_cost",
                        "cache_creation_input_token_cost",
                    ),
                )
                if pricing is not None:
                    result[model.lower()] = pricing
    return result


def read_openrouter(payload: Any) -> dict[str, ModelPricing]:
    """Read OpenRouter's models response, whose prices are per-token strings."""
    result = {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model, prices = row.get("id"), row.get("pricing")
            if isinstance(model, str) and model and isinstance(prices, dict):
                pricing = _pricing(
                    prices, ("prompt", "completion", "input_cache_read", "input_cache_write")
                )
                if pricing is not None:
                    result[model.lower()] = pricing
    return result


def read_modelsdev(payload: Any) -> dict[str, ModelPricing]:
    """Read models.dev's provider dictionaries and per-million costs."""
    result = {}
    if isinstance(payload, dict):
        for provider, entry in payload.items():
            models = entry.get("models") if isinstance(entry, dict) else None
            if not isinstance(models, dict):
                continue
            for model, row in models.items():
                prices = row.get("cost") if isinstance(row, dict) else None
                if isinstance(model, str) and model and isinstance(prices, dict):
                    pricing = _pricing(
                        prices, ("input", "output", "cache_read", "cache_write"), 1_000_000
                    )
                    if pricing is not None:
                        result[f"{provider}/{model}".lower()] = pricing
    return result


READERS = {"litellm": read_litellm, "openrouter": read_openrouter, "modelsdev": read_modelsdev}


class PriceMap:
    """Exact ids followed by unambiguous last-segment matches; never fuzzy."""

    def __init__(self, prices: Mapping[str, ModelPricing]) -> None:
        self.prices = {model.lower(): price for model, price in prices.items()}
        candidates: dict[str, set[ModelPricing]] = {}
        for model, price in self.prices.items():
            candidates.setdefault(model.rsplit("/", 1)[-1], set()).add(price)
        self._bare = {
            model: next(iter(rates)) for model, rates in candidates.items() if len(rates) == 1
        }

    def get(self, model: str) -> ModelPricing | None:
        key = model.lower()
        return (
            self.prices.get(key)
            or self.prices.get(key.rsplit("/", 1)[-1])
            or self._bare.get(key.rsplit("/", 1)[-1])
        )


_selected: PriceMap | None = None


def install_price_map(prices: PriceMap | None) -> None:
    """Replace the immutable lookup atomically in the server process."""
    global _selected
    _selected = prices


def selected_price(model: str) -> ModelPricing | None:
    return _selected.get(model) if _selected is not None else None
