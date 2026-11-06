"""OTLP usage decoding and emitter-specific, request-local normalization.

Only usage fields survive normalization. Delivery is at least once across a
restart; successfully written point/span identities are retained for two hours.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from google.protobuf.json_format import MessageToDict, ParseDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import ResourceMetrics

from omnigent.db.db_models import current_workspace_id
from omnigent.stores.usage_hours_store import UsageBuckets

if TYPE_CHECKING:
    from omnigent.server.agent_quota import AgentQuotaService

Signal = Literal["metrics", "traces", "logs"]
Export = ExportMetricsServiceRequest | ExportTraceServiceRequest
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_DELTA_HINT = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta"
_GENAI_PREFIX = "gen_ai.client.inference.usage."


@dataclass(frozen=True)
class Emitter:
    account: str
    token_metric: str
    token_attribute: str
    buckets: dict[str, str]
    cost_metric: str = ""
    cost_scale: float = 1.0
    inclusive_input: bool = True
    basis: str = "list"
    priced: bool = False


_EMITTERS = {
    "claude-code": Emitter(
        "claude-code",
        "claude_code.token.usage",
        "type",
        {
            "input": "input",
            "output": "output",
            "cacheRead": "cache_read",
            "cacheCreation": "cache_write",
        },
        "claude_code.cost.usage",
        inclusive_input=False,
        priced=True,
    ),
    "codex": Emitter(
        "codex-cli",
        "codex.turn.token_usage",
        "token_type",
        {
            "input": "input",
            "cached_input": "cache_read",
            "output": "output",
            "reasoning_output": "reasoning",
        },
        "codex.turn.cost_microusd",
        cost_scale=1e-6,
    ),
    "gemini-cli": Emitter(
        "gemini-cli",
        "gemini_cli.token.usage",
        "type",
        {
            "input": "input",
            "cache": "cache_read",
            "output": "output",
            "thought": "reasoning",
            "tool": "tool",
        },
    ),
    "litellm": Emitter("litellm", "", "", {}, basis="metered", priced=True),
}
_GENERIC_BUCKETS = {
    "input_tokens": "input",
    "cache_read.input_tokens": "cache_read",
    "cache_write.input_tokens": "cache_write",
    "output_tokens": "output",
    "reasoning.output_tokens": "reasoning",
}
_GENERIC = Emitter(
    "", "gen_ai.client.token.usage", "gen_ai.token.type", {"input": "input", "output": "output"}
)


def decode_export(body: bytes, media: str, signal: Signal) -> Export:
    """Decode an OTLP request, including OTLP's hex (not base64) JSON ids."""
    message = ExportMetricsServiceRequest() if signal == "metrics" else ExportTraceServiceRequest()
    if media == "application/x-protobuf":
        message.ParseFromString(body)
        return message
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Expected an OTLP object")
    if signal == "traces":
        for resource in payload.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    for key, size in (("traceId", 16), ("spanId", 8), ("parentSpanId", 8)):
                        if span.get(key):
                            value = bytes.fromhex(span[key])
                            if len(value) != size:
                                raise ValueError("Invalid OTLP id")
                            span[key] = base64.b64encode(value).decode("ascii")
    ParseDict(payload, message, ignore_unknown_fields=True)
    return message


def export_response(
    signal: Signal, media: str, rejected: int = 0, reasons: set[str] | None = None
) -> bytes:
    """Serialize the signal's OTLP success/partial-success message."""
    response: Any = {
        "metrics": ExportMetricsServiceResponse,
        "traces": ExportTraceServiceResponse,
        "logs": ExportLogsServiceResponse,
    }[signal]()
    if rejected:
        field_name = "rejected_data_points" if signal == "metrics" else "rejected_spans"
        setattr(response.partial_success, field_name, rejected)
        response.partial_success.error_message = "; ".join(sorted(reasons or set()))
    if media == "application/json":
        return json.dumps(MessageToDict(response), separators=(",", ":")).encode()
    return response.SerializeToString()


def _attributes(values: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for item in values:
        kind = item.value.WhichOneof("value")
        if kind in {"string_value", "int_value", "double_value", "bool_value"}:
            attributes[item.key] = getattr(item.value, kind)
    return attributes


def _emitter(service: str) -> Emitter:
    return _EMITTERS.get("codex" if service.startswith("codex") else service, _GENERIC)


def _account(attributes: dict[str, Any], resource: dict[str, Any], emitter: Emitter) -> str:
    explicit = attributes.get("omnigent.usage.account", resource.get("omnigent.usage.account"))
    if isinstance(explicit, str) and _ACCOUNT.fullmatch(explicit):
        return explicit
    service = str(resource.get("service.name", ""))
    return (
        emitter.account
        or re.sub(r"[^A-Za-z0-9_-]", "-", service).strip("-_")[:32]
        or "otel-unattributed"
    )


def _number(value: Any, *, integer: bool = False) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("Invalid usage value")
    if integer:
        if value != int(value) or value > 2**63 - 1:
            raise ValueError("Invalid token count")
        return int(value)
    return value


def _span_tokens(attributes: dict[str, Any], name: str) -> int | None:
    value = attributes.get("gen_ai.usage." + name)
    return int(_number(value, integer=True)) if value is not None else None


@dataclass
class UsageDelta:
    account: str
    hour: int
    model: str
    host: str
    basis: str
    priced: bool
    buckets: UsageBuckets
    cost: float = 0.0
    requests: int | None = None
    failed: int | None = None
    identities: list[bytes] = field(default_factory=list)


@dataclass
class _Group:
    account: str
    model: str
    host: str
    stamp: int
    service: str
    emitter: Emitter
    auth: str
    values: dict[str, int | float] = field(
        default_factory=lambda: defaultdict[str, int | float](int)
    )
    identities: list[bytes] = field(default_factory=list)

    def delta(self) -> UsageDelta:
        values = self.values
        buckets = UsageBuckets()
        if any(key != "cost" for key in values):
            cache_read, cache_write = int(values["cache_read"]), int(values["cache_write"])
            uncached = int(values["input"])
            if self.emitter.inclusive_input:
                uncached = max(0, uncached - cache_read - cache_write)
            uncached += int(values["tool"])
            output = int(values["output"])
            if self.service == "gemini-cli":
                output += int(values["reasoning"])
            reasoning = (
                int(values["reasoning"])
                if self.service.startswith("codex")
                or self.service == "gemini-cli"
                or "reasoning" in values
                else None
            )
            buckets = UsageBuckets(
                uncached,
                cache_read,
                cache_write,
                output,
                uncached + cache_read + cache_write + output,
                reasoning,
            )
        codex = self.service.startswith("codex")
        api = self.auth in {"api", "api_key"}
        return UsageDelta(
            self.account,
            self.stamp // 10**9 // 3600 * 3600,
            self.model,
            self.host,
            "metered" if codex and api else self.emitter.basis,
            api if codex else self.emitter.priced,
            buckets,
            float(values.get("cost", 0)),
            identities=self.identities,
        )


@dataclass
class NormalizedExport:
    deltas: list[UsageDelta] = field(default_factory=list)
    rejected: int = 0
    reasons: set[str] = field(default_factory=set)
    unmapped: set[tuple[str, str]] = field(default_factory=set)
    rejected_accounts: set[str] = field(default_factory=set)

    def reject(self, count: int, reason: str, account: str | None = None) -> None:
        self.rejected += count
        self.reasons.add(reason)
        if account is not None:
            self.rejected_accounts.add(account)


def normalize_export(
    message: Export, seen: set[bytes], managed_sessions: set[str]
) -> NormalizedExport:
    """Build four-bucket deltas before any storage or retry-state mutation."""
    result = NormalizedExport()
    pending = set(seen)
    if isinstance(message, ExportTraceServiceRequest):
        _normalize_traces(message, pending, result)
        return result
    resources: dict[bytes, list[ResourceMetrics]] = defaultdict(list)
    for resource in message.resource_metrics:
        identity = b"".join(
            sorted(
                item.SerializeToString(deterministic=True) for item in resource.resource.attributes
            )
        )
        resources[identity].append(resource)
    for resource_identity, blocks in resources.items():
        resource_attributes = _attributes(blocks[0].resource.attributes)
        if resource_attributes.get("launch_mode") == "omni":
            continue
        service = str(resource_attributes.get("service.name", ""))
        emitter = _emitter(service)
        if service == "litellm":
            continue
        metrics = [
            metric for block in blocks for scope in block.scope_metrics for metric in scope.metrics
        ]
        has_counters = any(
            metric.name in {_GENAI_PREFIX + name for name in _GENERIC_BUCKETS}
            for metric in metrics
        )
        groups: dict[tuple[str, str, int, str], _Group] = {}
        for metric in metrics:
            if emitter is _GENERIC and metric.name.startswith(
                "gen_ai.client.inference.operation."
            ):
                continue
            kind = metric.WhichOneof("data")
            if kind not in {"sum", "histogram"}:
                continue
            data = getattr(metric, kind)
            for point in data.data_points:
                attributes = _attributes(point.attributes)
                account = _account(attributes, resource_attributes, emitter)
                if (
                    attributes.get("session.id", resource_attributes.get("session.id"))
                    in managed_sessions
                ):
                    continue
                component = None
                if metric.name == emitter.token_metric:
                    if emitter is _GENERIC and has_counters:
                        continue
                    component = emitter.buckets.get(
                        str(attributes.get(emitter.token_attribute, ""))
                    )
                    if component is None:
                        continue
                elif metric.name == emitter.cost_metric:
                    component = "cost"
                elif emitter is _GENERIC and metric.name.startswith(_GENAI_PREFIX):
                    component = _GENERIC_BUCKETS.get(metric.name.removeprefix(_GENAI_PREFIX))
                if component is None:
                    if "token" in metric.name.lower() or "cost" in metric.name.lower():
                        result.unmapped.add(
                            (account, re.sub(r"[^A-Za-z0-9_.-]", "_", metric.name)[:128])
                        )
                    continue
                if data.aggregation_temporality != 1:
                    result.reject(
                        1, "Cumulative or unspecified temporality; set " + _DELTA_HINT, account
                    )
                    continue
                identity = hashlib.sha256(
                    resource_identity
                    + metric.name.encode()
                    + str(point.time_unix_nano).encode()
                    + b"".join(
                        sorted(
                            item.SerializeToString(deterministic=True) for item in point.attributes
                        )
                    )
                ).digest()
                if identity in pending:
                    continue
                try:
                    if kind == "histogram":
                        if not point.HasField("sum"):
                            raise ValueError("Histogram usage requires sum")
                        value = point.sum
                    else:
                        value_kind = point.WhichOneof("value")
                        if value_kind is None:
                            raise ValueError("Usage point requires a value")
                        value = getattr(point, value_kind)
                    value = _number(value, integer=component != "cost")
                    model = str(
                        attributes.get(
                            "model",
                            attributes.get(
                                "gen_ai.response.model", attributes.get("gen_ai.request.model", "")
                            ),
                        )
                    )
                    host = "otel:" + str(resource_attributes.get("host.name", ""))
                    if point.time_unix_nano == 0 or len(model) > 128 or len(host) > 64:
                        raise ValueError("Invalid usage dimensions")
                except ValueError as exc:
                    result.reject(1, str(exc), account)
                    continue
                auth = str(attributes.get("auth_mode", ""))
                key = account, model, point.time_unix_nano, auth
                group = groups.setdefault(
                    key, _Group(account, model, host, point.time_unix_nano, service, emitter, auth)
                )
                group.values[component] += value * (
                    emitter.cost_scale if component == "cost" else 1
                )
                group.identities.append(identity)
                pending.add(identity)
        result.deltas.extend(group.delta() for group in groups.values())
    return result


def _normalize_traces(
    message: ExportTraceServiceRequest, pending: set[bytes], result: NormalizedExport
) -> None:
    for resource in message.resource_spans:
        resource_attributes = _attributes(resource.resource.attributes)
        if (
            resource_attributes.get("launch_mode") == "omni"
            or resource_attributes.get("service.name") != "litellm"
        ):
            continue
        for scope in resource.scope_spans:
            for span in scope.spans:
                attributes = _attributes(span.attributes)
                if not any(
                    key in attributes
                    for key in (
                        "gen_ai.usage.input_tokens",
                        "gen_ai.usage.output_tokens",
                        "gen_ai.usage.total_tokens",
                    )
                ):
                    continue
                identity = span.trace_id + span.span_id
                if identity in pending:
                    continue
                try:
                    if (
                        len(span.trace_id) != 16
                        or len(span.span_id) != 8
                        or not any(span.trace_id)
                        or not any(span.span_id)
                        or span.end_time_unix_nano == 0
                    ):
                        raise ValueError("Invalid span identity or time")

                    input_tokens, output_tokens = (
                        _span_tokens(attributes, "input_tokens"),
                        _span_tokens(attributes, "output_tokens"),
                    )
                    cache_read, cache_write = (
                        _span_tokens(attributes, "cache_read.input_tokens"),
                        _span_tokens(attributes, "cache_creation.input_tokens"),
                    )
                    split = cache_read is not None or cache_write is not None
                    uncached = (
                        max(0, input_tokens - (cache_read or 0) - (cache_write or 0))
                        if split and input_tokens is not None
                        else None
                    )
                    total = _span_tokens(attributes, "total_tokens")
                    if total is None and input_tokens is not None and output_tokens is not None:
                        total = input_tokens + output_tokens
                    cost = float(_number(attributes.get("gen_ai.cost.total_cost", 0)))
                    model = str(
                        attributes.get(
                            "gen_ai.response.model", attributes.get("gen_ai.request.model", "")
                        )
                    )
                    host = "otel:" + str(resource_attributes.get("host.name", ""))
                    if len(model) > 128 or len(host) > 64:
                        raise ValueError("Invalid usage dimensions")
                except ValueError as exc:
                    result.reject(
                        1,
                        str(exc),
                        _account(attributes, resource_attributes, _EMITTERS["litellm"]),
                    )
                    continue
                result.deltas.append(
                    UsageDelta(
                        _account(attributes, resource_attributes, _EMITTERS["litellm"]),
                        span.end_time_unix_nano // 10**9 // 3600 * 3600,
                        model,
                        host,
                        "metered",
                        True,
                        UsageBuckets(
                            uncached,
                            (cache_read or 0) if split else None,
                            (cache_write or 0) if split else None,
                            output_tokens,
                            total,
                        ),
                        cost,
                        1,
                        int(span.status.code == 2),
                        [identity],
                    )
                )
                pending.add(identity)


class OtlpReceiver:
    """Serialize retries and retain identities only after their group is written."""

    def __init__(self, service: AgentQuotaService) -> None:
        self.service = service
        self._lock = threading.Lock()
        self._seen: dict[tuple[int, str, bytes], float] = {}
        self._sessions: dict[tuple[int, str, str], tuple[float, bool]] = {}

    def receive(self, owner: str, message: Export) -> NormalizedExport:
        from omnigent.server.agent_quota import UsageAccountLimitError, UsageWriterConflictError

        with self._lock:
            now = time.monotonic()
            scope = current_workspace_id(), owner
            self._seen = {key: until for key, until in self._seen.items() if until > now}
            self._sessions = {
                key: value for key, value in self._sessions.items() if value[0] > now
            }
            managed: set[str] = set()
            if isinstance(message, ExportMetricsServiceRequest):
                ids: set[str] = set()
                for resource in message.resource_metrics:
                    attributes = _attributes(resource.resource.attributes)
                    if attributes.get("launch_mode") == "omni":
                        continue
                    resource_session = attributes.get("session.id")
                    if isinstance(resource_session, str) and resource_session:
                        ids.add(resource_session)
                    for metric in (
                        metric for scope in resource.scope_metrics for metric in scope.metrics
                    ):
                        kind = metric.WhichOneof("data")
                        if kind not in {"sum", "histogram"}:
                            continue
                        for point in getattr(metric, kind).data_points:
                            session_id = _attributes(point.attributes).get("session.id")
                            if isinstance(session_id, str) and session_id:
                                ids.add(session_id)
                missing = {session for session in ids if (*scope, session) not in self._sessions}
                matches = (
                    self.service.managed_external_sessions(owner, missing) if missing else set()
                )
                for session in missing:
                    match = session in matches
                    self._sessions[(*scope, session)] = now + (600 if match else 60), match
                managed = {session for session in ids if self._sessions[(*scope, session)][1]}
            result = normalize_export(
                message, {key[2] for key in self._seen if key[:2] == scope}, managed
            )
            for account, metric in result.unmapped:
                with suppress(UsageAccountLimitError):
                    self.service.note_otlp_issue(owner, account, "unmapped_metric", metric=metric)
            for delta in result.deltas:
                try:
                    self.service.add_otlp(owner, delta)
                except (UsageAccountLimitError, UsageWriterConflictError) as exc:
                    result.reject(len(delta.identities), str(exc), delta.account)
                    continue
                for identity in delta.identities:
                    self._seen[(*scope, identity)] = now + 7200
            for account in result.rejected_accounts:
                with suppress(UsageAccountLimitError):
                    self.service.note_otlp_issue(owner, account, "rejected")
            return result
