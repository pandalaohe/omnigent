"""Public price formats and the existing pricing precedence."""

import pytest

from omnigent.llms.context_window import ModelPricing, fetch_model_pricing_with_provider
from omnigent.llms.price_source import (
    PriceMap,
    install_price_map,
    read_litellm,
    read_modelsdev,
    read_openrouter,
)


@pytest.fixture(autouse=True)
def clear_slot():
    install_price_map(None)
    yield
    install_price_map(None)


def test_formats_use_their_units_and_preserve_cache_rates():
    litellm = read_litellm(
        {
            "Vendor/Model": {
                "input_cost_per_token": 2e-6,
                "output_cost_per_token": 6e-6,
                "cache_read_input_token_cost": 0,
                "cache_creation_input_token_cost": 3e-6,
            }
        }
    )
    router = read_openrouter(
        {
            "data": [
                {
                    "id": "Vendor/Model",
                    "pricing": {
                        "prompt": "0.000002",
                        "completion": "0.000006",
                        "input_cache_read": "0",
                    },
                }
            ]
        }
    )
    modelsdev = read_modelsdev(
        {
            "vendor": {
                "models": {
                    "Model": {"cost": {"input": 2, "output": 6, "cache_read": 0, "cache_write": 3}}
                }
            }
        }
    )
    assert (
        litellm["vendor/model"] == modelsdev["vendor/model"] == ModelPricing(2e-6, 6e-6, 0, 3e-6)
    )
    assert router["vendor/model"] == ModelPricing(2e-6, 6e-6, 0)


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "unknown"])
def test_invalid_required_rates_are_skipped(value):
    assert read_litellm(
        {
            "missing": {},
            "bad": {"input_cost_per_token": value, "output_cost_per_token": 1},
            "free": {"input_cost_per_token": 0, "output_cost_per_token": 0},
        }
    ) == {"free": ModelPricing(0, 0)}


def test_exact_then_last_segment_never_fuzzy_or_ambiguous():
    prices = PriceMap(
        {
            "vendor/Model": ModelPricing(1, 2),
            "other/model": ModelPricing(3, 4),
            "alone/unique": ModelPricing(0, 0),
        }
    )
    assert prices.get("VENDOR/MODEL") == ModelPricing(1, 2)
    assert prices.get("prefix/unique") == ModelPricing(0, 0)
    assert prices.get("model") is None
    assert prices.get("model-preview") is None


def test_selected_slot_precedes_catalog_but_provider_config_wins(monkeypatch):
    catalog = ModelPricing(1, 2)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda _: catalog)
    install_price_map(PriceMap({"model": ModelPricing(3, 4)}))
    assert fetch_model_pricing_with_provider("model") == ModelPricing(3, 4)
    assert fetch_model_pricing_with_provider("other") == catalog
    config = {
        "providers": {
            "local": {
                "kind": "local",
                "default": True,
                "openai": {
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "test",
                    "pricing": {"input_per_million": 0, "output_per_million": 0},
                },
            }
        },
    }
    assert fetch_model_pricing_with_provider("model", config, "codex") == ModelPricing(0, 0)
    install_price_map(None)
    assert fetch_model_pricing_with_provider("model") == catalog
