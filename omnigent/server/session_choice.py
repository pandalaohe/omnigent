"""The owner-editable session selection document and its pure rules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.usage.contract import UsageAccountView

CHOICE_LABEL = "omnigent.session_choice"
SLOT_LABEL = "omnigent.startup.session_choice"
CounterKey = tuple[str, str, str, int, int]

PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=32)]


class ChoiceRevisionConflict(ValueError):
    """Another client saved the document since the caller read it."""


class _DocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, frozen=True)


class Link(_DocumentModel):
    agent_id: Annotated[StrictStr, Field(alias="agent", min_length=1)]
    model: StrictStr | None = None
    effort: StrictStr | None = None
    meter: StrictStr | None = None
    max_running: PositiveInt | None = None

    @model_validator(mode="after")
    def no_meter_limit(self) -> Self:
        if self.meter and self.max_running is not None:
            raise ValueError("maxRunning belongs in limits when a meter is selected")
        return self


class Preset(_DocumentModel):
    id: Identifier
    name: Annotated[StrictStr, Field(min_length=1)]
    links: Annotated[tuple[Link, ...], Field(min_length=1)]


class Row(_DocumentModel):
    preset: Identifier | None = None
    links: Annotated[tuple[Link, ...], Field(min_length=1)]
    weight: Annotated[int, Field(strict=True, ge=1, le=9)] | None = None
    adjusted: StrictBool = False


class WorkType(_DocumentModel):
    id: Identifier
    label: Annotated[StrictStr, Field(min_length=1)]
    definition: Annotated[StrictStr, Field(max_length=160)] = ""
    aliases: tuple[Identifier, ...] = ()
    if_full: Literal["queue", "ask"] = "queue"

    @model_validator(mode="after")
    def valid_id(self) -> Self:
        if self.id != self.id.lower() or re.search(r"\s", self.id):
            raise ValueError("id must be lower-case with spaces replaced by '-' (CJK allowed)")
        return self


class Reserve(_DocumentModel):
    mode: Literal["daily", "total"]
    window: Annotated[StrictStr, Field(min_length=1)]
    pct: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]


class TileLimits(_DocumentModel):
    reserve: Reserve | None = None
    reserve_exempt: tuple[Identifier, ...] = ()
    max_running: PositiveInt | None = None


class SessionChoice(_DocumentModel):
    enabled: StrictBool = False
    strategy: Literal["drain", "balance"] = "drain"
    default_work_type: Identifier | None = None
    work_types: Annotated[tuple[WorkType, ...], Field(max_length=24)] = ()
    presets: tuple[Preset, ...] = ()
    limits: dict[str, TileLimits] = Field(default_factory=dict)
    hosts: dict[str, dict[str, tuple[tuple[Row, ...], ...]]] = Field(default_factory=dict)
    ability_order: tuple[tuple[Identifier, ...], ...] = ()

    @model_validator(mode="after")
    def references(self) -> Self:
        ids = [work.id for work in self.work_types]
        names = [name for work in self.work_types for name in (work.id, *work.aliases)]
        if len(set(names)) != len(names):
            raise ValueError("workTypes ids and aliases must be unique")
        if self.default_work_type is not None and self.default_work_type not in ids:
            raise ValueError("defaultWorkType must name an existing work type")
        presets = [preset.id for preset in self.presets]
        if len(set(presets)) != len(presets):
            raise ValueError("presets ids must be unique")
        for host, types in self.hosts.items():
            if not host or any(work_type not in ids for work_type in types):
                raise ValueError("hosts must name a host and existing workTypes")
        for tile, limits in self.limits.items():
            if not tile or any(work_type not in ids for work_type in limits.reserve_exempt):
                raise ValueError("limits reserveExempt must name existing workTypes")
        abilities = [preset for level in self.ability_order for preset in level]
        if len(set(abilities)) != len(abilities) or any(p not in presets for p in abilities):
            raise ValueError("abilityOrder must contain existing presets only once")
        return self


def parse_session_choice(value: object) -> SessionChoice:
    """Validate the same document at the write and runtime boundaries."""
    return SessionChoice.model_validate(value)


def choice_document(settings: SessionChoice) -> dict[str, object]:
    return settings.model_dump(mode="json", by_alias=True, exclude_none=True)


def choice_revision(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_rows(
    incoming: SessionChoice, stored: SessionChoice | None
) -> tuple[SessionChoice, list[str]]:
    """Keep untouched copies unadjusted even when their preset has since changed."""
    presets = {preset.id: preset.links for preset in incoming.presets}
    untouched: dict[str, list[tuple[Link, ...]]] = {}
    if stored is not None:
        for types in stored.hosts.values():
            for tiers in types.values():
                for tier in tiers:
                    for row in tier:
                        if row.preset and not row.adjusted:
                            untouched.setdefault(row.preset, []).append(row.links)
    corrected: list[str] = []
    hosts = {}
    for host, types in incoming.hosts.items():
        hosts[host] = {}
        for work_type, tiers in types.items():
            normalized = []
            for tier_index, tier in enumerate(tiers):
                rows = []
                for position, row in enumerate(tier):
                    if not row.adjusted and (
                        row.preset not in presets
                        or (
                            row.links != presets[row.preset]
                            and row.links not in untouched.get(row.preset, [])
                        )
                    ):
                        row = row.model_copy(update={"adjusted": True})
                        corrected.append(f"hosts.{host}.{work_type}.{tier_index}.{position}")
                    rows.append(row)
                normalized.append(tuple(rows))
            hosts[host][work_type] = tuple(normalized)
    return incoming.model_copy(update={"hosts": hosts}), corrected


def model_family(text: str) -> str:
    text = text.lower()
    for family, words in (
        ("claude", ("claude", "opus", "sonnet", "haiku")),
        ("gpt", ("gpt", "codex")),
        ("kimi", ("kimi",)),
        ("gemini", ("gemini",)),
        ("deepseek", ("deepseek",)),
    ):
        if any(word in text for word in words):
            return family
    return text


def pool_of(link: Link, product: str | None) -> str:
    return link.meter or f"{product or link.agent_id}:{link.model or ''}"


def ordered_links(settings: SessionChoice):
    for preset in settings.presets:
        yield from preset.links
    for host in sorted(settings.hosts):
        for work_type in settings.work_types:
            for tier in settings.hosts[host].get(work_type.id, ()):
                for row in tier:
                    yield from row.links


def pool_of_session(
    session: Conversation,
    settings: SessionChoice,
    *,
    product: str | None = None,
    model: str | None = None,
    products: Mapping[str, str | None] | None = None,
) -> str | None:
    """A selection label wins; otherwise the first exact product/model link owns it."""
    raw = session.labels.get(CHOICE_LABEL)
    if raw:
        try:
            record = json.loads(raw)
            if isinstance(record, dict) and isinstance(record.get("pool"), str):
                return record["pool"]
        except ValueError:
            pass
    if product is None:
        from omnigent.runtime.instruction_layers import agent_key_for_harness
        from omnigent.server.routes._sessions.helpers import _resolve_harness_impl

        product = agent_key_for_harness(_resolve_harness_impl(session))
    model = model if model is not None else session.reported_model or session.model_override
    if product is None:
        return None
    for link in ordered_links(settings):
        link_product = (products or {}).get(link.agent_id)
        if link_product is None:
            from omnigent.runtime.instruction_layers import agent_key_for_harness

            link_product = agent_key_for_harness(link.agent_id)
        if link_product == product and link.model == model:
            return pool_of(link, product)
    return None


def reserve_now(mode: str, pct: float, resets_at: float, now: float) -> float:
    return pct if mode == "total" else pct * max(0, resets_at - now) / 86400


@dataclass(frozen=True)
class FloorOff:
    until: Literal["reset", "manual"] = "reset"
    windows: Mapping[str, tuple[float, float]] = field(default_factory=dict)  # reset, used


def floor_off_active(entry: FloorOff, reading: UsageAccountView | None, now: float) -> bool:
    if entry.until == "manual":
        return True
    if not entry.windows or any(reset <= now for reset, _ in entry.windows.values()):
        return False
    if reading is None or reading.freshness != "fresh":
        return True
    windows = {window.id: window for window in reading.windows}
    for name, (reset, used) in entry.windows.items():
        current = windows.get(name)
        if (
            current is None
            or current.past_reset
            or (current.percent_used is not None and current.percent_used <= used - 5)
            or (current.resets_at is not None and abs(current.resets_at - reset) > 3600)
        ):
            return False
    return True


@dataclass(frozen=True)
class ChoiceRequest:
    owner: str
    host: str
    products: Mapping[str, str | None] = field(default_factory=dict)
    unavailable: frozenset[str] = frozenset()
    models: Mapping[str, frozenset[str]] = field(default_factory=dict)
    caller_product: str | None = None
    caller_model: str | None = None
    differ_family: str | None = None
    pin: str | None = None
    exclude: tuple[str, ...] = ()
    independent: bool = False
    preview: bool = False
    named_agent: str | None = None


@dataclass(frozen=True)
class Skipped:
    link: Link
    why: str
    until: float | None = None
    running: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "link": self.link.model_dump(mode="json", by_alias=True, exclude_none=True),
            "why": self.why,
            "until": self.until,
            "running": list(self.running),
        }


@dataclass(frozen=True)
class Choice:
    outcome: Literal["pick", "hold", "ask"]
    work_type: str
    link: Link | None
    tier: int | None
    pool: str | None
    reason: str
    skipped: tuple[Skipped, ...] = ()
    flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    clears_at: float | None = None
    work_types: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "work_type": self.work_type,
            **(
                self.link.model_dump(mode="json", by_alias=True, exclude_none=True)
                if self.link is not None
                else {}
            ),
            "tier": self.tier,
            "pool": self.pool,
            "reason": self.reason,
            "skipped": [item.payload() for item in self.skipped],
            "flags": list(self.flags),
            "warnings": list(self.warnings),
            "clears_at": self.clears_at,
            "work_types": list(self.work_types),
        }


def active_on(settings: SessionChoice, host: str) -> bool:
    return settings.enabled and bool(settings.work_types) and bool(settings.hosts.get(host))


def resolve_work_type(
    settings: SessionChoice, name: str | None
) -> tuple[WorkType, tuple[str, ...]]:
    for work_type in settings.work_types:
        if name == work_type.id or name in work_type.aliases:
            return work_type, ()
    default = next(
        (work for work in settings.work_types if work.id == settings.default_work_type),
        settings.work_types[0],
    )
    return default, ("work_type_omitted" if not name else "unknown_work_type",)


def _ability(
    settings: SessionChoice, product: str | None, model: str | None, request: ChoiceRequest
):
    levels = {
        preset: index for index, level in enumerate(settings.ability_order) for preset in level
    }
    for preset in settings.presets:
        if preset.id in levels:
            for link in preset.links:
                if (
                    request.products.get(link.agent_id, link.agent_id) == product
                    and link.model == model
                ):
                    return levels[preset.id]
    return None


def _filter_link(
    settings: SessionChoice,
    work_type: WorkType,
    link: Link,
    request: ChoiceRequest,
    readings: Mapping[str, UsageAccountView],
    running: Mapping[str, tuple[str, ...]],
    floor_off: Mapping[str, FloorOff],
    cooldowns: Mapping[str, float],
    now: float,
) -> Skipped | None:
    product = request.products.get(link.agent_id, link.agent_id)
    pool = pool_of(link, product)
    if link.agent_id in request.unavailable or (
        link.model
        and request.models.get(link.agent_id)
        and link.model not in request.models[link.agent_id]
    ):
        return Skipped(link, "unavailable")
    caller_level = _ability(settings, request.caller_product, request.caller_model, request)
    level = _ability(settings, product, link.model, request)
    if caller_level is not None and level is not None and level < caller_level:
        return Skipped(link, "above_caller")
    if (
        request.differ_family
        and model_family(link.model or product or link.agent_id) == request.differ_family
    ):
        return Skipped(link, "same_family")
    if pool in request.exclude:
        return Skipped(link, "excluded", now + 600)
    reading = readings.get(pool)
    limits = settings.limits.get(pool, TileLimits())
    if reading is not None and reading.freshness == "fresh":
        exhausted = [
            window
            for window in reading.windows
            if not window.past_reset
            and window.percent_used is not None
            and window.percent_used >= 100
            and window.resets_at is not None
            and window.resets_at > now
        ]
        if exhausted:
            return Skipped(link, "exhausted", max(window.resets_at or now for window in exhausted))
        released = pool in floor_off and floor_off_active(floor_off[pool], reading, now)
        reserve = limits.reserve
        if reserve and work_type.id not in limits.reserve_exempt and not released:
            window = next((w for w in reading.windows if w.id == reserve.window), None)
            if (
                window
                and not window.past_reset
                and window.remaining_pct is not None
                and window.resets_at is not None
                and window.resets_at > now
                and (reserve.mode == "total" or (window.window_minutes or 0) >= 1440)
                and window.remaining_pct
                <= reserve_now(reserve.mode, reserve.pct, window.resets_at, now)
            ):
                until = window.resets_at
                if reserve.mode == "daily" and reserve.pct > 0:
                    until = max(now, until - window.remaining_pct / reserve.pct * 86400)
                return Skipped(link, "reserve", until)
    cap = limits.max_running if link.meter else link.max_running
    if cap is not None and len(running.get(pool, ())) >= cap:
        return Skipped(link, "busy", now, running.get(pool, ()))
    if cooldowns.get(pool, 0) > now:
        return Skipped(link, "cooldown", cooldowns[pool])
    return None


def choose(
    settings: SessionChoice,
    work_type: str | None,
    request: ChoiceRequest,
    readings: Mapping[str, UsageAccountView],
    running: Mapping[str, tuple[str, ...]],
    floor_off: Mapping[str, FloorOff],
    cooldowns: Mapping[str, float],
    counters: MutableMapping[CounterKey, float],
    *,
    now: float,
) -> Choice:
    work, flags = resolve_work_type(settings, work_type)
    if request.named_agent:
        flags += (f"named_no_reason:{request.named_agent}",)
    tiers = settings.hosts.get(request.host, {}).get(work.id, ())
    if request.pin is not None:
        preset = next((p for p in settings.presets if p.id == request.pin), None)
        tiers = ((Row(preset=preset.id, links=preset.links),),) if preset else ()
    skipped: list[Skipped] = []
    survivors: list[tuple[int, int, Row, Link, int]] = []
    for index, tier in enumerate(tiers):
        for position, row in enumerate(tier):
            for link in row.links:
                failure = _filter_link(
                    settings, work, link, request, readings, running, floor_off, cooldowns, now
                )
                if failure is not None:
                    skipped.append(failure)
                else:
                    survivors.append((index, position, row, link, len(tier) - position))
                    break
        if survivors and settings.strategy == "drain":
            break
    if survivors and settings.strategy == "balance":

        def rank(item):
            reading = readings.get(
                pool_of(item[3], request.products.get(item[3].agent_id, item[3].agent_id))
            )
            state = reading.state if reading and reading.freshness == "fresh" else "unknown"
            return {"ok": 0, "unknown": 0, "tight": 1, "low": 2}.get(state, 0)

        best = min(rank(item) for item in survivors)
        survivors = [item for item in survivors if rank(item) == best]
        if best == 2:

            def pace(item):
                reading = readings[
                    pool_of(item[3], request.products.get(item[3].agent_id, item[3].agent_id))
                ]
                return reading.pace_pct if reading.pace_pct is not None else 0

            highest = max(pace(item) for item in survivors)
            survivors = [item for item in survivors if pace(item) == highest]
    if survivors:
        tier_index = min(item[0] for item in survivors)
        survivors = [item for item in survivors if item[0] == tier_index]
        weighted = []
        for index, position, row, link, default_weight in survivors:
            key = (request.owner, request.host, work.id, index, position)
            weight = row.weight or default_weight
            weighted.append((counters.get(key, 0) + weight, key, weight, link))
        picked = max(weighted, key=lambda item: item[0])
        if not request.preview:
            for credit, key, _, _ in weighted:
                counters[key] = credit
            counters[picked[1]] -= sum(item[2] for item in weighted)
        link = picked[3]
        pool = pool_of(link, request.products.get(link.agent_id, link.agent_id))
        reading = readings.get(pool)
        warnings = (
            (f"{pool} may not last until its reset",) if reading and reading.state == "low" else ()
        )
        return Choice(
            "pick",
            work.id,
            link,
            tier_index + 1,
            pool,
            "First eligible tier, weighted choice",
            tuple(skipped),
            flags,
            warnings,
            work_types=tuple(w.id for w in settings.work_types),
        )
    holdable = [
        item for item in skipped if item.why in {"busy", "reserve", "exhausted", "cooldown"}
    ]
    if holdable and work.if_full == "queue" and not request.independent:
        first = min(holdable, key=lambda item: (item.why != "busy", item.until or now))
        pool = pool_of(first.link, request.products.get(first.link.agent_id, first.link.agent_id))
        tier = next(
            index + 1
            for index, rows in enumerate(tiers)
            for row in rows
            if first.link in row.links
        )
        return Choice(
            "hold",
            work.id,
            first.link,
            tier,
            pool,
            first.why,
            tuple(skipped),
            flags,
            clears_at=first.until,
            work_types=tuple(w.id for w in settings.work_types),
        )
    return Choice(
        "ask",
        work.id,
        None,
        None,
        None,
        "No eligible link",
        tuple(skipped),
        flags,
        work_types=tuple(w.id for w in settings.work_types),
    )


class ChoiceState:
    """A workspace's owner-scoped counters and exclusions, discarded at restart."""

    def __init__(self) -> None:
        self.counters: dict[CounterKey, float] = {}
        self.cooldowns: dict[tuple[str, str], float] = {}
        self.tiers: dict[tuple[str, str, str, int], tuple[Row, ...]] = {}

    def saved(self, owner: str, settings: SessionChoice) -> None:
        current = {
            (owner, host, work_type, index): tier
            for host, types in settings.hosts.items()
            for work_type, tiers in types.items()
            for index, tier in enumerate(tiers)
        }
        changed = {
            key for key in self.tiers if key[0] == owner and self.tiers[key] != current.get(key)
        }
        for key in list(self.counters):
            if key[:4] in changed:
                self.counters.pop(key)
        self.tiers = {key: tier for key, tier in self.tiers.items() if key[0] != owner}
        self.tiers.update(current)

    def select(
        self,
        settings: SessionChoice,
        work_type: str | None,
        request: ChoiceRequest,
        readings: Mapping[str, UsageAccountView],
        running: Mapping[str, tuple[str, ...]],
        floor_off: Mapping[str, FloorOff],
        *,
        now: float,
    ) -> Choice:
        counters = self.counters
        if request.preview:
            preview_state = ChoiceState()
            preview_state.counters = dict(self.counters)
            preview_state.tiers = dict(self.tiers)
            preview_state.saved(request.owner, settings)
            counters = preview_state.counters
        if not request.preview:
            self.saved(request.owner, settings)
            self.cooldowns = {key: until for key, until in self.cooldowns.items() if until > now}
            for pool in request.exclude:
                self.cooldowns[(request.owner, pool)] = now + 600
        cooldowns = {
            pool: until
            for (owner, pool), until in self.cooldowns.items()
            if owner == request.owner
        }
        return choose(
            settings,
            work_type,
            request,
            readings,
            running,
            floor_off,
            cooldowns,
            counters,
            now=now,
        )


def state_for(app: Any) -> ChoiceState:
    from omnigent.db.db_models import current_workspace_id

    states = getattr(app.state, "session_choice_states", None)
    if states is None:
        states = app.state.session_choice_states = {}
    return states.setdefault(current_workspace_id(), ChoiceState())


def holds_slot(session: Conversation, now: float) -> bool:
    if session.archived:
        return False
    marker = session.labels.get(SLOT_LABEL, "")
    if marker.startswith("queued@"):
        return False
    if marker.startswith(("admitted@", "releasing@")):
        try:
            if now - float(marker.rsplit("@", 1)[-1]) < 120:
                return True
        except ValueError:
            pass
    return (
        session.live_status == "running"
        and session.runner_last_seen is not None
        and session.runner_last_seen >= now - 300
    )
