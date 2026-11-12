"""Typed, source-supplied task board display projections."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from omnigent.server.task_board.models import SOURCE_ID_RE, ReportError, _validate, task_progress
from omnigent.server.task_board.models import UTCDateTime as ExistingUTCDateTime

UTCDateTime = Annotated[
    ExistingUTCDateTime, WithJsonSchema({"type": "string", "format": "omnigent-utc"})
]

MAX_SAFE = 9_007_199_254_740_991


def text(value: str, long: bool = False) -> str:
    if not value.strip() or any(
        unicodedata.category(c) == "Cc" and (not long or c not in "\t\n") for c in value
    ):
        raise ValueError("Expected plain nonblank text")
    return value


def short_text(value: str) -> str:
    return text(value)


def long_text(value: str) -> str:
    return text(value, True)


def valid_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected a date")
    date.fromisoformat(value)
    return value


def timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Expected an IANA time zone") from exc
    return value


def source_id(value: str) -> str:
    if not SOURCE_ID_RE.fullmatch(value):
        raise ValueError("Invalid source id")
    return value


def href(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value)
        ):
            raise ValueError("Expected a safe HTTP link")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Expected a safe HTTP link") from exc
    return value


Id = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(short_text)]
Label = Id
Key = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(short_text)]
Sentence = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(short_text)]
Note = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(long_text)]
QuoteText = Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(long_text)]
Date = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(valid_date)]
Zone = Annotated[Id, AfterValidator(timezone)]
SourceId = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"), AfterValidator(source_id)
]
Uid = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(short_text)]
Count = Annotated[int, Field(ge=0, le=MAX_SAFE)]
Rank = Annotated[int, Field(ge=-MAX_SAFE, le=MAX_SAFE)]
Number = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Expected integer version")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def no_nulls(cls, value: Any, info: Any) -> Any:
        if value is None and info.field_name not in {
            "amount",
            "tokens",
            "lastActivity",
            "end_time",
            "baseId",
        }:
            raise ValueError("Omit unknown properties instead of sending null")
        return value


class TaskRef(Closed):
    source: SourceId
    uid: Uid


class AccountRef(Closed):
    namespace: Id
    id: Id


class Link(Closed):
    id: Id
    label: Label
    href: Annotated[str, Field(max_length=2048), AfterValidator(href)]


class Cost(Closed):
    amount: Number | None
    kind: Literal["actual", "estimate"]
    tokens: Count | None = None
    sourceLabel: Label | None = None


class Badge(Closed):
    id: Id
    label: Label
    tone: Literal["neutral", "info", "success", "warning", "danger"] | None = None


class DetailRow(Closed):
    id: Id
    label: Key
    value: Note


class TaskIdentity(Closed):
    key: Key
    label: Label
    groupId: SourceId
    groupLabel: Label | None = None
    groupBadge: Label | None = None
    rank: Rank | None = None


class Actor(Closed):
    id: Id
    agentId: Id
    label: Label
    host: Label | None = None


class Question(Closed):
    id: Id
    text: Sentence


class Step(Closed):
    id: Id
    kind: Key
    label: Label
    state: Literal["done", "current", "future"]


class Tool(Closed):
    id: Id
    name: Label
    label: Label
    startedAt: UTCDateTime
    limitSeconds: Number | None = None


class Session(Closed):
    id: Id
    task: TaskRef
    parentId: Id | None = None
    role: Literal["root", "child", "peer"]
    agentId: Id
    label: Label
    title: Sentence
    host: Label | None = None
    model: Label | None = None
    effort: Label | None = None
    status: Literal["running", "idle", "archived"]
    createdAt: UTCDateTime
    lastActivityAt: UTCDateTime
    runningSince: UTCDateTime | None = None
    runningSeconds: Number | None = None
    idleSeconds: Number | None = None
    archivedAt: UTCDateTime | None = None
    endedAt: UTCDateTime | None = None
    now: Sentence | None = None
    next: Sentence | None = None
    need: Sentence | None = None
    last: Sentence | None = None
    context: Fraction | None = None
    compactAt: Fraction | None = None
    tool: Tool | None = None
    questions: list[Question] = Field(default_factory=list, max_length=32)
    steps: list[Step] = Field(default_factory=list, max_length=64)
    cost: Cost | None = None

    @model_validator(mode="after")
    def runtime(self) -> Session:
        if self.status == "running" and self.runningSince is None and self.runningSeconds is None:
            raise ValueError("Running session needs a start or duration")
        if self.status == "archived" and self.archivedAt is None:
            raise ValueError("Archived session needs archivedAt")
        unique_ids(self.questions)
        unique_ids(self.steps)
        return self


class Interval(Closed):
    id: Id
    start: UTCDateTime = Field(alias="from")
    end_time: UTCDateTime | None = Field(alias="to")
    agentId: Id
    host: Label | None = None
    sessionId: Id | None = None
    label: Label | None = None
    origin: Literal["event", "session"]
    tokens: Count | None = None
    cost: Cost | None = None


class Check(Closed):
    id: Id
    host: Label
    verdict: Literal["passed", "failed", "flaky"]
    count: Count
    at: UTCDateTime
    seconds: Number | None = None
    head: Id | None = None
    sourceLabel: Label | None = None
    link: Link | None = None


class Quote(Closed):
    id: Id
    text: QuoteText
    date: Date | None = None
    at: UTCDateTime | None = None
    task: TaskRef
    sourceLabel: Label | None = None
    sourceLink: Link | None = None
    rank: Rank | None = None


class Commit(Closed):
    id: Id
    sha: Id
    subject: Sentence
    at: UTCDateTime
    link: Link | None = None


class Event(Closed):
    id: Id
    at: UTCDateTime
    kind: Literal["open", "start", "transfer", "stop", "outcome", "check", "commit", "other"]
    task: TaskRef
    agentId: Id | None = None
    host: Label | None = None
    rootSessionId: Id | None = None
    label: Label | None = None
    from_: Label | None = Field(default=None, alias="from")
    workSeconds: Number | None = None
    checkId: Id | None = None
    commitIds: list[Id] = Field(default_factory=list, max_length=1000)
    outcome: Sentence | None = None
    sourceLabel: Label | None = None
    rank: Rank | None = None


class Relation(Closed):
    id: Id
    from_: TaskRef = Field(alias="from")
    to: TaskRef
    kind: Literal["parent", "dependency", "include"]
    label: Label | None = None
    origin: Literal["observed", "manual"]
    provenance: Sentence
    since: UTCDateTime | None = None
    rank: Rank | None = None


class Hub(Closed):
    scope: Literal["explicit"]
    members: list[TaskRef] = Field(max_length=2000)


class HistoryState(Closed):
    first: UTCDateTime
    last: UTCDateTime
    end: UTCDateTime | None = None
    outcome: Literal["completed", "merged", "dropped", "split", "unknown"]
    active: bool
    pinned: bool
    future: bool
    commitCount: Count | None = None


def unique_ids(items: list[Any]) -> None:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate record id")


class TaskDisplayV1(Closed):
    version: Literal[1]
    identity: TaskIdentity | None = None
    badges: list[Badge] = Field(default_factory=list, max_length=8)
    rows: list[DetailRow] = Field(default_factory=list, max_length=16)
    links: list[Link] = Field(default_factory=list, max_length=8)
    current: Sentence | None = None
    need: Sentence | None = None
    next: Sentence | None = None
    state: (
        Literal["done", "ready", "running", "user", "failed", "blocked", "queued", "held"] | None
    ) = None
    lastActivity: UTCDateTime | None = None
    actors: list[Actor] = Field(default_factory=list, max_length=32)
    sessions: list[Session] = Field(default_factory=list, max_length=500)
    intervals: list[Interval] = Field(default_factory=list, max_length=200)
    nodeSelection: Literal["sessions-first", "intervals"] | None = None
    checks: list[Check] = Field(default_factory=list, max_length=32)
    quotes: list[Quote] = Field(default_factory=list, max_length=1000)
    commits: list[Commit] = Field(default_factory=list, max_length=1000)
    events: list[Event] = Field(default_factory=list, max_length=5000)
    relations: list[Relation] = Field(default_factory=list, max_length=200)
    hub: Hub | None = None
    historyState: HistoryState | None = None
    omitted: dict[
        Literal[
            "badges",
            "rows",
            "links",
            "actors",
            "sessions",
            "intervals",
            "checks",
            "quotes",
            "commits",
            "events",
            "relations",
            "members",
        ],
        Count,
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_records(self) -> TaskDisplayV1:
        for items in (
            self.badges,
            self.rows,
            self.links,
            self.actors,
            self.sessions,
            self.intervals,
            self.checks,
            self.quotes,
            self.commits,
            self.events,
            self.relations,
        ):
            unique_ids(items)
        if self.hub:
            refs = [(ref.source, ref.uid) for ref in self.hub.members]
            if len(refs) != len(set(refs)):
                raise ValueError("Duplicate hub member")
        return self


class SourceIdentity(Closed):
    label: Label
    badge: Label | None = None
    rank: Rank | None = None


class GroupIdentity(SourceIdentity):
    id: SourceId


class Future(Closed):
    id: Id
    label: Label
    at: UTCDateTime
    sourceLabel: Label
    link: Link | None = None


class ClosedDay(Closed):
    date: Date
    count: Count


class Account(Closed):
    ref: AccountRef
    label: Label
    authoritySource: SourceId
    timeZone: Zone


class AccountDay(Closed):
    id: Id
    account: AccountRef
    date: Date
    amount: Number
    kind: Literal["estimate"]
    sourceLabel: Label


class Release(Closed):
    id: Id
    name: Label
    publishedAt: UTCDateTime
    source: SourceId
    link: Link | None = None


class Snapshot(Closed):
    id: Id
    baseId: Id | None
    operation: Literal["begin", "append", "publish"]
    chunkIndex: Count | None = None
    chunkCount: Count
    expectedTaskCount: Count
    contentHash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    coverageFrom: Date
    coverageTo: Date
    removedUids: list[Uid] = Field(default_factory=list, max_length=10000)
    sourceGeneration: Id | None = None
    abandons: Id | None = None


class SourceDisplayV1(Closed):
    version: Literal[1]
    asOf: UTCDateTime
    timeZone: Zone
    snapshot: Snapshot
    identity: SourceIdentity | None = None
    groups: list[GroupIdentity] = Field(default_factory=list, max_length=1000)
    checkHosts: list[Label] = Field(default_factory=list, max_length=2)
    future: list[Future] = Field(default_factory=list, max_length=1000)
    closedDays: list[ClosedDay] = Field(default_factory=list, max_length=36600)
    accounts: list[Account] = Field(default_factory=list, max_length=1000)
    accountDays: list[AccountDay] = Field(default_factory=list, max_length=36600)
    releases: list[Release] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_records(self) -> SourceDisplayV1:
        for items in (self.groups, self.future, self.releases):
            unique_ids(items)
        accounts = [(a.ref.namespace, a.ref.id) for a in self.accounts]
        days = [(d.account.namespace, d.account.id, d.date) for d in self.accountDays]
        dates = [day.date for day in self.closedDays]
        if (
            len(accounts) != len(set(accounts))
            or len(days) != len(set(days))
            or len(dates) != len(set(dates))
        ):
            raise ValueError("Duplicate account or date")
        declared = set(accounts)
        if any((d.account.namespace, d.account.id) not in declared for d in self.accountDays):
            raise ValueError("Account day requires a declared account")
        return self


def validate_displays(tasks: list[dict[str, Any]], group: dict[str, Any] | None) -> None:
    key = "omnigent.ai:sourceDisplay"
    if group is not None and key in group:
        _validate(SourceDisplayV1, group[key], key)
    entries = group["entries"] if group is not None else tasks
    for i, task in enumerate(entries):
        if group is not None and task.get("@type", "Task") != "Task":
            continue
        if key not in task:
            continue
        prefix = f"entries[{i}].{key}" if group is not None else key
        _validate(TaskDisplayV1, task[key], prefix)
        state = task[key].get("state")
        progress = task_progress(task)
        waiting = task.get("omnigent.ai:waiting")
        expected = {
            "completed": "done",
            "cancelled": "done",
            "failed": "failed",
            "in-process": "running",
        }.get(progress)
        if expected is None and waiting:
            expected = "user" if waiting["on"] == "user" else "blocked"
        if state is not None and (
            state != expected if expected else state not in {"ready", "held", "queued"}
        ):
            raise ReportError("bad_value", prefix + ".state", "State conflicts with task progress")
