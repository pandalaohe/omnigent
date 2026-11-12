"""Bounded source snapshots and CloudEvents source notifications."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

Account = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
Source = Annotated[str, Field(min_length=1, max_length=128)]
Severity = Literal["error", "warn", "info", "recover"]
SourceState = Literal[
    "ok",
    "auth_failed",
    "credential_missing",
    "not_configured",
    "format_changed",
    "unreachable",
    "paused",
    "never_read",
    "session_expired",
    "access_denied",
    "expiring",
    "unknown",
]
Timestamp = Annotated[StrictInt, Field(ge=0, le=253402300799)]


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: Account | None = None
    source: Source
    state: SourceState
    last_ok: Timestamp | None = None
    last_error_at: Timestamp | None = None
    observed_at: Timestamp | None = None
    fresh_until: Timestamp | None = None


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    sources: Annotated[list[dict[str, Any]], Field(max_length=128)] = []


class SourceEvent(BaseModel):
    # CloudEvents permits extensions beyond the severity this consumer understands.
    model_config = ConfigDict(extra="ignore")

    specversion: Literal["1.0"]
    id: Source
    source: Source
    type: Source
    time: str
    subject: Account | None = None
    datacontenttype: Literal["application/json"] | None = None
    data: dict[str, Any] | None = None
    severity: Severity = "info"

    @field_validator("subject", "datacontenttype", "data", mode="before")
    @classmethod
    def non_null_attribute(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit absent attributes")
        return value

    @field_validator("source")
    @classmethod
    def uri_reference(cls, value: str) -> str:
        if re.search(r"[^A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]", value) or re.search(
            r"%(?![0-9A-Fa-f]{2})", value
        ):
            raise ValueError("source must be a URI-reference")
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            and not value.startswith(("/", "?", "#"))
            and ":" in value.split("/", 1)[0]
        ):
            raise ValueError("relative URI first segment cannot contain a colon")
        return value

    @field_validator("time")
    @classmethod
    def rfc3339(cls, value: str) -> str:
        if not re.fullmatch(
            r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)", value
        ):
            raise ValueError("time must be RFC 3339")
        datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        return value

    @property
    def time_utc(self) -> int:
        return int(datetime.fromisoformat(self.time.upper().replace("Z", "+00:00")).timestamp())


class Rejected(BaseModel):
    index: int
    reason: str


def _depth(value: Any, level: int = 0) -> int:
    if level > 16:
        raise ValueError("data nesting exceeds limit")
    if isinstance(value, dict):
        return max([level, *(_depth(child, level + 1) for child in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(child, level + 1) for child in value)])
    return level


def _invalid_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def parse_cloudevents(body: bytes, media_type: str) -> list[SourceEvent | Rejected]:
    """Reject envelopes atomically; retain indexed per-event validation failures."""
    if len(body) > 1024 * 1024:
        raise ValueError("body exceeds 1 MiB")
    payload = json.loads(body, parse_constant=_invalid_constant)
    if media_type == "application/cloudevents+json" and isinstance(payload, dict):
        entries = [payload]
    elif media_type == "application/cloudevents-batch+json" and isinstance(payload, list):
        entries = payload
    else:
        raise ValueError("expected structured or batch CloudEvents")
    if len(entries) > 500:
        raise ValueError("batch exceeds 500 events")
    result: list[SourceEvent | Rejected] = []
    for index, entry in enumerate(entries):
        try:
            event = SourceEvent.model_validate(entry)
            if event.data is not None:
                serialized = json.dumps(event.data, ensure_ascii=False, allow_nan=False).encode()
                if len(serialized) > 4096:
                    result.append(Rejected(index=index, reason="data_too_large"))
                    continue
                _depth(event.data)
            if isinstance(entry, dict) and "data_base64" in entry:
                raise ValueError("only JSON data is accepted")
            result.append(event)
        except (ValidationError, ValueError, OverflowError):
            result.append(Rejected(index=index, reason="invalid_attribute"))
    return result
