"""Conservative legacy-conversion inspection using server-owned mapping evidence."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from omnigent.server.session_choice import (
    ChoiceRevisionConflict,
    LimitScope,
    SessionChoice,
    choice_document,
    choice_revision,
    ordered_links,
    parse_session_choice,
    pool_of,
)
from omnigent.server.session_choice_runtime import running_pools, session_products
from omnigent.server.user_preferences_store import (
    LegacyChoiceConversionPlan,
    _legacy_choice_values,
)


def _catalog(app: Any, owner: str) -> list[dict[str, Any]]:
    from omnigent.entities import Conversation
    from omnigent.runtime.instruction_layers import agent_key_for_harness
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl

    store = app.state.agent_store
    agents = {}
    for read in (
        lambda after: store.list(limit=200, after=after),
        lambda after: store.list_user_agents(owner, limit=200, after=after),
    ):
        after = None
        while True:
            page = read(after)
            agents.update((agent.id, agent) for agent in page.data)
            if not page.has_more:
                break
            after = page.last_id
    result = []
    for agent in sorted(agents.values(), key=lambda value: value.id):
        harness = _resolve_harness_impl(
            Conversation(
                id="", root_conversation_id="", created_at=0, updated_at=0, agent_id=agent.id
            ),
            agent_store=store,
            agent_cache=app.state.agent_cache,
        )
        result.append(
            {
                "agent": agent.id,
                "bundle": agent.bundle_location,
                "harness": harness,
                "product": agent_key_for_harness(harness),
            }
        )
    return result


def conversion_preview(app: Any, owner: str, settings: SessionChoice) -> dict[str, Any]:
    """Convert exact generated pools only after catalog and live count equivalence checks.

    An arbitrary meter remains a shared account even if today's links have one user.
    The generated product:model path additionally needs the full owner/server catalog
    and both running identity sets; a singleton link never supplies that proof.
    """
    document: dict[str, Any] = choice_document(settings)
    products = session_products(app, settings) if hasattr(app.state, "agent_store") else {}
    links = list(ordered_links(settings))
    mapping = [
        {
            "agent": link.agent_id,
            "model": link.model,
            "meter": link.meter,
            "product": products.get(link.agent_id),
            "pool": pool_of(link, products.get(link.agent_id)),
        }
        for link in links
    ]
    entries = _legacy_choice_values(document)
    catalog = _catalog(app, owner) if hasattr(app.state, "agent_store") else []
    running = running_pools(app, owner, settings, products, time.time()) if catalog else None
    target = deepcopy(document)
    target.update(schemaVersion=2, limitRules=deepcopy(document.get("limitRules", [])))
    replaced = []
    new_rules = []
    unmapped = []
    for path in entries:
        if path[0] == "limits":
            assert isinstance(path[1], str)
            matches = [row for row in mapping if row["pool"] == path[1]]
            row = matches[0] if matches else {}
            reason: str | None = (
                "pool_mapping_unavailable"
                if not matches
                else "default_model_unresolved"
                if any(row["model"] is None for row in matches)
                else "shared_pool"
                if len({(row["agent"], row["model"]) for row in matches}) > 1
                else None
            )
            if reason is None:
                if any(match["meter"] is not None for match in matches):
                    reason = "exclusive_agent_ownership_unproven"
                elif any(
                    match["agent"] == row["agent"]
                    and (match["model"] is None or match["model"] == row["model"])
                    and (match["model"] is None or match["pool"] != path[1])
                    for match in mapping
                ):
                    # Omitted models can resolve into this selector on another host/project;
                    # today's default and empty running set do not prove disjoint coverage.
                    reason = "selector_pool_scope_unproven"
                elif entries[path].get("maxRunning") is not None:
                    # Auto-selection ignores a generated pool's cap; a scoped cap would apply.
                    reason = "generated_pool_cap_not_equivalent"
                elif not row["product"] or {
                    item["agent"] for item in catalog if item["product"] == row["product"]
                } != {row["agent"]}:
                    reason = "exclusive_product_ownership_unproven"
                elif any(item["harness"] is None for item in catalog):
                    reason = "catalog_identity_unavailable"
                elif any(
                    rule.agent == row["agent"]
                    and (rule.model is None or rule.model == row["model"])
                    for rule in settings.limit_rules
                ):
                    reason = "existing_scoped_rule_overlap"
                elif running is None or not running.available:
                    reason = "count_unavailable"
                else:
                    assert isinstance(row["agent"], str)
                    scope = LimitScope(agent=row["agent"], model=row["model"])
                    scoped = running.scoped(scope)
                    if scoped is None or set(scoped) != set(running.get(path[1], ())):
                        reason = "historical_count_scope_differs"
            if reason is None:
                rule = {
                    "id": "converted-"
                    + choice_revision(
                        {
                            "owner": owner,
                            "path": path,
                            "agent": row["agent"],
                            "model": row["model"],
                        }
                    )[:20],
                    "agent": row["agent"],
                    "model": row["model"],
                    **entries[path],
                }
                if any(existing["id"] == rule["id"] for existing in target["limitRules"]):
                    reason = "rule_id_conflict"
                else:
                    target["limitRules"].append(rule)
                    new_rules.append(rule)
                    replaced.append(list(path))
                    del target["limits"][path[1]]
        else:
            reason = "pool_count_scope_not_agent_scope"
        if reason is not None:
            unmapped.append({"path": list(path), "reason": reason})
    if new_rules:
        target = choice_document(parse_session_choice(target))
    return {
        "sourceRevision": choice_revision(document),
        "mappingFingerprint": choice_revision(
            {"owner": owner, "document": document, "mapping": mapping, "catalog": catalog}
        ),
        "replacedPaths": replaced,
        "newRules": new_rules,
        "newRuleIds": [rule["id"] for rule in new_rules],
        "unmapped": unmapped,
        "convertible": bool(new_rules),
        "settings": target if new_rules else None,
    }


def require_conversion_plan(
    app: Any, owner: str, settings: SessionChoice
) -> LegacyChoiceConversionPlan:
    plan = conversion_preview(app, owner, settings)
    if not plan["convertible"]:
        raise ChoiceRevisionConflict(
            "Legacy pool ownership/count equivalence is unproven; keep these legacy limits."
        )
    return LegacyChoiceConversionPlan(
        mapping_fingerprint=plan["mappingFingerprint"],
        target=parse_session_choice(plan["settings"]),
        replaced_paths=tuple(tuple(path) for path in plan["replacedPaths"]),
        new_rule_ids=tuple(plan["newRuleIds"]),
    )
