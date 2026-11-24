"""Server-proxied per-request usage reads scoped to the authenticated owner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.server_config import UsageRequestSource
from omnigent.stores.usage_hours_store import _activity_cutoff_hour, hours_of

_logger = logging.getLogger(__name__)

REQUEST_SOURCE_TIMEOUT_S = 5.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ALLOWED_QUERY_KEYS = frozenset(
    {"hour", "tz", "account", "model", "host", "agent", "cursor", "limit"}
)
_UPSTREAM_422_ERRORS = frozenset({"invalid-cursor", "cursor-scope-mismatch", "invalid-query"})
_STATUS_VALUES = frozenset({"ok", "error", "cancelled"})


class UsageRequestsError(Exception):
    """Structured HTTP error returned by the per-request proxy."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class UsageRequestTokens(BaseModel):
    input: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None
    reasoning: int | None = None


class UsageRequestRow(BaseModel):
    """Allowlisted per-request row fields; prompt text is never carried."""

    id: str
    source: str | None = None
    at: str
    host: str | None = None
    agent: str | None = None
    account: str | None = None
    group: str | None = None
    model: str | None = None
    model_requested: str | None = None
    tokens: UsageRequestTokens = Field(default_factory=UsageRequestTokens)
    cost_usd: str | None = None
    cost_basis: str | None = None
    ttft_ms: int | None = None
    duration_ms: int | None = None
    tps: float | None = None
    status: str = "ok"
    http_status: int | None = None
    error_class: str | None = None
    fallback: bool | None = None


class UsageRequestSection(BaseModel):
    state: str
    reason: str | None = None


class UsageRequestsView(BaseModel):
    hour: int
    tz: str = "UTC"
    rows: list[UsageRequestRow] = Field(default_factory=list)
    next_cursor: str | None = None
    retained_from: int | None = None
    sections: dict[str, UsageRequestSection] | None = None


def _has_control(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


@dataclass(frozen=True)
class UsageRequestsFilter:
    hour: int
    tz: str = "UTC"
    account: tuple[str, ...] = ()
    model: tuple[str, ...] = ()
    host: tuple[str, ...] = ()
    agent: tuple[str, ...] = ()
    cursor: str | None = None
    limit: int = 8

    @classmethod
    def parse(cls, query: Any, now: float) -> UsageRequestsFilter:
        if set(query.keys()) - _ALLOWED_QUERY_KEYS:
            raise ValueError("Invalid usage request query")

        def one(name: str, default: str | None = None) -> str | None:
            values = query.getlist(name)
            if len(values) > 1:
                raise ValueError(f"Duplicate {name} parameter")
            return values[0] if values else default

        raw_hour = one("hour")
        if raw_hour is None or re.fullmatch(r"-?[0-9]{1,16}", raw_hour) is None:
            raise ValueError("hour must be a UTC hour start in Unix seconds")
        hour = int(raw_hour)
        current_hour = hours_of(int(now))
        cutoff_hour = _activity_cutoff_hour(int(now))
        if hour % 3600 != 0 or hour > current_hour or hour < cutoff_hour:
            raise ValueError("hour is out of range")

        tz = one("tz", "UTC")
        assert tz is not None
        ZoneInfo(tz)

        raw_limit = one("limit", "8")
        if raw_limit is None or re.fullmatch(r"[0-9]{1,2}", raw_limit) is None:
            raise ValueError("limit must be an integer from 1 to 50")
        limit = int(raw_limit)
        if not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")

        cursor = one("cursor")
        if cursor is not None and (
            not cursor
            or len(cursor.encode("utf-8")) > 512
            or re.fullmatch(r"[\x21-\x7e]+", cursor) is None
        ):
            raise ValueError("Invalid cursor")

        selected: dict[str, tuple[str, ...]] = {}
        for name, max_length in (("account", 64), ("model", 128), ("host", 64), ("agent", 32)):
            values = query.getlist(name)
            if len(values) > 64:
                raise ValueError(f"Too many {name} values")
            for value in values:
                if len(value) > max_length or _has_control(value):
                    raise ValueError(f"Invalid {name} value")
                if name == "account" and not value:
                    raise ValueError("Invalid account value")
                if (
                    name == "agent"
                    and value
                    and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", value) is None
                ):
                    raise ValueError("Invalid agent value")
            selected[name] = tuple(sorted(set(values)))

        return cls(
            hour=hour,
            tz=tz,
            account=selected["account"],
            model=selected["model"],
            host=selected["host"],
            agent=selected["agent"],
            cursor=cursor,
            limit=limit,
        )


def resolve_owner_accounts(
    service: AgentQuotaService, owner: str, requested: tuple[str, ...], now: float
) -> tuple[str, ...]:
    """Resolve the caller's allowed upstream account scope from server state."""
    overview = service.overview(owner, now=now, include_marks=False)
    relations = service.pool_store.read_map(owner) if service.pool_store is not None else {}
    non_usage = {account_id for account_id, rel in relations.items() if rel.channel != "usage"}
    tile_ids = {account.id for account in overview.accounts}
    reported_ids = {
        account.id
        for account in overview.accounts
        if account.kind != "native" and account.id not in non_usage
    }
    owned_ids = tile_ids | set(relations)

    if requested:
        if any(account_id not in owned_ids for account_id in requested):
            raise UsageRequestsError(403, "Usage account is outside your scope")
        resolved: set[str] = set()
        for account_id in requested:
            if account_id in reported_ids:
                resolved.add(account_id)
            for child_id, relation in relations.items():
                if (
                    relation.channel == "usage"
                    and relation.parent_account_id == account_id
                    and child_id in reported_ids
                ):
                    resolved.add(child_id)
        return tuple(sorted(resolved))
    return tuple(sorted(reported_ids))


def _resolve_auth_header(source: UsageRequestSource) -> dict[str, str]:
    token: str | None = None
    if source.credential_env is not None:
        token = os.environ.get(source.credential_env, "").strip()
        if not token or "\r" in token or "\n" in token:
            _logger.warning(
                "usage.request_source credential_env %s is unset or invalid",
                source.credential_env,
            )
            raise UsageRequestsError(503, "Usage request credential is unavailable")
    elif source.credential_file is not None:
        try:
            token = Path(source.credential_file).read_text(encoding="utf-8").strip()
        except OSError:
            _logger.warning("usage.request_source credential_file could not be read")
            raise UsageRequestsError(503, "Usage request credential is unavailable") from None
        if not token or "\r" in token or "\n" in token:
            _logger.warning("usage.request_source credential_file is empty or invalid")
            raise UsageRequestsError(503, "Usage request credential is unavailable")
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


def _nonneg_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _optional_str(value: Any, *, max_length: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length or _has_control(value):
        return None
    return value


def _cost_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = repr(value)
    elif isinstance(value, str) and len(value) <= 64:
        raw = value
    else:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return str(amount) if amount.is_finite() else None


def _project_row(raw: Any) -> UsageRequestRow | None:
    if not isinstance(raw, Mapping):
        return None
    row_id = _optional_str(raw.get("id"), max_length=128)
    at = _optional_str(raw.get("at"), max_length=64)
    if row_id is None or at is None:
        return None
    raw_tokens = raw.get("tokens")
    tokens_map = raw_tokens if isinstance(raw_tokens, Mapping) else {}
    tokens = UsageRequestTokens(
        input=_nonneg_int(tokens_map.get("input")),
        cache_read=_nonneg_int(tokens_map.get("cache_read")),
        cache_write=_nonneg_int(tokens_map.get("cache_write")),
        output=_nonneg_int(tokens_map.get("output")),
        reasoning=_nonneg_int(tokens_map.get("reasoning")),
    )
    raw_status = raw.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in _STATUS_VALUES else "ok"
    raw_http = raw.get("http_status")
    http_status = raw_http if type(raw_http) is int and 100 <= raw_http <= 599 else None
    raw_tps = raw.get("tps")
    tps = (
        float(raw_tps)
        if isinstance(raw_tps, (int, float)) and not isinstance(raw_tps, bool) and raw_tps >= 0
        else None
    )
    raw_fallback = raw.get("fallback")
    fallback = raw_fallback if isinstance(raw_fallback, bool) else None
    return UsageRequestRow(
        id=row_id,
        source=_optional_str(raw.get("source"), max_length=32),
        at=at,
        host=_optional_str(raw.get("host"), max_length=64),
        agent=_optional_str(raw.get("agent"), max_length=64),
        account=_optional_str(raw.get("account"), max_length=64),
        group=_optional_str(raw.get("group"), max_length=64),
        model=_optional_str(raw.get("model"), max_length=128),
        model_requested=_optional_str(raw.get("model_requested"), max_length=128),
        tokens=tokens,
        cost_usd=_cost_str(raw.get("cost_usd")),
        cost_basis=_optional_str(raw.get("cost_basis"), max_length=32),
        ttft_ms=_nonneg_int(raw.get("ttft_ms")),
        duration_ms=_nonneg_int(raw.get("duration_ms")),
        tps=tps,
        status=status,
        http_status=http_status,
        error_class=_optional_str(raw.get("error_class"), max_length=32),
        fallback=fallback,
    )


def _project_sections(raw: Any) -> dict[str, UsageRequestSection] | None:
    if not isinstance(raw, Mapping):
        return None
    projected: dict[str, UsageRequestSection] = {}
    for key in ("litellm", "relay"):
        entry = raw.get(key)
        if isinstance(entry, Mapping):
            state = _optional_str(entry.get("state"), max_length=32)
            if state is not None:
                projected[key] = UsageRequestSection(
                    state=state,
                    reason=_optional_str(entry.get("reason"), max_length=64),
                )
    return projected or None


def _project_page(payload: Any, filters: UsageRequestsFilter) -> UsageRequestsView:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("rows"), list):
        raise UsageRequestsError(502, "Upstream request source returned an invalid response")
    rows: list[UsageRequestRow] = []
    for item in payload["rows"][: filters.limit]:
        projected = _project_row(item)
        if projected is None:
            raise UsageRequestsError(502, "Upstream request source returned an invalid response")
        rows.append(projected)
    raw_cursor = payload.get("next_cursor")
    next_cursor: str | None = None
    if raw_cursor is not None:
        if (
            not isinstance(raw_cursor, str)
            or not raw_cursor
            or len(raw_cursor.encode("utf-8")) > 512
            or re.fullmatch(r"[\x21-\x7e]+", raw_cursor) is None
        ):
            raise UsageRequestsError(502, "Upstream request source returned an invalid cursor")
        next_cursor = raw_cursor
    raw_retained = payload.get("retained_from")
    retained_from = raw_retained if type(raw_retained) is int else None
    return UsageRequestsView(
        hour=filters.hour,
        tz=filters.tz,
        rows=rows,
        next_cursor=next_cursor,
        retained_from=retained_from,
        sections=_project_sections(payload.get("sections")),
    )


async def fetch_usage_requests(
    service: AgentQuotaService,
    owner: str,
    query: Any,
    *,
    request_source: UsageRequestSource | None,
    now: float,
    timeout_s: float | None = None,
) -> UsageRequestsView:
    """Validate, scope, proxy and project one page of per-request usage rows."""
    effective_timeout = REQUEST_SOURCE_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        filters = UsageRequestsFilter.parse(query, now)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise UsageRequestsError(422, str(exc)) from None

    upstream_accounts = await asyncio.to_thread(
        resolve_owner_accounts, service, owner, filters.account, now
    )
    if request_source is None:
        raise UsageRequestsError(404, "not_configured")
    if not upstream_accounts:
        return UsageRequestsView(hour=filters.hour, tz=filters.tz, rows=[], next_cursor=None)

    headers = {"Accept": "application/json", **_resolve_auth_header(request_source)}
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("hour", str(filters.hour)),
        ("limit", str(filters.limit)),
    ]
    for account_id in upstream_accounts:
        params.append(("account", account_id))
    for host in filters.host:
        params.append(("host", host))
    for agent in filters.agent:
        params.append(("agent", agent))
    for model in filters.model:
        params.append(("model", model))
    if filters.cursor is not None:
        params.append(("cursor", filters.cursor))

    url = f"{request_source.base_url}/api/v2/responses"
    data = bytearray()
    try:
        async with (
            asyncio.timeout(effective_timeout),
            httpx.AsyncClient(
                timeout=effective_timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream("GET", url, params=params, headers=headers) as response,
        ):
            status_code = response.status_code
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise UsageRequestsError(502, "Upstream request response exceeded size limit")
                data.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        raise UsageRequestsError(504, "request_source_timeout") from None
    except httpx.HTTPError:
        raise UsageRequestsError(502, "Upstream request source could not be reached") from None

    body: Any = None
    if data:
        try:
            body = json.loads(data)
        except ValueError:
            body = None

    if status_code == 200:
        return _project_page(body, filters)
    upstream_error = body.get("error") if isinstance(body, Mapping) else None
    if status_code == 404 and upstream_error == "unknown-account":
        return UsageRequestsView(hour=filters.hour, tz=filters.tz, rows=[], next_cursor=None)
    if status_code == 422:
        code = upstream_error if upstream_error in _UPSTREAM_422_ERRORS else "invalid-query"
        raise UsageRequestsError(422, str(code))
    if status_code == 503:
        raise UsageRequestsError(503, "Upstream request source is unavailable")
    raise UsageRequestsError(502, "Upstream request source returned an unexpected status")
