"""Validate a report without normalizing away absent or unknown JSCalendar properties."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

SOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
OPEN_PROGRESS = frozenset({"needs-action", "in-process"})
Progress = Literal["needs-action", "in-process", "completed", "failed", "cancelled"]


def utc_timestamp(value: str) -> float:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d*[1-9])?Z", value) is None:
        raise ValueError("Expected a UTCDateTime ending in Z")
    return datetime.fromisoformat(value).timestamp()


def utc_text(value: float) -> str:
    text = datetime.fromtimestamp(value, UTC).isoformat().removesuffix("+00:00")
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "Z"


def utc_sort_key(value: str) -> str:
    """Canonical UTC text without Z sorts exactly, including sub-microsecond precision."""
    return value.removesuffix("Z")


def _utc(value: str) -> str:
    utc_timestamp(value)
    return value


def _local(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d*[1-9])?", value) is None:
        raise ValueError("Expected a LocalDateTime without an offset")
    datetime.fromisoformat(value)
    return value


def _uri(value: str) -> str:
    if not urlsplit(value).scheme or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Expected an absolute URI")
    return value


UTCDateTime = Annotated[str, AfterValidator(_utc)]
LocalDateTime = Annotated[str, AfterValidator(_local)]
URI = Annotated[str, AfterValidator(_uri)]
Short = Annotated[str, Field(max_length=64)]
Line = Annotated[str, Field(max_length=500)]


def _true(value: Any) -> bool:
    if value is not True:
        raise ValueError("Expected true")
    return True


TrueValue = Annotated[Literal[True], BeforeValidator(_true)]


@dataclass(frozen=True)
class TaskBoardLimits:
    max_body_bytes: int = 8 * 1024 * 1024
    max_entries: int = 10_000
    max_history_per_task: int = 5_000
    max_source_fields: int = 32
    max_sources: int = 64

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> TaskBoardLimits:
        board = config.get("task_board", {})
        limits = board.get("limits", {}) if isinstance(board, Mapping) else {}
        names = {item.name for item in fields(cls)}
        if (
            not isinstance(limits, Mapping)
            or set(limits) - names
            or any(type(value) is not int or value <= 0 for value in limits.values())
        ):
            raise ValueError(
                "task_board.limits requires known limits with positive integer values"
            )
        return cls(**limits)


class ReportError(ValueError):
    def __init__(
        self,
        code: str,
        path: str,
        reason: str,
        *,
        status: int = 422,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code, self.path, self.reason, self.status = code, path, reason, status
        self.params = params or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "reason": self.reason, "params": self.params}


class _Object(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def non_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Omit unknown properties instead of sending null")
        return value


class _CalendarObject(_Object):
    uid: str = Field(min_length=1, max_length=255)
    updated: UTCDateTime


class _Participant(_Object):
    roles: dict[str, TrueValue]
    object_type: Literal["Participant"] = Field(alias="@type")
    name: str | None = None
    progress: Progress = "needs-action"
    progressUpdated: UTCDateTime | None = None


class _Relation(_Object):
    object_type: Literal["Relation"] = Field(alias="@type")
    relation: dict[str, TrueValue] = Field(default_factory=dict)


class _Link(_Object):
    object_type: Literal["Link"] = Field(alias="@type")
    href: URI
    rel: str | None = None
    title: str | None = None


class _Waiting(_Object):
    on: Literal["user", "input", "auth", "dependency", "external"]
    since: UTCDateTime | None = None
    note: Line | None = None


class _GroupLabel(_Object):
    id: Short
    name: str | None = Field(default=None, max_length=128)


class _WorkSpan(_Object):
    session: str = Field(min_length=1, max_length=128)
    agent: Short | None = None
    host: str | None = Field(default=None, max_length=128)
    start: UTCDateTime = Field(alias="from")
    end: UTCDateTime | None = Field(default=None, alias="to")


class _SourceField(_Object):
    label: Short
    value: str = Field(max_length=2_000)


class _History(_Object):
    id: str = Field(min_length=1, max_length=128)
    time: UTCDateTime
    kind: Literal[
        "created",
        "state",
        "waiting",
        "assignee",
        "parent",
        "relation",
        "field",
        "comment",
        "closed",
        "reopened",
        "commit",
        "other",
    ]
    actor: str | None = Field(default=None, max_length=128)
    label: Short | None = None
    field: Short | None = None
    old: Line | None = Field(default=None, alias="from")
    new: Line | None = Field(default=None, alias="to")
    note: str | None = Field(default=None, max_length=2_000)


class _Task(_CalendarObject):
    object_type: Literal["Task"] = Field(alias="@type")
    sequence: int = Field(default=0, ge=0, le=9_007_199_254_740_991)
    title: Line | None = None
    description: str | None = Field(default=None, max_length=20_000)
    created: UTCDateTime | None = None
    progress: Progress = "needs-action"
    progressUpdated: UTCDateTime | None = None
    percentComplete: int | None = Field(default=None, ge=0, le=100)
    priority: int | None = Field(default=None, ge=0, le=9)
    start: LocalDateTime | None = None
    due: LocalDateTime | None = None
    timeZone: str | None = None
    participants: dict[str, _Participant] = Field(default_factory=dict)
    relatedTo: dict[str, _Relation] = Field(default_factory=dict)
    links: dict[str, _Link] = Field(default_factory=dict)
    keywords: dict[str, TrueValue] = Field(default_factory=dict)
    key: Short | None = Field(default=None, alias="omnigent.ai:key")
    source_state: Short | None = Field(default=None, alias="omnigent.ai:sourceState")
    resolution: Short | None = Field(default=None, alias="omnigent.ai:resolution")
    waiting: _Waiting | None = Field(default=None, alias="omnigent.ai:waiting")
    group: _GroupLabel | None = Field(default=None, alias="omnigent.ai:group")
    next_step: Line | None = Field(default=None, alias="omnigent.ai:next")
    worked_by: list[_WorkSpan] = Field(
        default_factory=list, max_length=200, alias="omnigent.ai:workedBy"
    )
    source_fields: dict[str, _SourceField] = Field(
        default_factory=dict, alias="omnigent.ai:sourceFields"
    )
    history: list[_History] = Field(default_factory=list, alias="omnigent.ai:history")

    @field_validator("timeZone")
    @classmethod
    def known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("Expected an IANA time zone") from exc
        return value


class _Group(_CalendarObject):
    object_type: Literal["Group"] = Field(alias="@type")
    entries: list[dict[str, Any]]
    title: str | None = Field(default=None, max_length=128)
    source: URI | None = None
    complete: Literal["open", "all"] | None = Field(default=None, alias="omnigent.ai:complete")


@dataclass(frozen=True)
class Report:
    tasks: list[dict[str, Any]]
    group: dict[str, Any] | None = None


@dataclass
class WriteOutcome:
    applied: int = 0
    unchanged: int = 0
    held: list[dict[str, str]] = field(default_factory=list)
    returned: int = 0
    gone: int = 0
    complete: str | None = None
    history_added: int = 0


def task_progress(task: dict[str, Any]) -> str:
    if "progress" in task:
        return task["progress"]
    states = [p.get("progress", "needs-action") for p in task.get("participants", {}).values()]
    if states and all(p == "completed" for p in states):
        return "completed"
    for state in ("failed", "in-process"):
        if state in states:
            return state
    return "needs-action"


def _validate(model: type[BaseModel], value: dict[str, Any], prefix: str) -> None:
    try:
        model.model_validate(value)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        path = prefix
        for part in error["loc"]:
            path += f"[{part}]" if isinstance(part, int) else ("." if path else "") + str(part)
        kind = error["type"]
        code = "bad_value"
        if kind == "missing":
            code = "missing_property"
        elif kind in {"string_too_long", "too_long"}:
            code = "too_long"
        elif kind.endswith("_type"):
            code = "bad_type"
        raise ReportError(code, path, "Invalid property", params={"path": path}) from None


def parse_report(body: bytes, content_type: str, limits: TaskBoardLimits) -> Report:
    if content_type.split(";", 1)[0].strip().lower() not in {
        "application/json",
        "application/jscalendar+json",
    }:
        raise ReportError("bad_type", "", "Expected a JSON media type", status=415)
    if len(body) > limits.max_body_bytes:
        raise ReportError("body_too_large", "", "Report exceeds the body limit", status=413)
    try:
        value = json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ReportError("bad_value", "", "Expected valid JSON") from None
    if not isinstance(value, dict):
        raise ReportError("bad_type", "", "Expected a Group or Task")
    group = value if value.get("@type") == "Group" else None
    if group is not None:
        _validate(_Group, group, "")
        entries = group["entries"]
    else:
        entries = [value]
    if len(entries) > limits.max_entries:
        raise ReportError("too_many_entries", "entries", "Too many report entries", status=413)
    tasks, seen = [], set()
    for i, task in enumerate(entries):
        prefix = f"entries[{i}]" if group is not None else ""
        if group is not None and "@type" in task and task["@type"] != "Task":
            continue
        _validate(_Task, task, prefix)
        for read_only in ("source", "gone", "closedSeen", "unresolved"):
            key = "omnigent.ai:" + read_only
            if key in task:
                raise ReportError(
                    "bad_value", f"{prefix}." + key if prefix else key, "Read-only property"
                )
        if task["uid"] in seen:
            raise ReportError("duplicate_uid", prefix + ".uid", "Duplicate task uid")
        seen.add(task["uid"])
        for key, maximum, code in (
            ("omnigent.ai:history", limits.max_history_per_task, "too_many_history"),
            ("omnigent.ai:sourceFields", limits.max_source_fields, "too_many_source_fields"),
        ):
            if len(task.get(key, [])) > maximum:
                raise ReportError(
                    code, (prefix + "." if prefix else "") + key, "Too many entries", status=413
                )
        tasks.append(task)
    return Report(tasks, group)


def _reject_constant(_value: str) -> Any:
    raise ValueError("JSON numbers must be finite")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number
