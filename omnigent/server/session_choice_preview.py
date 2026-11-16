"""Read-only explanations of exact candidates, including candidates outside tiers."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from fastapi import HTTPException

from omnigent.server.session_choice import (
    SLOT_LABEL,
    Link,
    RunningSnapshot,
    SessionChoice,
    WorkType,
    _candidate_link,
    _filter_link,
    active_on,
    candidate_lines,
    choice_revision,
    ordered_links,
    parse_session_choice,
    pool_limits,
    pool_of,
    resolve_work_type,
    rule_limits,
)
from omnigent.server.session_choice_runtime import context_request, inputs


def read_preview_settings(store, owner, *, draft=None):
    """Bind explanations to the exact revision read, with visible configuration errors."""
    saved: dict[str, Any] = (
        store.session_choice(owner)
        if store is not None
        else {"settings": None, "revision": choice_revision(None)}
    )
    if draft is not None:
        return parse_session_choice(draft), saved
    if saved.get("configurationError"):
        raise HTTPException(
            409, {"code": "session_choice_configuration", **saved["configurationError"]}
        )
    return (
        parse_session_choice(saved["settings"])
        if saved["settings"] is not None
        else SessionChoice()
    ), saved


def explain_limits(
    settings,
    link,
    context,
    running,
    *,
    include_running=False,
    sessions=None,
    now=None,
    load_sessions=None,
):
    _, fields = rule_limits(
        settings, link.agent_id, context.host, link.model, context.provider_groups
    )
    result = {name: field.payload() for name, field in fields.items()}
    source = fields["maxRunning"]
    ids = (
        running.scoped(source.scope, context.provider_groups)
        if source.scope is not None and isinstance(running, RunningSnapshot)
        else None
    )
    result["maxRunning"].update(
        runningCount=len(ids) if ids is not None else None,
        reservedCount=None,
        countState="known"
        if ids is not None
        else "unknown"
        if source.defined
        else "not_applicable",
    )
    if ids is not None and sessions is None and load_sessions is not None:
        sessions = load_sessions(list(ids))
    if ids is not None and sessions is not None:
        reserved = 0
        for sid in ids:
            session = sessions.get(sid)
            if session is None:
                break
            marker = session.labels.get(SLOT_LABEL, "")
            if marker.startswith(("admitted@", "releasing@")):
                with suppress(ValueError):
                    reserved += (now if now is not None else time.time()) - float(
                        marker.rsplit("@", 1)[-1]
                    ) < 120
        else:
            result["maxRunning"]["reservedCount"] = reserved
    if include_running:
        result["maxRunning"]["runningSessionIds"] = list(ids) if ids is not None else None
    return result


async def validate_known_windows(app, owner, settings):
    """Check only available metadata; offline and unreported windows remain unknown."""
    if settings.schema_version != 2 or not settings.limit_rules:
        return
    hosts = getattr(app.state, "host_store", None)
    if hosts is None:
        return
    known = await asyncio.to_thread(hosts.list_hosts, owner)
    agents = tuple(dict.fromkeys(rule.agent for rule in settings.limit_rules))
    from omnigent.server.agent_quota import QuotaContext

    service = getattr(app.state, "agent_quota_service", None)
    readings = (
        await asyncio.to_thread(service.pool_readings, QuotaContext(owner, None))
        if service
        else {}
    )
    for host in known:
        context = await context_request(
            app,
            owner,
            host.host_id,
            settings,
            None,
            preview=True,
            additional_agents=agents,
        )
        for agent in agents:
            models = {
                None,
                *context.models.get(agent, ()),
                *(rule.model for rule in settings.limit_rules if rule.agent == agent),
                *(link.model for link in ordered_links(settings) if link.agent_id == agent),
            }
            for model in models:
                resolved = model or context.effective_models.get(agent)
                limits, fields = rule_limits(
                    settings, agent, host.host_id, resolved, context.provider_groups
                )
                reserve = limits.reserve
                if reserve is None or reserve.mode != "daily":
                    continue
                link = next(
                    (
                        link
                        for link in ordered_links(settings)
                        if link.agent_id == agent
                        and (link.model or context.effective_models.get(agent)) == resolved
                    ),
                    Link(agent=agent, model=resolved),
                ).model_copy(update={"model": resolved})
                reading = readings.get(
                    pool_of(link, context.products.get(agent), context.provider_groups)
                )
                window = (
                    next((w for w in reading.windows if w.id == reserve.window), None)
                    if reading
                    else None
                )
                if window is not None and (
                    (window.window_minutes is not None and window.window_minutes < 1440)
                    or window.resets_at is None
                ):
                    raise HTTPException(
                        422,
                        {
                            "code": "daily_window_unavailable",
                            "agent": agent,
                            "host": host.host_id,
                            "model": resolved,
                            "fields": {
                                key: fields[key].payload()
                                for key in ("reserve.mode", "reserve.window")
                            },
                        },
                    )


async def candidate_preview(
    app: Any,
    owner: str,
    host: str,
    settings: SessionChoice,
    agent_id: str,
    model: str | None,
    *,
    caller=None,
    work_type=None,
    include_running=False,
) -> dict[str, Any]:
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.routes._session_create_validation import validate_session_agent

    try:
        await validate_session_agent(
            user_id=owner,
            agent_id=agent_id,
            agent_store=app.state.agent_store,
            permission_store=app.state.permission_store,
            conversation_store=app.state.conversation_store,
        )
    except OmnigentError as exc:
        if exc.code in {ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN}:
            raise HTTPException(404, "Agent not found") from None
        raise
    # v2 resolves the same calling-default chain used during admission, even for a v1 draft.
    context_settings = settings.model_copy(update={"schema_version": 2})
    context = await context_request(
        app,
        owner,
        host,
        context_settings,
        caller,
        preview=True,
        additional_agents=(agent_id,),
    )
    resolved = model or context.effective_models.get(agent_id)
    if resolved is None:
        raise HTTPException(409, {"code": "candidate_unresolved"})
    links = [
        link
        for link in ordered_links(settings)
        if link.agent_id == agent_id
        and (link.model or context.effective_models.get(agent_id)) == resolved
    ]
    # A request never supplies its own pool. Follow the first exact runtime link.
    link = _candidate_link(
        settings,
        (links[0] if links else Link(agent=agent_id)).model_copy(update={"model": resolved}),
        context,
    )
    resolved = link.model
    now = time.time()
    readings, running, floors = await asyncio.to_thread(inputs, app, settings, context, now)
    work, flags = (
        resolve_work_type(settings, work_type)
        if settings.work_types
        else (
            WorkType(id="work", label="Work"),
            ("work_type_unconfigured",),
        )
    )
    from omnigent.server.session_choice import state_for
    from omnigent.stores.host_store import host_is_live

    cooldowns = {
        pool: until for (user, pool), until in state_for(app).cooldowns.items() if user == owner
    }
    failure = _filter_link(
        settings, work, link, context, readings, running, floors, cooldowns, now
    )
    host_row = await asyncio.to_thread(app.state.host_store.get_host, host)
    offline = host_row is None or not host_is_live(host_row)
    lines = candidate_lines(settings, link, context, readings, floors, now)
    unknown = any(line.state == "unknown" for _, line in lines) or (
        link._model_resolution is not None
        and link._model_resolution.outcome in {"missing", "unverified"}
    )
    _, fields = rule_limits(settings, agent_id, host, resolved, context.provider_groups)
    scope = fields["maxRunning"].scope
    scoped_ids = (
        running.scoped(scope, context.provider_groups)
        if isinstance(running, RunningSnapshot) and scope is not None
        else None
    )
    sessions = (
        await asyncio.to_thread(app.state.conversation_store.get_conversations, list(scoped_ids))
        if scoped_ids is not None
        else None
    )
    return {
        "mode": "candidate",
        "candidate": {"agent_id": agent_id, "host_id": host, "model": resolved},
        "requestedModel": model,
        "work_type": work.id if settings.work_types else None,
        "flags": list(flags),
        "policyActive": active_on(settings, host),
        "configuredInTiers": any(
            item.agent_id == agent_id
            and _candidate_link(settings, item, context).model == resolved
            for tiers in settings.hosts.get(host, {}).values()
            for tier in tiers
            for row in tier
            for item in row.links
        ),
        "effectiveLimits": explain_limits(
            settings,
            link,
            context,
            running,
            include_running=include_running,
            sessions=sessions,
            now=now,
        ),
        "legacyChecks": {
            "pool": pool_of(link, context.products.get(agent_id), context.provider_groups),
            "limits": pool_limits(
                settings, link, context.products.get(agent_id), context.provider_groups
            ),
            "linkMaxRunning": link.max_running,
        },
        "lines": [asdict(line) for _, line in lines],
        "check": {
            "state": "unknown"
            if failure and failure.why in {"count_unavailable", "identity_unavailable"}
            else "blocked"
            if failure or offline
            else "unknown"
            if unknown
            else "passes_checks",
            "reason": failure.why
            if failure
            else "host_offline"
            if offline
            else "reading_unknown"
            if unknown
            else None,
        },
        "admission": False,
    }
