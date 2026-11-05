"""Join an owner's reported models, cached host catalogs and hourly use."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from omnigent.harness_plugins import native_agents
from omnigent.llms.context_window import fetch_model_pricing_with_provider
from omnigent.runtime.instruction_layers import agent_key_for_harness
from omnigent.server.agent_quota import AgentQuotaService
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore


def usage_models(
    service: AgentQuotaService,
    catalogs: HostModelCatalogCacheStore | None,
    owner: str,
    account_id: str,
    *,
    host_id: str | None = None,
    **range_options: Any,
) -> dict[str, Any]:
    overview = service.overview(owner, include_marks=False)
    account = next((view for view in overview.accounts if view.id == account_id), None)
    if account is None:
        raise LookupError("Usage account not found")
    activity = service.activity(owner, account=account_id, include_models=True, **range_options)
    amount = next((item for item in activity["accounts"] if item["account"] == account_id), {})
    used = {item["model"]: item for item in amount.get("models", [])}
    rows: dict[str, dict[str, Any]] = {}

    def row(model: str) -> dict[str, Any]:
        return rows.setdefault(model, {"id": model, "sources": [], "usage": used.get(model)})

    for model in account.models:
        row(model.id)["sources"].append({"kind": "report", "label": account.display_name})

    hosts = service.host_store.list_hosts(owner) if service.host_store is not None else []
    by_host = {host.host_id: host for host in hosts}
    if catalogs is not None and account.product:
        for record in catalogs.list(list(by_host)):
            if agent_key_for_harness(record["harness"]) != account.product:
                continue
            origin = by_host[record["host_id"]]
            offline = (
                service.host_registry is None or service.host_registry.get(origin.host_id) is None
            )
            for model in record["models"]:
                model_id = model.get("id") if isinstance(model, dict) else None
                if not isinstance(model_id, str) or not model_id:
                    continue
                row(model_id)["sources"].append(
                    {
                        "kind": "host",
                        "host_id": origin.host_id,
                        "label": "This host" if origin.host_id == host_id else origin.name,
                        "stale": bool(record["error"]) or offline,
                        "fetched_at": record["fetched_at"],
                        "configuration": model.get("source"),
                        "harness": record["harness"],
                    }
                )
    for model in used:
        row(model)
    from omnigent.onboarding.provider_config import load_config

    provider_config = load_config()
    product_harness = next(
        (agent.harness for agent in native_agents() if agent.key == account.product), None
    )
    for model, value in rows.items():
        harnesses = {source["harness"] for source in value["sources"] if "harness" in source}
        price_candidates = {
            fetch_model_pricing_with_provider(
                model, provider_config=provider_config, harness=harness, cached_only=True
            )
            for harness in harnesses or {product_harness}
        }
        price = next(iter(price_candidates)) if len(price_candidates) == 1 else None
        value["pricing"] = asdict(price) if price is not None else None
    return {
        "account": account_id,
        "display_name": account.display_name,
        "range": activity["range"],
        "span": amount.get("span"),
        "models": [rows[model] for model in sorted(rows)],
    }
