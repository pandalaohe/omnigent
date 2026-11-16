"""Configured external admission and temporary, host-scoped refusal exclusions."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from pydantic.alias_generators import to_camel

from omnigent.models.model_reference import ProviderGroups

if TYPE_CHECKING:
    from omnigent.server.session_choice import ChoiceRequest, FloorOff, SessionChoice
    from omnigent.usage.contract import UsageAccountView


HeaderToken = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")]


class ExternalAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, frozen=True)

    host: Annotated[StrictStr, Field(min_length=1)]
    agent: Annotated[StrictStr, Field(min_length=1)]
    admitted_by_router: StrictBool = False
    refusal_prefix: HeaderToken | None = None
    exhaustion_code: HeaderToken | None = None
    default_exclusion_seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)] = (
        600
    )
    route_tiles: tuple[Annotated[StrictStr, Field(min_length=1)], ...] = ()


def external_admission_for(
    settings: SessionChoice, host: str | None, agent: str | None
) -> ExternalAdmission | None:
    return next(
        (
            rule
            for rule in settings.external_admission
            if rule.admitted_by_router and rule.host == host and rule.agent == agent
        ),
        None,
    )


_HTTP_FAILURE = re.compile(
    r"^(?:ERROR: )?unexpected status \d{3}(?: [A-Za-z][A-Za-z ]*)?: (.*)$", re.DOTALL
)


def refusal_until(message: str, rule: ExternalAdmission, now: float) -> float | None:
    """Read a header at the failure/body start, never a substring of diagnostic prose."""
    if not rule.refusal_prefix or not rule.exhaustion_code:
        return None
    body = message.removeprefix("stream error: ")
    wrapped = _HTTP_FAILURE.fullmatch(body)
    if wrapped:
        body = wrapped[1]
    if body.startswith("{"):
        try:
            payload, end = json.JSONDecoder().raw_decode(body)
        except ValueError:
            return None
        # Codex's HTTP diagnostic appends its URL after the upstream JSON body.
        if body[end:] and not body[end:].startswith(", url: "):
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error", payload)
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            return None
        body = error["message"]
    prefix = f"[{rule.refusal_prefix}:{rule.exhaustion_code} clears_at="
    if not body.startswith(prefix):
        return None
    timestamp, closing, _ = body[len(prefix) :].partition("]")
    if not closing:
        return None
    if timestamp == "-":
        until = now + rule.default_exclusion_seconds
    else:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            until = parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    return until if math.isfinite(until) and until > now else None


TileStates = tuple[tuple[str, str], ...]
Scope = tuple[str, str, str]


def route_states(
    rule: ExternalAdmission,
    settings: SessionChoice,
    readings: Mapping[str, UsageAccountView],
    floors: Mapping[str, FloorOff],
    now: float,
    groups: ProviderGroups,
) -> TileStates:
    from omnigent.models.model_reference import canonical_pool, canonical_pool_values
    from omnigent.server.session_choice import TileLimits, tile_line

    readings = canonical_pool_values(readings, groups)
    limits = canonical_pool_values(settings.limits, groups)
    floors = canonical_pool_values(floors, groups)
    return tuple(
        (
            tile,
            tile_line(
                limits.get(tile, TileLimits()), readings.get(tile), floors.get(tile), now
            ).state,
        )
        for tile in sorted({canonical_pool(tile, groups) for tile in rule.route_tiles})
    )


@dataclass(frozen=True)
class ExternalRefusal:
    scope: Scope
    rule: ExternalAdmission
    until: float
    states: TileStates | None


@dataclass
class ExternalAdmissionState:
    exclusions: dict[Scope, ExternalRefusal] = field(default_factory=dict)
    pending: dict[tuple[str, str], ExternalRefusal] = field(default_factory=dict)

    def prune(self, now: float) -> None:
        self.exclusions = {key: item for key, item in self.exclusions.items() if item.until > now}
        self.pending = {key: item for key, item in self.pending.items() if item.until > now}

    def active(
        self,
        settings: SessionChoice,
        request: ChoiceRequest,
        readings: Mapping[str, UsageAccountView] | None,
        floors: Mapping[str, FloorOff] | None,
        now: float,
    ) -> dict[str, float]:
        self.prune(now)
        result = {}
        for scope, item in tuple(self.exclusions.items()):
            owner, host, agent = scope
            if (owner, host) != (request.owner, request.host):
                continue
            rule = external_admission_for(settings, host, agent)
            if rule is None or rule != item.rule:
                if not request.preview:
                    self.exclusions.pop(scope, None)
                continue
            if readings is not None and floors is not None:
                states = route_states(
                    rule, settings, readings, floors, now, request.provider_groups
                )
                if item.states is not None and states != item.states:
                    if not request.preview:
                        self.exclusions.pop(scope, None)
                    continue
                if item.states is None and not request.preview:
                    # Establish a baseline after a failed read, rather than claiming recovery.
                    self.exclusions[scope] = ExternalRefusal(scope, rule, item.until, states)
            result[agent] = item.until
        return result
