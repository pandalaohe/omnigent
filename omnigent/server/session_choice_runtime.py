"""Owner-scoped reads and create admission for the pure session selector."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any

from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.runtime.instruction_layers import agent_key_for_harness
from omnigent.server.agent_quota import QuotaContext
from omnigent.server.session_choice import (
    CHOICE_LABEL,
    SLOT_LABEL,
    Choice,
    ChoiceRequest,
    Link,
    RunningIdentity,
    RunningSnapshot,
    SessionChoice,
    WorkType,
    active_on,
    candidate_lines,
    catalog_model,
    check_candidate_limits,
    choice_revision,
    codex_route_pool,
    holds_slot,
    ordered_links,
    parse_session_choice,
    pin_codex_link,
    pool_of,
    pool_of_session,
    quota_reset_at,
    resolve_work_type,
    resolved_link,
    state_for,
    tile_line,
)

CHOICE_PROTOCOL_HEADER = "x-omnigent-session-choice"
_logger = logging.getLogger(__name__)


def choice_settings(app, owner):
    return choice_settings_snapshot(app, owner)[0]


def choice_settings_snapshot(app, owner):
    prefs = app.state.user_preferences_store
    if prefs is None:
        return SessionChoice(), choice_revision(None)
    try:
        saved = prefs.session_choice(owner)
        if saved.get("configurationError"):
            raise HTTPException(
                409, {"code": "session_choice_configuration", **saved["configurationError"]}
            )
        return (
            (
                parse_session_choice(saved["settings"])
                if saved["settings"] is not None
                else SessionChoice()
            ),
            saved["revision"],
        )
    except HTTPException:
        raise
    except Exception:
        _logger.exception("Session choice configuration read failed for %s", owner)
        raise HTTPException(409, {"code": "session_choice_configuration"}) from None


def session_identity(app, session):
    from omnigent.runner.routing import routing_host_id
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl, _resolve_llm_model

    model = (
        session.reported_model
        or session.model_override
        or _resolve_llm_model(
            session,
            agent_store=getattr(app.state, "agent_store", None),
            agent_cache=getattr(app.state, "agent_cache", None),
        )
    )
    if session.model_override and model and "/" not in model:
        product = agent_key_for_harness(
            _resolve_harness_impl(
                session,
                agent_store=getattr(app.state, "agent_store", None),
                agent_cache=getattr(app.state, "agent_cache", None),
            )
        )
        if codex_route_pool(session.model_override, product):
            # A bare runner report changes the model name, not the launch route.
            model = f"{session.model_override.partition('/')[0]}/{model}"
    return RunningIdentity(
        session.id,
        session.agent_id,
        routing_host_id(session, app.state.conversation_store),
        model,
    )


def session_lines(app, session, settings, readings, products, floors, now):
    tile = tile_of_session(
        session,
        settings,
        readings,
        products,
        agent_store=getattr(app.state, "agent_store", None),
        agent_cache=getattr(app.state, "agent_cache", None),
    )
    if tile is None:
        return None, ()
    identity = session_identity(app, session)
    if any(
        rule.agent == identity.agent
        and (rule.host is None or identity.host is None or rule.host == identity.host)
        and (rule.model is None or identity.model is None or rule.model == identity.model)
        and (
            (
                rule.reserve is not None
                and bool(rule.reserve.model_fields_set & {"mode", "window", "stop_pct"})
            )
            or (rule.reserve is None and "reserve" in rule.model_fields_set)
        )
        and (
            (identity.host is None and rule.host is not None)
            or (identity.model is None and rule.model is not None)
        )
        for rule in settings.limit_rules
    ):
        _logger.warning("Session choice stop identity unavailable for %s", session.id)
        settings = settings.model_copy(update={"limit_rules": ()})
    # Legacy sessions without an agent can still have a product-level stop line.
    link = Link.model_validate(
        {"agent": identity.agent or "unknown", "model": identity.model, "meter": tile}
    )
    request = ChoiceRequest("", identity.host or "", products)
    lines = candidate_lines(settings, link, request, readings, floors, now)
    if settings.schema_version == 2:
        legacy = settings.model_copy(update={"schema_version": 1})
        legacy_tile = tile_of_session(
            session,
            legacy,
            readings,
            products,
            agent_store=getattr(app.state, "agent_store", None),
            agent_cache=getattr(app.state, "agent_cache", None),
        )
        legacy_limits = settings.limits.get(legacy_tile)
        legacy_lines = (
            (
                (
                    legacy_limits,
                    tile_line(
                        legacy_limits, readings.get(legacy_tile), floors.get(legacy_tile), now
                    ),
                ),
            )
            if legacy_limits is not None
            else ()
        )
        if any(line.state == "stop" for _, line in legacy_lines):
            return legacy_tile, legacy_lines
        return tile, (*legacy_lines, *lines[1:])
    return tile, lines


def session_products(app, settings):
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl

    return {
        link.agent_id: agent_key_for_harness(
            _resolve_harness_impl(
                Conversation(
                    id="",
                    root_conversation_id="",
                    created_at=0,
                    updated_at=0,
                    agent_id=link.agent_id,
                ),
                agent_store=app.state.agent_store,
                agent_cache=app.state.agent_cache,
            )
        )
        for link in ordered_links(settings)
    }


def tile_of_session(session, settings, readings, products, *, agent_store=None, agent_cache=None):
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl, _resolve_llm_model

    product = agent_key_for_harness(
        _resolve_harness_impl(session, agent_store=agent_store, agent_cache=agent_cache)
    )
    if settings.schema_version == 2:
        record = choice_record(session)
        if record and isinstance(record.get("pool"), str) and record["pool"]:
            return record["pool"]
        model = (
            session.reported_model
            or session.model_override
            or _resolve_llm_model(session, agent_store=agent_store, agent_cache=agent_cache)
        )
        link = next(
            (
                link
                for link in ordered_links(settings)
                if link.agent_id == session.agent_id and link.model == model
            ),
            Link(agent=session.agent_id or "unknown", model=model),
        )
        return pool_of(link, product)
    pool = pool_of_session(
        session,
        settings,
        product=product,
        model=session.reported_model
        or session.model_override
        or _resolve_llm_model(session, agent_store=agent_store, agent_cache=agent_cache),
        products=products,
    )
    if pool in readings:
        return pool
    if product == "codex" and pool in {"codex-pro", "krill"}:
        return None
    if product is None:
        return None
    matches = [key for key, reading in readings.items() if reading.product == product]
    return matches[0] if len(matches) == 1 else None


def stop_targets(app, owner, settings, readings, products, tiles, now, *, stopping=False):
    from omnigent.server.session_collab import collab_owner_for

    store = app.state.conversation_store
    result = {tile: [] for tile in tiles}
    after = None
    while True:
        page = store.list_conversations(
            kind=None,
            live_only=True,
            include_archived=False,
            limit=200,
            after=after,
        )
        for session in page.data:
            if (
                session.live_status != "running"
                or session.runner_last_seen is None
                or now - session.runner_last_seen > 300
                or session.labels.get("omnigent.wrapper") == "claude-code-native-ui-subagent"
                or collab_owner_for(session, store, app.state.permission_store) != owner
            ):
                continue
            if stopping and settings.schema_version == 2:
                tile, lines = session_lines(app, session, settings, readings, products, {}, now)
                if not any(line.state == "stop" for _, line in lines):
                    continue
            else:
                tile = tile_of_session(
                    session,
                    settings,
                    readings,
                    products,
                    agent_store=getattr(app.state, "agent_store", None),
                    agent_cache=getattr(app.state, "agent_cache", None),
                )
            if tile in result:
                result[tile].append(session.id)
        if not page.has_more or page.last_id is None:
            return {tile: tuple(ids) for tile, ids in result.items()}
        after = page.last_id


def _stopped_tile(app, conv):
    from omnigent.server.session_collab import collab_owner_for

    def read():
        owner = collab_owner_for(conv, app.state.conversation_store, app.state.permission_store)
        settings = choice_settings(app, owner)
        now = time.time()
        readings = app.state.agent_quota_service.pool_readings(QuotaContext(owner, None), now=now)
        floors = app.state.user_preferences_store.choice_floor_off(owner)
        tile, lines = session_lines(
            app, conv, settings, readings, session_products(app, settings), floors, now
        )
        return next(((tile, line) for _, line in lines if line.state == "stop"), None)

    return _read(app, conv.id, "stop line", read, None)


def stopped_line(app, conv):
    stopped = _stopped_tile(app, conv)
    return stopped[1] if stopped else None


def stop_line_text(tile, line):
    return (
        f"Stopped: {tile} {line.window} has {line.remaining_pct}% left, at or below "
        f"the stop line {line.stop_line_pct}%. Release it in Settings → Session choice, "
        f"or wait until {line.clears_at}."
    )


def refuse_if_stopped(app, conv):
    from omnigent.errors import ErrorCode, OmnigentError

    stopped = _stopped_tile(app, conv)
    if stopped is not None:
        raise OmnigentError(stop_line_text(*stopped), code=ErrorCode.QUOTA_STOPPED)


async def enforce_stop_lines(app):
    from datetime import datetime

    from omnigent.db.db_models import workspace_scope
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.server.peer_sweeper import PeerSweeper
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item
    from omnigent.server.schemas import SessionEventInput

    prefs = app.state.user_preferences_store
    if prefs is None:
        return
    owners = await asyncio.to_thread(prefs.stop_line_owners)
    semaphore = asyncio.Semaphore(8)

    async def cut(owner, sid):
        async with semaphore:
            try:
                conv = await asyncio.to_thread(app.state.conversation_store.get_conversation, sid)
                if conv is None:
                    return
                if (
                    conv.live_status != "running"
                    or conv.runner_last_seen is None
                    or time.time() - conv.runner_last_seen > 300
                ):
                    return
                stopped = await asyncio.to_thread(_stopped_tile, app, conv)
                if stopped is None:
                    return
                tile, line = stopped
                result = await app.state.quota_stop_post_event(
                    PeerSweeper._synthetic_request(sid, app),
                    sid,
                    SessionEventInput(type="stop_session", data={}),
                    acting_user_id=owner,
                )
                if (
                    not isinstance(result, dict)
                    or result.get("queued") is not False
                    or result.get("stopped") is False
                ):
                    _logger.warning("Quota stop did not stop session %s", sid)
                    return
                day = datetime.now(UTC).strftime("%Y%m%d")
                items = await asyncio.to_thread(
                    app.state.conversation_store.append,
                    sid,
                    [
                        NewConversationItem(
                            type="message",
                            response_id=f"quota-stop-{sid}",
                            stable_id=hashlib.sha256(
                                f"quota-stop-{sid}-{tile}-{day}".encode()
                            ).hexdigest()[:32],
                            data=MessageData(
                                role="assistant",
                                agent="system",
                                content=[
                                    {"type": "output_text", "text": stop_line_text(tile, line)}
                                ],
                            ),
                        )
                    ],
                )
                for item in items:
                    if not item.deduplicated:
                        _publish_external_conversation_item(sid, item)
            except Exception:
                _logger.exception("Quota stop failed for session %s", sid)

    for workspace, owner, settings in owners:
        with workspace_scope(workspace):
            try:
                now = time.time()
                readings = await asyncio.to_thread(
                    app.state.agent_quota_service.pool_readings, QuotaContext(owner, None), now=now
                )
                floors = await asyncio.to_thread(prefs.choice_floor_off, owner)
                stopped = {
                    tile
                    for tile, limits in settings.limits.items()
                    if tile_line(limits, readings.get(tile), floors.get(tile), now).state == "stop"
                }
                if settings.schema_version == 2:
                    stopped.update(readings)
                if not stopped:
                    continue
                products = await asyncio.to_thread(session_products, app, settings)
                targets = await asyncio.to_thread(
                    stop_targets,
                    app,
                    owner,
                    settings,
                    readings,
                    products,
                    stopped,
                    now,
                    stopping=True,
                )
                await asyncio.gather(*(cut(owner, sid) for ids in targets.values() for sid in ids))
            except Exception:
                _logger.exception("Quota stop round failed for owner %s", owner)


def choice_labels(choice: Choice) -> dict[str, str]:
    return _payload_labels(choice.payload())


def _payload_labels(payload: dict[str, Any]) -> dict[str, str]:
    from omnigent.db.db_models import LABEL_VALUE_MAX_LEN

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= LABEL_VALUE_MAX_LEN:
        return {CHOICE_LABEL: text}
    chunks = [
        text[index : index + LABEL_VALUE_MAX_LEN]
        for index in range(0, len(text), LABEL_VALUE_MAX_LEN)
    ]
    return {
        CHOICE_LABEL: json.dumps({"chunks": len(chunks), "pool": payload.get("pool")}),
        **{f"{CHOICE_LABEL}.{index}": chunk for index, chunk in enumerate(chunks)},
    }


def choice_record(session: Conversation) -> dict[str, Any] | None:
    try:
        record = json.loads(session.labels.get(CHOICE_LABEL, "null"))
        if isinstance(record, dict) and "chunks" in record:
            record = json.loads(
                "".join(
                    session.labels[f"{CHOICE_LABEL}.{index}"] for index in range(record["chunks"])
                )
            )
        return record if isinstance(record, dict) else None
    except (ValueError, KeyError, TypeError):
        return None


async def record_quota_exhaustion(
    app: Any,
    session: Conversation,
    message: str,
    *,
    actor: str | None,
    from_runner: bool,
    response_id: str | None,
    input_generation: int | None = None,
) -> None:
    """Record a provider error for the session's server-selected owner and pool."""
    from omnigent.runner.launch_failure import provider_billing_refusal
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.server.session_choice import ProviderRefusal, provider_route
    from omnigent.server.session_collab import collab_owner_for

    now = time.time()
    until = quota_reset_at(message, now)
    record = choice_record(session)
    pool = record.get("pool") if record else None
    reason = provider_billing_refusal(message)
    if (until is None and reason is None) or not isinstance(pool, str) or not pool:
        return
    owner = await asyncio.to_thread(
        collab_owner_for, session, app.state.conversation_store, app.state.permission_store
    )
    if not from_runner and (actor or RESERVED_USER_LOCAL) != owner:
        return
    state = state_for(app)
    # Older forwarders omit turn ids; deduplicate their exact rejection instead.
    identity = (session.id, response_id or hashlib.sha256(message.encode()).hexdigest())
    if identity in state.quota_rejections:
        return
    if reason is not None:
        if (
            not from_runner
            or not response_id
            or type(input_generation) is not int
            or input_generation <= 0
        ):
            return
        current = session_identity(app, session)
        if (
            record
            and record.get("model")
            and provider_route(None, None, record["model"])
            != provider_route(None, None, current.model)
        ):
            return
        route = provider_route(current.host, current.agent, current.model)
        key = (owner, pool, route)
        attempt = state.provider_attempts.get((*key, session.id, response_id, input_generation))
        if attempt is None or attempt <= state.provider_recoveries.get(key, float("-inf")):
            return
        state.quota_rejections.add(identity)
        state.refusals[key] = ProviderRefusal(reason, now, until, session.id, identity[1])
        return
    assert until is not None
    state.quota_rejections.add(identity)
    key = (owner, pool)
    state.exhausted[key] = max(state.exhausted.get(key, 0), until)


async def record_provider_recovery(
    app, session, *, from_runner, response_id, input_generation=None, started=False
):
    """Clear a refusal only after a newer, authenticated same-route attempt succeeds."""
    if (
        not from_runner
        or not response_id
        or type(input_generation) is not int
        or input_generation <= 0
    ):
        return
    from omnigent.server.session_choice import provider_route
    from omnigent.server.session_collab import collab_owner_for

    owner = await asyncio.to_thread(
        collab_owner_for, session, app.state.conversation_store, app.state.permission_store
    )
    current = session_identity(app, session)
    route = provider_route(current.host, current.agent, current.model)
    record = choice_record(session)
    pool = record.get("pool") if record else None
    if not isinstance(pool, str) or not pool:
        return
    state = state_for(app)
    key = (owner, pool, route)
    execution = (*key, session.id, response_id, input_generation)
    if started:
        state.provider_attempts.setdefault(execution, time.time())
        return
    attempt = state.provider_attempts.get(execution)
    if attempt is None:
        return
    state.provider_recoveries[key] = max(state.provider_recoveries.get(key, attempt), attempt)
    for key, refusal in tuple(state.refusals.items()):
        if (
            key == (owner, pool, route)
            and attempt > refusal.observed_at
            and (refusal.session_id != session.id or refusal.response_id != response_id)
        ):
            state.refusals.pop(key, None)


def _read(app: Any, owner: str, name: str, operation: Any, default: Any):
    """External reads fail open; the first failure of each read is logged once."""
    try:
        return operation()
    except Exception:
        failures = getattr(app.state, "session_choice_read_failures", None)
        if failures is None:
            failures = app.state.session_choice_read_failures = set()
        if (owner, name) not in failures:
            failures.add((owner, name))
            _logger.warning("Session choice %s read failed for %s", name, owner, exc_info=True)
        return default


async def context_request(
    app: Any,
    owner: str,
    host: str,
    settings: SessionChoice,
    caller: Conversation | None,
    *,
    user_id: str | None = None,
    project_config: dict | None = None,
    **options: Any,
) -> ChoiceRequest:
    """Verify missing candidate models against the host when its cache is stale."""
    from omnigent.calling_defaults import load_master
    from omnigent.host.frames import CAP_CACHED_MODEL_OPTIONS
    from omnigent.server.routes._host_launch import resolve_host_owner
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl
    from omnigent.server.routes.calling_defaults import _sync_pair

    if settings.schema_version == 2:
        options["master"] = await load_master(owner, app.state.user_preferences_store)
        options["project_config"] = project_config

    request = await asyncio.to_thread(
        _context_request, app, owner, host, settings, caller, **options
    )
    catalogs = getattr(app.state, "host_model_catalog_cache_store", None)
    registry = getattr(app.state, "host_registry", None)
    connection = registry.get(host) if registry is not None else None
    if catalogs is None or registry is None or connection is None:
        return request
    if CAP_CACHED_MODEL_OPTIONS not in connection.hello.capabilities:
        return request
    records = {row["harness"]: row for row in await asyncio.to_thread(catalogs.list, [host])}
    harnesses = set()
    for configured_link in ordered_links(settings):
        link = resolved_link(configured_link, request)
        if (
            not link.model
            or link.agent_id in request.unavailable
            or link.model in request.models.get(link.agent_id, ())
            or catalog_model(link.model, request.products.get(link.agent_id))
            in request.models.get(link.agent_id, ())
        ):
            continue
        probe = Conversation(
            id="", created_at=0, updated_at=0, root_conversation_id="", agent_id=link.agent_id
        )
        harness = await asyncio.to_thread(
            _resolve_harness_impl,
            probe,
            agent_store=app.state.agent_store,
            agent_cache=getattr(app.state, "agent_cache", None),
        )
        record = records.get(harness)
        if harness in {"codex", "codex-native", "pi", "pi-native"} and (
            record is None or time.time() - (record["fetched_at"] or 0) > 300
        ):
            harnesses.add(harness)
    if harnesses:
        await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id if user_id is not None else owner,
            host_id=host,
            host_store=app.state.host_store,
        )
    semaphore = asyncio.Semaphore(4)
    await asyncio.gather(
        *(
            _sync_pair(
                host_registry=registry,
                host_conn=connection,
                catalog_store=catalogs,
                semaphore=semaphore,
                host_id=host,
                harness=harness,
                refresh="cached",
            )
            for harness in harnesses
        )
    )
    if harnesses:
        return await asyncio.to_thread(
            _context_request, app, owner, host, settings, caller, **options
        )
    return request


def _context_request(
    app: Any,
    owner: str,
    host: str,
    settings: SessionChoice,
    caller: Conversation | None,
    *,
    pin: str | None = None,
    preview: bool = False,
    exclude: tuple[str, ...] = (),
    differ_from: str | None = None,
    named_agent: str | None = None,
    independent: bool = False,
    master: dict | None = None,
    project_config: dict | None = None,
    additional_agents: tuple[str, ...] = (),
) -> ChoiceRequest:
    from omnigent.harness_availability import harness_launch_availability
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl, _resolve_llm_model
    from omnigent.server.session_choice import model_family

    agents = app.state.agent_store
    cache = getattr(app.state, "agent_cache", None)
    products: dict[str, str | None] = {}
    harnesses: dict[str, str | None] = {}
    default_models: dict[str, str | None] = {}
    default_agents = {
        link.agent_id
        for link in ordered_links(settings)
        if link.model is None and link.meter in {"codex-pro", "krill"}
    }
    unavailable = set()
    host_store = getattr(app.state, "host_store", None)
    host_row = host_store.get_host(host) if host_store is not None else None
    registry = getattr(app.state, "host_registry", None)
    connection = registry.get(host) if registry is not None else None
    readiness = (
        connection.hello.configured_harnesses
        if connection is not None
        else getattr(host_row, "configured_harnesses", None)
    )
    for agent_id in dict.fromkeys(
        [*(link.agent_id for link in ordered_links(settings)), *additional_agents]
    ):
        agent = agents.get(agent_id)
        if agent is None:
            unavailable.add(agent_id)
            continue
        probe = Conversation(
            id="", created_at=0, updated_at=0, root_conversation_id="", agent_id=agent_id
        )
        harness = _resolve_harness_impl(probe, agent_store=agents, agent_cache=cache, agent=agent)
        harnesses[agent_id] = harness
        products[agent_id] = agent_key_for_harness(harness)
        if products[agent_id] == "codex" and agent_id in default_agents:
            default_models[agent_id] = _resolve_llm_model(
                probe, agent_store=agents, agent_cache=cache
            )
        if harness_launch_availability(harness, readiness)[0] is False:
            unavailable.add(agent_id)
    catalogs = getattr(app.state, "host_model_catalog_cache_store", None)
    catalog = {row["harness"]: row["models"] for row in catalogs.list([host])} if catalogs else {}
    models = {
        agent: frozenset(
            entry["id"] for entry in catalog[harness] if isinstance(entry.get("id"), str)
        )
        for agent, harness in harnesses.items()
        if harness in catalog
    }
    effective_models = {}
    if settings.schema_version == 2:
        from omnigent.calling_defaults import resolve_calling

        for agent, harness in harnesses.items():
            resolution = resolve_calling(
                explicit={"agent_id": agent},
                explicit_fields={"agent_id"},
                project_config=project_config,
                master=master or {},
                host_id=host,
                agent_harness=lambda _, bound_harness=harness: bound_harness,
            )
            probe = Conversation(
                id="", created_at=0, updated_at=0, root_conversation_id="", agent_id=agent
            )
            defaults = [
                entry["id"] for entry in catalog.get(harness, ()) if entry.get("isDefault") is True
            ]
            effective_models[agent] = (
                resolution.model
                or _resolve_llm_model(probe, agent_store=agents, agent_cache=cache)
                or (defaults[0] if len(defaults) == 1 else None)
            )
    caller_harness = _resolve_harness_impl(caller, agent_store=agents, agent_cache=cache)
    caller_model = (
        (
            caller.reported_model
            or caller.model_override
            or _resolve_llm_model(caller, agent_store=agents, agent_cache=cache)
        )
        if caller
        else None
    )
    differ_family = None
    if differ_from:
        target = app.state.conversation_store.get_conversation(differ_from)
        if target is not None:
            from omnigent.server.session_collab import collab_owner_for

            if (
                collab_owner_for(target, app.state.conversation_store, app.state.permission_store)
                != owner
            ):
                raise HTTPException(404, "Session not found")
            model = (
                target.reported_model
                or target.model_override
                or _resolve_llm_model(target, agent_store=agents, agent_cache=cache)
            )
            product = agent_key_for_harness(
                _resolve_harness_impl(target, agent_store=agents, agent_cache=cache)
            )
            differ_family = model_family(model or product or "")
    return ChoiceRequest(
        owner,
        host,
        products,
        frozenset(unavailable),
        models,
        agent_key_for_harness(caller_harness),
        caller_model,
        differ_family,
        pin,
        exclude,
        independent,
        preview,
        named_agent,
        default_models,
        effective_models,
    )


async def authorize_agents(app: Any, request: ChoiceRequest) -> ChoiceRequest:
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.routes._session_create_validation import validate_session_agent

    unavailable = set(request.unavailable)
    for agent_id in request.products:
        if agent_id in unavailable:
            continue
        try:
            await validate_session_agent(
                user_id=request.owner,
                agent_id=agent_id,
                agent_store=app.state.agent_store,
                permission_store=app.state.permission_store,
                conversation_store=app.state.conversation_store,
            )
        except OmnigentError as exc:
            if exc.code not in {ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN}:
                raise
            unavailable.add(agent_id)
    return replace(request, unavailable=frozenset(unavailable))


def running_pools(app: Any, owner: str, settings: SessionChoice, products: Any, now: float):
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl
    from omnigent.server.session_collab import collab_owners_for

    store = app.state.conversation_store
    result: dict[str, list[str]] = {}
    identities = {}
    after = None
    while True:
        page = store.list_conversations(
            kind=None,
            include_archived=False,
            limit=200,
            after=after,
        )
        owners = collab_owners_for(page.data, store, app.state.permission_store)
        for session in page.data:
            if not holds_slot(session, now):
                continue
            resolved_owner = owners.get(session.id)
            if resolved_owner is not None and resolved_owner != owner:
                continue
            identity = session_identity(app, session)
            product = agent_key_for_harness(
                _resolve_harness_impl(
                    session, agent_store=app.state.agent_store, agent_cache=app.state.agent_cache
                )
            )
            pool = pool_of_session(
                session, settings, product=product, model=identity.model, products=products
            )
            identities[session.id] = replace(
                identity, owner_known=resolved_owner is not None, pool=pool
            )
            if pool is not None and resolved_owner is not None:
                result.setdefault(pool, []).append(session.id)
        if not page.has_more or page.last_id is None:
            return RunningSnapshot(
                {pool: tuple(dict.fromkeys(ids)) for pool, ids in result.items()},
                tuple(identities.values()),
            )
        after = page.last_id


def inputs(app: Any, settings: SessionChoice, request: ChoiceRequest, now: float):
    service = getattr(app.state, "agent_quota_service", None)
    readings = (
        _read(
            app,
            request.owner,
            "quota",
            lambda: service.pool_readings(QuotaContext(request.owner, request.host), now=now),
            {},
        )
        if service
        else {}
    )
    running = _read(
        app,
        request.owner,
        "running",
        lambda: running_pools(app, request.owner, settings, request.products, now),
        RunningSnapshot(available=False),
    )
    prefs = getattr(app.state, "user_preferences_store", None)
    floors = (
        _read(app, request.owner, "floor off", lambda: prefs.choice_floor_off(request.owner), {})
        if prefs
        else {}
    )
    return state_for(app).quota_readings(request.owner, readings, now), running, floors


def limit_receipt(app, settings, revision, link, request, running, now):
    from omnigent.server.session_choice_preview import explain_limits

    pool = pool_of(link, request.products.get(link.agent_id))
    legacy = settings.limits.get(pool)
    return {
        "savedRevision": revision,
        "effectiveLimits": explain_limits(
            settings,
            link,
            request,
            running,
            now=now,
            load_sessions=app.state.conversation_store.get_conversations,
        ),
        "legacyChecks": {
            "pool": pool,
            "limits": legacy.model_dump(mode="json", by_alias=True, exclude_none=True)
            if legacy is not None
            else None,
            "linkMaxRunning": link.max_running,
        },
    }


@dataclass
class CreateChoice:
    body: Any
    choice: Choice
    host: str
    stack: AsyncExitStack
    owner: str

    async def persisted(self, app: Any, session: Conversation) -> None:
        if app.state.permission_store is not None:
            from omnigent.server.auth import LEVEL_OWNER

            await asyncio.to_thread(app.state.permission_store.ensure_user, self.owner)
            await asyncio.to_thread(
                app.state.permission_store.grant, self.owner, session.id, LEVEL_OWNER
            )
        await self.stack.aclose()

    def labels(self, now: float, *, host_load_held: bool = False) -> dict[str, str]:
        marker = (
            f"queued@{self.host}@{self.choice.pool}"
            if self.choice.outcome == "hold" or host_load_held
            else f"admitted@{self.host}@{self.choice.pool}@{now}"
        )
        return {**choice_labels(self.choice), SLOT_LABEL: marker}


@asynccontextmanager
async def admit_create(
    app: Any,
    body: Any,
    caller: Conversation | None,
    user_id: str | None,
) -> AsyncIterator[CreateChoice | None]:
    from omnigent.runner.routing import routing_host_id
    from omnigent.server.auth import LEVEL_READ
    from omnigent.server.routes._auth_helpers import require_access
    from omnigent.server.session_collab import collab_owner_for

    if caller is None or body.sub_agent_name is not None or body.harness_override == "auto":
        yield None
        return
    permissions = app.state.permission_store
    conversations = app.state.conversation_store
    if permissions is not None:
        await require_access(user_id, caller.id, LEVEL_READ, permissions, conversations)
    owner = collab_owner_for(caller, conversations, permissions)
    host = body.host_id or routing_host_id(caller, conversations)
    if not host:
        yield None
        return
    project_id = body.project_id or caller.project_id
    projects = getattr(app.state, "project_store", None)
    project = projects.get(project_id, user_id=user_id) if projects and project_id else None
    override = (
        (project.config.get("orchestration") or {}).get("session_choice") if project else None
    )
    if override == "off":
        yield None
        return
    pin = override.get("pin") if isinstance(override, dict) else None
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.state.child_cap_locks.hold(f"owner:{owner}"))
        settings, revision = await asyncio.to_thread(choice_settings_snapshot, app, owner)
        if not active_on(settings, host):
            yield None
            return
        now = time.time()
        request = await context_request(
            app,
            owner,
            host,
            settings,
            caller,
            user_id=user_id,
            project_config=project.config if project else None,
            additional_agents=(body.agent_id,)
            if body.agent_id and (body.user_named or body.choice_reason)
            else (),
            pin=pin,
            exclude=tuple(body.exclude),
            differ_from=body.differ_from,
            named_agent=body.agent_id
            if body.agent_id and not body.user_named and not body.choice_reason
            else None,
            independent=body.parent_session_id is None,
        )
        readings, running, floors = await asyncio.to_thread(inputs, app, settings, request, now)
        request = await authorize_agents(app, request)

        named = bool(body.agent_id and (body.user_named or body.choice_reason))
        if named:
            request = replace(request, preview=True)
        state = state_for(app)
        try:
            choice = state.select(
                settings, body.work_type, request, readings, running, floors, now=now
            )
        except Exception:
            _logger.exception("Session selection failed")
            raise HTTPException(409, {"code": "session_choice_unavailable"}) from None
        if named:
            from omnigent.server.routes._sessions.helpers import (
                _resolve_harness_impl,
                _resolve_llm_model,
            )

            link = Link.model_validate(
                {
                    "agent": body.agent_id,
                    "model": body.model_override,
                    "effort": body.reasoning_effort,
                }
            )
            probe = Conversation(
                id="",
                created_at=0,
                updated_at=0,
                root_conversation_id="",
                agent_id=body.agent_id,
                model_override=body.model_override,
                harness_override=body.harness_override,
            )
            product = agent_key_for_harness(
                _resolve_harness_impl(
                    probe, agent_store=app.state.agent_store, agent_cache=app.state.agent_cache
                )
            )
            model = (
                body.model_override
                or request.effective_models.get(body.agent_id)
                or await asyncio.to_thread(
                    _resolve_llm_model,
                    probe,
                    agent_store=app.state.agent_store,
                    agent_cache=app.state.agent_cache,
                )
            )
            if product == "codex" and model:
                link = link.model_copy(update={"model": model})
            pool = pool_of_session(
                probe, settings, product=product, model=model, products=request.products
            )
            if settings.schema_version == 2:
                link = link.model_copy(update={"model": model})
                request = replace(request, products={**request.products, body.agent_id: product})
                if pool is None:
                    pool = pool_of(link, product)
            if pool is not None and any(item.meter == pool for item in ordered_links(settings)):
                link = link.model_copy(update={"meter": pool})
            cap = settings.limits[pool].max_running if pool in settings.limits else None
            if pool and not cap:
                cap = next(
                    (
                        item.max_running
                        for item in ordered_links(settings)
                        if pool_of(item, request.products.get(item.agent_id)) == pool
                    ),
                    None,
                )
            if not link.meter and cap:
                link = link.model_copy(update={"max_running": cap})
            link = resolved_link(link, request)
            work, _ = resolve_work_type(settings, body.work_type)
            failure = check_candidate_limits(
                settings,
                work,
                link,
                request,
                readings,
                running,
                floors,
                now,
                user_named=body.user_named,
            )
            if failure:
                receipt = await asyncio.to_thread(
                    limit_receipt, app, settings, revision, link, request, running, now
                )
                code = {
                    "stop": "agent_stopped",
                    "exhausted": "agent_exhausted",
                    "reserve": "agent_reserved",
                    "busy": "agent_busy",
                }.get(failure.why, failure.why)
                raise HTTPException(
                    409,
                    {
                        **receipt,
                        "code": code,
                        "running": list(failure.running),
                        "next": choice.payload(),
                        "reset": failure.until,
                    },
                )
            choice = replace(
                choice,
                outcome="pick",
                link=link,
                tier=None,
                pool=pool,
                reason="Named by user" if body.user_named else body.choice_reason,
                flags=("user_named" if body.user_named else "choice_reason",),
            )
        if choice.outcome == "ask":
            payload = choice.payload()
            payload["skipped"] = [
                {
                    **item.payload(),
                    **await asyncio.to_thread(
                        limit_receipt, app, settings, revision, item.link, request, running, now
                    ),
                }
                for item in choice.skipped
            ]
            raise HTTPException(
                409, {"code": "session_choice_ask", **payload, "savedRevision": revision}
            )
        assert choice.link is not None
        link = choice.link
        product = request.products.get(link.agent_id)
        if product == "codex" and link.model is None and link.meter in {"codex-pro", "krill"}:
            raise HTTPException(
                409,
                {"code": "session_choice_ask", "reason": "Choose a model for this Codex route"},
            )
        link = pin_codex_link(link, product)
        choice = replace(choice, link=link)
        receipt = await asyncio.to_thread(
            limit_receipt, app, settings, revision, link, request, running, now
        )
        choice = replace(
            choice,
            saved_revision=receipt["savedRevision"],
            effective_limits=receipt["effectiveLimits"],
            legacy_checks=receipt["legacyChecks"],
        )
        updated = body.model_copy(
            update={
                "agent_id": link.agent_id,
                "model_override": link.model,
                "reasoning_effort": link.effort,
            }
        )
        yield CreateChoice(updated, choice, host, stack, owner)


async def release_choice(app: Any, session: Conversation, host: str) -> bool:
    """Reserve only the held session's bound pool before doing any host work."""
    from omnigent.server.session_choice import _filter_link
    from omnigent.server.session_collab import collab_owner_for

    marker = session.labels.get(SLOT_LABEL, "")
    if not marker.startswith(("queued@", "releasing@")):
        return True
    record = choice_record(session)
    if record is None:
        return False
    owner = collab_owner_for(session, app.state.conversation_store, app.state.permission_store)
    async with app.state.child_cap_locks.hold(f"owner:{owner}"):
        current = await asyncio.to_thread(
            app.state.conversation_store.get_conversation, session.id
        )
        if current is None or current.labels.get(SLOT_LABEL) != marker:
            return False
        now = time.time()
        if marker.startswith("releasing@") and now - float(marker.rsplit("@", 1)[-1]) < 120:
            return False
        try:
            settings, revision = await asyncio.to_thread(choice_settings_snapshot, app, owner)
        except HTTPException:
            _logger.warning("Session choice release configuration unavailable for %s", session.id)
            return False
        link = Link.model_validate(
            {
                key: record[key]
                for key in ("agent", "model", "effort", "meter", "maxRunning")
                if key in record
            }
        )
        # The label's pool owns the queued session even after a meter edit.
        link = link.model_copy(
            update={"meter": record.get("pool") if record.get("meter") else None}
        )
        if settings.schema_version == 2:
            identity = await asyncio.to_thread(session_identity, app, current)
            if identity.agent is None or identity.host != host:
                return False
            link = link.model_copy(update={"agent_id": identity.agent, "model": identity.model})
        if settings.work_types:
            work, _ = resolve_work_type(settings, record.get("work_type"))
        else:
            settings = settings.model_copy(
                update={"work_types": (WorkType(id="work", label="Work"),)}
            )
            work = settings.work_types[0]
        request = await context_request(
            app, owner, host, settings, None, additional_agents=(link.agent_id,)
        )
        request = await authorize_agents(app, request)
        readings, running, floors = await asyncio.to_thread(inputs, app, settings, request, now)
        running = (
            running.without(session.id)
            if isinstance(running, RunningSnapshot)
            else {
                pool: tuple(sid for sid in ids if sid != session.id)
                for pool, ids in running.items()
            }
        )
        cooldowns = {
            pool: until
            for (user, pool), until in state_for(app).cooldowns.items()
            if user == owner
        }
        if "user_named" in record.get("flags", ()):
            failure = check_candidate_limits(
                settings, work, link, request, readings, running, floors, now, user_named=True
            )
            if failure is not None:
                return False
        else:
            failure = _filter_link(
                settings, work, link, request, readings, running, floors, cooldowns, now
            )
            if failure is not None:
                return False
        receipt = await asyncio.to_thread(
            limit_receipt, app, settings, revision, link, request, running, now
        )
        return await asyncio.to_thread(
            app.state.conversation_store.compare_and_set_label,
            session.id,
            SLOT_LABEL,
            marker,
            f"releasing@{host}@{record.get('pool')}@{now}",
            updates=_payload_labels(
                {
                    **record,
                    **link.model_dump(mode="json", by_alias=True, exclude_none=True),
                    **receipt,
                    "outcome": "pick",
                    "reason": "Queued session admitted",
                }
            ),
        )
