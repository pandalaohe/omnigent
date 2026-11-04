"""The owner-editable session selection document and its pure rules."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator
from pydantic.alias_generators import to_camel

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
