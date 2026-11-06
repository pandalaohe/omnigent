"""Owner-scoped quota snapshots and durable usage readings."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from omnigent.codex_rate_limits import fresh_codex_rate_limits
from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import Conversation
from omnigent.quota_pace import (
    AgentQuota,
    QuotaTiers,
    Window,
    accept_snapshot,
    agent_quota,
    claude_windows,
    codex_windows,
    quota_line,
    rate_limit_quota,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostRegistry
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.activity import MarkSettings, attributed_rows, error_mark, summarize, totals
from omnigent.usage.contract import (
    UsageAccountView,
    UsageHostView,
    UsageOverview,
    UsagePeriodView,
    UsageReport,
    UsageReportV2,
    UsageWindow,
    UsageWindowView,
    WriteOutcome,
    next_mark,
    parse_usage_report,
    period_of,
    window_start,
)
from omnigent.usage.ranges import RangeKey, range_spans

_logger = logging.getLogger(__name__)
_DELETE = object()


@dataclass(frozen=True)
class QuotaContext:
    owner_id: str
    host_id: str | None


class UsageAccountLimitError(ValueError):
    """The owner already has the contract's 32 reported accounts."""


def _fresh(snapshot: dict[str, Any], now: float) -> bool:
    if not accept_snapshot(snapshot.get("captured_at"), now=now):
        return False
    fresh_until = snapshot.get("fresh_until")
    return fresh_until is None or (
        isinstance(fresh_until, (int, float))
        and not isinstance(fresh_until, bool)
        and now <= fresh_until
    )


def _safe_id(value: object, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or fallback)).strip("-")
    return (text or fallback)[:32]


def _minutes(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _native_windows(snapshot: dict[str, Any], product: str) -> list[UsageWindow]:
    result: list[UsageWindow] = []
    if product == "codex" and isinstance(snapshot.get("limits"), list):
        for bucket in snapshot["limits"]:
            if not isinstance(bucket, dict):
                continue
            limit_id = bucket.get("limit_id", "codex")
            for index, raw in enumerate(bucket.get("windows") or []):
                if not isinstance(raw, dict):
                    continue
                minutes = _minutes(raw.get("window_duration_mins"))
                if minutes is None:
                    continue
                kind = raw.get("kind")
                result.append(
                    UsageWindow(
                        id=_safe_id(f"{limit_id}-{kind or index}", f"window-{index}"),
                        kind=(
                            "session"
                            if kind == "primary"
                            else "weekly"
                            if kind == "secondary"
                            else "other"
                        ),
                        label=f"{minutes / 60:g}h" if minutes % 1440 else f"{minutes / 1440:g}d",
                        percent_used=raw.get("used_percent"),
                        window_minutes=int(minutes),
                        resets_at=raw.get("resets_at"),
                    )
                )
        return result
    for index, raw in enumerate(snapshot.get("windows") or []):
        if not isinstance(raw, dict):
            continue
        label = raw.get("label")
        minutes = _minutes(raw.get("duration_mins"))
        if not isinstance(label, str):
            continue
        if minutes is None and all(
            raw.get(field) is None for field in ("used_value", "used_percent")
        ):
            continue
        result.append(
            UsageWindow(
                id=_safe_id(raw.get("id") or label, f"window-{index}"),
                kind=raw.get("kind")
                if raw.get("kind") in {"session", "weekly", "weekly_model", "monthly"}
                else "weekly"
                if label == "w"
                else "session"
                if label == "5h"
                else "other",
                label=raw.get("aria_label") or label,
                percent_used=raw.get("used_percent"),
                window_minutes=minutes,
                resets_at=raw.get("resets_at"),
                used_value=raw.get("used_value"),
                limit_value=raw.get("limit_value"),
                unit=raw.get("unit"),
            )
        )
    return result


class AgentQuotaService:
    """One server's advisory cache, isolated by workspace and owner."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        preferences_store: SqlAlchemyUserPreferencesStore | None,
        host_registry: HostRegistry | None,
        host_store: HostStore | None,
        snapshot_store: UsageSnapshotStore | None = None,
        *,
        usage_enabled: bool = True,
        hours_store: UsageHoursStore | None = None,
        mark_settings: MarkSettings | None = None,
        agent_store: AgentStore | None = None,
        agent_cache: AgentCache | None = None,
    ) -> None:
        self.conversations = conversation_store
        self.permissions = permission_store
        self.preferences = preferences_store
        self.host_registry = host_registry
        self.host_store = host_store
        self.snapshot_store = snapshot_store
        self.usage_enabled = usage_enabled
        self.hours_store = hours_store
        self.mark_settings = mark_settings or MarkSettings()
        self.agent_store = agent_store
        self.agent_cache = agent_cache
        self._snapshots: dict[tuple[int, str, str | None, str], dict[str, Any]] = {}
        self._accounts: dict[tuple[int, str, str], dict[str, Any]] = {}
        self._display: dict[tuple[int, str, str | None, str], dict[str, Any]] = {}
        self._hydrated: set[tuple[int, str]] = set()
        self._dirty: dict[tuple[int, str, str], Any] = {}
        self._last_written: dict[tuple[int, str, str], float] = {}
        self._persisted_windows: dict[tuple[int, str, str], str] = {}
        self._lock = threading.Lock()
        self._account_write_lock = threading.Lock()

    def context(self, conversation: Conversation, owner_hint: str | None = None) -> QuotaContext:
        from omnigent.server.routes.sessions.routes_peer import effective_owner_id

        host_id = conversation.host_id
        current = conversation
        seen = {current.id}
        for _ in range(64):
            parent_id = current.parent_conversation_id
            if parent_id is None or parent_id in seen:
                break
            seen.add(parent_id)
            parent = self.conversations.get_conversation(parent_id)
            if parent is None:
                break
            host_id = host_id or parent.host_id
            current = parent
        owner = (
            effective_owner_id(conversation, self.conversations, self.permissions)
            if self.permissions is not None
            else RESERVED_USER_LOCAL
        )
        return QuotaContext(owner or owner_hint or RESERVED_USER_LOCAL, host_id)

    def report(
        self,
        owner: str,
        host_id: str | None,
        product: str,
        snapshot: dict[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        accepted = self._store_snapshot(owner, host_id, product, snapshot, now=now)
        if accepted and self.usage_enabled:
            self._note_display(owner, host_id, product, snapshot, now=now, project=True)
        return accepted

    def _store_snapshot(
        self,
        owner: str,
        host_id: str | None,
        product: str,
        snapshot: dict[str, Any],
        *,
        now: float,
    ) -> bool:
        if not accept_snapshot(snapshot.get("captured_at"), now=now):
            return False
        key = (current_workspace_id(), owner, host_id, product)
        with self._lock:
            # Retain stale source identity so it remains unknown, not source-less.
            previous = self._snapshots.get(key)
            if previous is None or snapshot["captured_at"] >= previous["captured_at"]:
                self._snapshots[key] = snapshot
        return True

    def report_claude(self, conversation: Conversation, snapshot: dict[str, Any] | None) -> None:
        if snapshot is None or str(snapshot.get("provider", "")).lower() != "claude":
            return
        context = self.context(conversation)
        if not self.usage_enabled:
            legacy = [
                window
                for window in snapshot.get("windows", [])
                if isinstance(window, dict) and window.get("label") in {"5h", "w"}
            ]
            if not legacy and snapshot.get("windows") != []:
                return
            snapshot = {**snapshot, "windows": legacy}
        self.report(context.owner_id, context.host_id, "claude", snapshot)

    def report_pi(self, conversation: Conversation, snapshot: dict[str, Any] | None) -> None:
        """Keep a Pi provider's reported allowance separate from CLI subscriptions."""
        context = self.context(conversation)
        if snapshot is None:
            with self._lock:
                self._snapshots.pop(
                    (current_workspace_id(), context.owner_id, context.host_id, "pi"), None
                )
            return
        self.report(context.owner_id, context.host_id, "pi", snapshot)

    def note_reading(
        self,
        owner: str,
        host_id: str,
        product: str,
        snapshot: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        """Retain a native reading for display without extending routing after disconnect."""
        if not self.usage_enabled:
            return
        now = time.time() if now is None else now
        if accept_snapshot(snapshot.get("captured_at"), now=now):
            self._note_display(owner, host_id, product, snapshot, now=now, project=False)

    def _note_display(
        self,
        owner: str,
        host_id: str | None,
        product: str,
        snapshot: dict[str, Any],
        *,
        now: float,
        project: bool,
    ) -> None:
        key = (current_workspace_id(), owner, host_id, product)
        captured = float(snapshot["captured_at"])
        with self._lock:
            previous = self._display.get(key)
            if previous is not None and captured < float(previous.get("last_attempt_at") or 0):
                return
            windows = _native_windows(snapshot, product)
            if windows:
                marks = dict((previous or {}).get("reset_marks") or {})
                for window in windows:
                    span = window_start(window, now)
                    if span is not None:
                        start, length = span
                        marks[window.id] = next_mark(marks.get(window.id), start, length)
                record = {
                    "schema_version": 1,
                    "snapshot": deepcopy(snapshot),
                    "product": product,
                    "host_id": host_id,
                    "project_snapshot": project,
                    "read_at": captured,
                    "last_attempt_at": captured,
                    "last_attempt_status": "available",
                    "fresh_until": snapshot.get("fresh_until"),
                    "pinned_window": snapshot.get("pinned_period")
                    or (previous or {}).get("pinned_window"),
                    "reset_marks": marks,
                    "report_log": list((previous or {}).get("report_log") or []),
                }
            else:
                record = deepcopy(previous) or {
                    "schema_version": 1,
                    "snapshot": None,
                    "product": product,
                    "host_id": host_id,
                    "project_snapshot": project,
                    "read_at": None,
                    "fresh_until": None,
                    "pinned_window": None,
                    "reset_marks": {},
                    "report_log": [],
                }
                record["last_attempt_at"] = captured
                record["last_attempt_status"] = "unavailable"
            self._display[key] = record
            if project:
                record["routing_snapshot"] = deepcopy(snapshot)
                record["project_snapshot"] = True
            else:
                record["routing_snapshot"] = deepcopy((previous or {}).get("routing_snapshot"))
                record["project_snapshot"] = record["routing_snapshot"] is not None
            self._dirty[(key[0], owner, self._agent_key(product, host_id))] = record

    @staticmethod
    def _agent_key(product: str, host_id: str | None) -> str:
        return f"usage.agent.{product}.{host_id or 'global'}"

    @staticmethod
    def _account_key(account_id: str) -> str:
        return f"usage.account.{account_id}"

    def report_account(
        self,
        owner: str,
        account_id: str,
        report: UsageReport | UsageReportV2,
        *,
        now: float | None = None,
        event: dict[str, Any] | None = None,
    ) -> WriteOutcome | None:
        # Serialize source replacements without holding the native reading
        # cache's lock during database I/O.
        with self._account_write_lock:
            if isinstance(report, UsageReportV2):
                return self._report_account_v2(owner, account_id, report, now=now, event=event)
            self._report_account(owner, account_id, report, now=now)
            if event:
                key = (current_workspace_id(), owner, account_id)
                with self._lock:
                    record = self._accounts[key]
                    self._append_log(record, "cloudevent", float(report.observed_at), **event)
                    record["source"] = event["source"][:64]
                    record["source_at"] = report.observed_at
        return None

    def _report_account_v2(
        self,
        owner: str,
        account_id: str,
        report: UsageReportV2,
        *,
        now: float | None,
        event: dict[str, Any] | None,
    ) -> WriteOutcome:
        outcome = WriteOutcome()
        now = time.time() if now is None else now
        if not self.usage_enabled:
            return outcome
        self._hydrate(owner)
        key = (current_workspace_id(), owner, account_id)
        observed = float(report.observed_at)
        with self._lock:
            previous = deepcopy(self._accounts.get(key))
            if (
                previous is None
                and sum(1 for ws, user, _ in self._accounts if (ws, user) == key[:2]) >= 32
            ):
                raise UsageAccountLimitError("at most 32 usage accounts per owner")
        record = previous or {"schema_version": 1, "account_id": account_id, "report_log": []}
        record["last_report_at"] = max(
            observed, float(record.get("last_report_at") or record.get("last_attempt_at") or 0)
        )
        stored = dict(record.get("report") or {})
        stored.update(schema_version=2, observed_at=observed)
        fields = report.model_fields_set
        data = report.model_dump(mode="json")
        for name in fields - {
            "windows",
            "balances",
            "activity",
            "status",
            "observed_at",
            "fresh_until",
        }:
            stored[name] = data[name]
        record["product"] = stored.get("product") or record.get("product")
        if record["product"] is None:
            from omnigent.harness_plugins import native_agents

            if account_id in {agent.key for agent in native_agents()}:
                record["product"] = account_id
        record["host_id"] = stored.get("host_id") or record.get("host_id")
        source = report.source or (event or {}).get("source") or record.get("source")
        if source is not None:
            record["source"] = str(source)[:64]
            record["source_at"] = observed
            stored["source"] = record["source"]
        reading_sections = fields & {"windows", "balances"}
        if report.status == "unavailable" and not reading_sections:
            reading_sections = {"windows", "balances"}
        sections = dict(record.get("sections") or {})
        # Old accounts already have a windows reading, predating section metadata.
        sections.setdefault(
            "windows",
            {
                name: record.get(name)
                for name in ("read_at", "fresh_until", "last_attempt_at", "last_attempt_status")
            },
        )
        windows_changed = False
        for name in ("windows", "balances"):
            if name not in reading_sections:
                continue
            section = dict(sections.get(name) or {})
            if observed < float(section.get("last_attempt_at") or 0):
                outcome.older_sections.append(name)
                continue
            section.update(last_attempt_at=observed, last_attempt_status=report.status)
            if report.status == "available":
                stored[name] = data[name]
                section.update(read_at=observed, fresh_until=report.fresh_until)
            else:
                self._append_log(record, "read_failed", observed, section=name)
            sections[name] = section
            if name == "windows":
                windows_changed = True
                record.update(section)
        record["sections"] = sections
        stored.setdefault("windows", [])
        stored.setdefault("balances", [])
        if "pinned_window" in fields:
            record["pinned_window"] = report.pinned_window
        # A persisted pin may be temporarily absent from the latest window set.
        stored.pop("pinned_window", None)
        merged = UsageReportV2.model_validate(stored)
        if report.activity is not None and self.hours_store is not None:
            entries = [
                entry.model_copy(update={"cost_basis": "list"})
                if "cost_basis" not in entry.model_fields_set
                and merged.source_type == "subscription"
                else entry
                for entry in report.activity
            ]
            written = self.hours_store.replace_rows(owner, account_id, entries)
            outcome.applied = written.applied
            outcome.held = written.held
            outcome.skipped = written.skipped
            for entry in outcome.held:
                self._append_log(record, "lower_reread_held", observed, **entry.model_dump())
            for entry in entries:
                if entry.correction and not entry.removed:
                    self._append_log(
                        record, "correction", observed, hour=entry.hour, source=source
                    )
            record["has_activity"] = True
            record["activity_writer"] = "report"
        marks = dict(record.get("reset_marks") or {})
        if windows_changed and report.status == "available":
            for window in merged.windows:
                span = window_start(window, now)
                if span is not None:
                    marks[window.id] = next_mark(marks.get(window.id), *span)
        record["reset_marks"] = marks
        record["report"] = merged.stored()
        if event:
            self._append_log(record, "cloudevent", observed, **event)
        snapshot = None
        if windows_changed and record.get("product"):
            snapshot = {
                "provider": merged.display_name or account_id,
                "captured_at": observed,
                "fresh_until": report.fresh_until,
                "pinned_period": record.get("pinned_window"),
                "windows": [
                    {
                        "id": window.id,
                        "kind": window.kind,
                        "label": window.id,
                        "aria_label": window.label or window.id,
                        "used_percent": window.percent_used,
                        "duration_mins": window.window_minutes,
                        "resets_at": window.resets_at,
                        "used_value": window.used_value,
                        "limit_value": window.limit_value,
                        "unit": window.unit,
                    }
                    for window in merged.windows
                ]
                if report.status == "available"
                else [],
            }
            record["routing_snapshot"] = deepcopy(snapshot)
        elif "pinned_window" in fields and isinstance(record.get("routing_snapshot"), dict):
            snapshot = deepcopy(record["routing_snapshot"])
            snapshot["pinned_period"] = record.get("pinned_window")
            record["routing_snapshot"] = deepcopy(snapshot)
        with self._lock:
            self._accounts[key] = record
            self._dirty[(key[0], owner, self._account_key(account_id))] = record
        if snapshot is not None:
            self._store_snapshot(
                owner, record.get("host_id"), record["product"], snapshot, now=now
            )
        return outcome

    def _report_account(
        self,
        owner: str,
        account_id: str,
        report: UsageReport,
        *,
        now: float | None = None,
    ) -> None:
        if not self.usage_enabled:
            return
        now = time.time() if now is None else now
        self._hydrate(owner)
        from omnigent.harness_plugins import native_agents

        key = (current_workspace_id(), owner, account_id)
        observed = float(report.observed_at)
        snapshot: dict[str, Any] | None = None
        with self._lock:
            previous = self._accounts.get(key)
            if previous is not None and observed < float(
                previous.get("last_report_at") or previous.get("last_attempt_at") or 0
            ):
                return
            if (
                previous is None
                and sum(1 for ws, user, _ in self._accounts if (ws, user) == key[:2]) >= 32
            ):
                raise UsageAccountLimitError("at most 32 usage accounts per owner")
        if (
            report.status == "available"
            and report.activity is not None
            and self.hours_store is not None
        ):
            self.hours_store.replace_activity(owner, account_id, report.activity)
        with self._lock:
            previous = self._accounts.get(key)
            if previous is not None and observed < float(
                previous.get("last_report_at") or previous.get("last_attempt_at") or 0
            ):
                return
            if (
                previous is None
                and sum(1 for ws, user, _ in self._accounts if (ws, user) == key[:2]) >= 32
            ):
                raise UsageAccountLimitError("at most 32 usage accounts per owner")
            record = deepcopy(previous) or {
                "schema_version": 1,
                "account_id": account_id,
                "report": None,
                "product": None,
                "host_id": None,
                "read_at": None,
                "fresh_until": None,
                "pinned_window": None,
                "reset_marks": {},
                "report_log": [],
            }
            product = (
                report.product
                or (previous or {}).get("product")
                or (account_id if account_id in {agent.key for agent in native_agents()} else None)
            )
            host_id = (
                report.host_id if report.host_id is not None else (previous or {}).get("host_id")
            )
            record["product"] = product
            record["host_id"] = host_id
            record["last_attempt_at"] = observed
            record["last_report_at"] = observed
            record["last_attempt_status"] = report.status
            if report.status == "available":
                if report.activity is not None and self.hours_store is not None:
                    record["has_activity"] = True
                if report.pinned_window is not None:
                    record["pinned_window"] = report.pinned_window
                record["report"] = report.stored()
                record["read_at"] = observed
                record["fresh_until"] = report.fresh_until
                marks = dict(record.get("reset_marks") or {})
                for window in report.windows:
                    span = window_start(window, now)
                    if span is not None:
                        start, length = span
                        marks[window.id] = next_mark(marks.get(window.id), start, length)
                record["reset_marks"] = marks
            else:
                self._append_log(record, "read_failed", observed)
            record["sections"] = {
                "windows": {
                    name: record.get(name)
                    for name in (
                        "read_at",
                        "fresh_until",
                        "last_attempt_at",
                        "last_attempt_status",
                    )
                }
            }
            self._accounts[key] = record
            self._dirty[(key[0], owner, self._account_key(account_id))] = record
            if product is not None:
                windows = [] if report.status == "unavailable" else report.windows
                snapshot = {
                    "provider": report.display_name or account_id,
                    "captured_at": observed,
                    "fresh_until": (report.fresh_until if report.status == "available" else None),
                    "pinned_period": record.get("pinned_window"),
                    "windows": [
                        {
                            "id": window.id,
                            "kind": window.kind,
                            "label": window.id,
                            "aria_label": window.label or window.id,
                            "used_percent": window.percent_used,
                            "duration_mins": window.window_minutes,
                            "resets_at": window.resets_at,
                            "used_value": window.used_value,
                            "limit_value": window.limit_value,
                            "unit": window.unit,
                        }
                        for window in windows
                    ],
                }
                record["routing_snapshot"] = deepcopy(snapshot)
        if product is not None and snapshot is not None:
            self._store_snapshot(owner, host_id, product, snapshot, now=now)

    @staticmethod
    def _append_log(record: dict[str, Any], kind: str, at: float, **details: Any) -> None:
        entries = list(record.get("report_log") or [])
        if (
            not entries
            or entries[-1]["kind"] != kind
            or entries[-1]["at"] <= (record.get("read_at") or 0)
        ):
            _logger.warning("Usage source report: %s", kind)
        entries.append({"kind": kind, "at": at, **details})
        record["report_log"] = entries[-200:]

    def note_rejected(self, owner: str, account_id: str, *, now: float | None = None) -> None:
        if not self.usage_enabled:
            return
        now = time.time() if now is None else now
        self._hydrate(owner)
        key = (current_workspace_id(), owner, account_id)
        with self._lock:
            previous = self._accounts.get(key)
            if (
                previous is None
                and sum(1 for ws, user, _ in self._accounts if (ws, user) == key[:2]) >= 32
            ):
                raise UsageAccountLimitError("at most 32 usage accounts per owner")
            record = deepcopy(previous) or {
                "schema_version": 1,
                "account_id": account_id,
                "report": None,
                "product": None,
                "host_id": None,
                "read_at": None,
                "fresh_until": None,
                "pinned_window": None,
                "reset_marks": {},
                "report_log": [],
            }
            self._append_log(record, "rejected", now)
            self._accounts[key] = record
            self._dirty[(key[0], owner, self._account_key(account_id))] = record

    def delete_account(self, owner: str, account_id: str) -> bool:
        with self._account_write_lock:
            return self._delete_account(owner, account_id)

    def _delete_account(self, owner: str, account_id: str) -> bool:
        if not self.usage_enabled:
            return False
        self._hydrate(owner)
        key = (current_workspace_id(), owner, account_id)
        if self.hours_store is not None:
            self.hours_store.delete_account(owner, account_id)
        with self._lock:
            record = self._accounts.get(key)
            if record is None:
                return False
            self._accounts.pop(key)
            self._dirty[(key[0], owner, self._account_key(account_id))] = _DELETE
            product = record.get("product")
            if isinstance(product, str):
                self._rebuild_projection(key[0], owner, record.get("host_id"), product)
            return True

    def _rebuild_projection(
        self, workspace: int, owner: str, host_id: str | None, product: str
    ) -> None:
        candidates: list[dict[str, Any]] = []
        for key, record in self._accounts.items():
            if (
                key[:2] == (workspace, owner)
                and record.get("product") == product
                and record.get("host_id") == host_id
                and isinstance(record.get("routing_snapshot"), dict)
            ):
                candidates.append(record["routing_snapshot"])
        for key, record in self._display.items():
            if (
                key[:2] == (workspace, owner)
                and key[2] == host_id
                and key[3] == product
                and record.get("project_snapshot")
                and isinstance(record.get("routing_snapshot"), dict)
            ):
                candidates.append(record["routing_snapshot"])
        key = (workspace, owner, host_id, product)
        if candidates:
            self._snapshots[key] = deepcopy(
                max(candidates, key=lambda snapshot: snapshot["captured_at"])
            )
        else:
            self._snapshots.pop(key, None)

    def quotas(
        self, context: QuotaContext, *, now: float | None = None
    ) -> tuple[QuotaTiers, dict[str, AgentQuota]]:
        return self._quotas(context, now=now)

    def _quotas(
        self,
        context: QuotaContext,
        *,
        now: float | None = None,
        all_hosts: bool = False,
    ) -> tuple[QuotaTiers, dict[str, AgentQuota]]:
        now = time.time() if now is None else now
        self._hydrate(context.owner_id)
        settings = read_collab_settings(self.preferences, context.owner_id)
        tiers = QuotaTiers(
            settings.quota_tight_below_pct,
            settings.quota_low_below_pct,
            settings.quota_floor_pct,
        )
        from omnigent.harness_plugins import native_agents

        workspace = current_workspace_id()
        with self._lock:
            candidates: dict[str, list[dict[str, Any]]] = {}
            if all_hosts:
                for key, snapshot in self._snapshots.items():
                    if key[:2] == (workspace, context.owner_id):
                        candidates.setdefault(key[3], []).append(snapshot)
            else:
                for agent in native_agents():
                    for host in {None, context.host_id}:
                        snapshot = self._snapshots.get(
                            (workspace, context.owner_id, host, agent.key)
                        )
                        if snapshot is not None:
                            candidates.setdefault(agent.key, []).append(snapshot)
        if self.host_registry is not None and all_hosts:
            for live_host in self.host_registry.online_host_ids(workspace_id=workspace):
                conn = self.host_registry.get(live_host, workspace_id=workspace)
                if conn is not None and (
                    self.permissions is None or conn.owner == context.owner_id
                ):
                    native = conn.hello.codex_rate_limits
                    if native is not None:
                        candidates.setdefault("codex", []).append(native)
        elif context.host_id and self.host_registry is not None:
            conn = self.host_registry.get(context.host_id)
            # The live tunnel already records its authenticated owner.
            if conn is not None and (self.permissions is None or conn.owner == context.owner_id):
                native = conn.hello.codex_rate_limits
                if native is not None:
                    candidates.setdefault("codex", []).append(native)
        result: dict[str, AgentQuota] = {}
        for product, snapshots in candidates.items():
            fresh = [snapshot for snapshot in snapshots if _fresh(snapshot, now)]
            newest = max(fresh or snapshots, key=lambda snapshot: snapshot["captured_at"])
            if product == "pi" and newest.get("scope") == "API rate limits":
                result[product] = rate_limit_quota(newest, now=now)
                continue
            windows = (
                codex_windows(fresh_codex_rate_limits(newest, now=now), now=now)
                if product == "codex" and "limits" in newest
                else claude_windows(
                    newest,
                    now=now,
                    normalize_claude_labels=str(newest.get("provider", "")).lower() == "claude",
                )
            )
            period = (
                settings.quota_periods.get(product)
                or settings.quota_period_default
                or newest.get("pinned_period")
            )
            quota = agent_quota(windows, tiers, now=now, deciding_period=period)
            if not fresh:
                quota = AgentQuota("unknown", None, None, (), reason="stale_report")
            elif newest.get("windows") == []:
                quota = replace(quota, reason="empty_report")
            result[product] = quota
        return tiers, result

    def payload(
        self,
        conversation: Conversation,
        *,
        requester_id: str | None = None,
        agent_store: AgentStore | None = None,
        agent_cache: AgentCache | None = None,
    ) -> dict[str, Any]:
        from omnigent.harness_plugins import native_agents

        context = self.context(conversation)
        tiers, quotas = self.quotas(context)
        # Missing reports are explicit at the API boundary; instruction lines
        # still enumerate only products with an actual source in this scope.
        missing = AgentQuota("unknown", None, None, (), reason="no_report")
        from omnigent.runtime.instruction_layers import agent_key_for_harness
        from omnigent.server.routes._sessions.helpers import _resolve_harness_impl
        from omnigent.server.session_choice import active_on, ordered_links, pool_of

        choice = read_collab_settings(self.preferences, context.owner_id).session_choice
        pools = []
        for link in ordered_links(choice):
            probe = Conversation(
                id="", created_at=0, updated_at=0, root_conversation_id="", agent_id=link.agent_id
            )
            product = agent_key_for_harness(
                _resolve_harness_impl(probe, agent_store=agent_store, agent_cache=agent_cache)
            )
            pools.append(pool_of(link, product))
        payload: dict[str, Any] = {
            "tiers": asdict(tiers),
            "agents": {
                agent.key: asdict(quotas.get(agent.key, missing)) for agent in native_agents()
            },
            "session_choice": {
                "enabled": active_on(choice, context.host_id or ""),
                "work_types": [work.id for work in choice.work_types],
                "pools": list(dict.fromkeys(pools)),
            },
        }
        if self.usage_enabled and requester_id == context.owner_id:
            try:
                issues = self.usage_issues(context.owner_id)
                if issues:
                    payload["usage_issues"] = issues
            except Exception:
                _logger.warning("Could not read usage issues", exc_info=True)
        return payload

    def overview(
        self, owner: str, *, now: float | None = None, include_marks: bool = True
    ) -> UsageOverview:
        now = time.time() if now is None else now
        tiers, quotas = self._quotas(QuotaContext(owner, None), now=now, all_hosts=True)
        settings = read_collab_settings(self.preferences, owner)
        workspace = current_workspace_id()
        with self._lock:
            account_records = [
                (key[2], deepcopy(record))
                for key, record in self._accounts.items()
                if key[:2] == (workspace, owner)
            ]
            display_records = [
                (key[2], key[3], deepcopy(record))
                for key, record in self._display.items()
                if key[:2] == (workspace, owner)
            ]
        linked = {
            record.get("product")
            for _, record in account_records
            if isinstance(record.get("product"), str)
        }
        native_products = {"claude", "codex", *(product for _, product, _ in display_records)}
        accounts: list[UsageAccountView] = []
        for product in sorted(native_products - linked):
            records = [item for item in display_records if item[1] == product]
            accounts.append(self._native_account(product, records, tiers, settings, now))
        for account_id, record in sorted(account_records):
            accounts.append(
                self._reported_account(
                    account_id,
                    record,
                    [item for item in display_records if item[1] == record.get("product")],
                    tiers,
                    settings,
                    now,
                )
            )
        from omnigent.harness_plugins import native_agents

        missing = AgentQuota("unknown", None, None, (), reason="no_report")
        if include_marks and self.usage_enabled and self.hours_store is not None:
            try:
                projected = self.activity(owner, range_key="h24", now=now)
                marks = {item["account"]: item["error_mark"] for item in projected["accounts"]}
                for account in accounts:
                    account.mark = marks.get(account.id)
            except Exception:
                _logger.warning("Could not read usage error marks", exc_info=True)
        return UsageOverview(
            generated_at=now,
            tiers=asdict(tiers),
            accounts=accounts,
            agents={
                agent.key: asdict(quotas.get(agent.key, missing)) for agent in native_agents()
            },
        )

    def _activity_meters(self, owner: str) -> dict[tuple[str, str], set[str | None]]:
        from omnigent.runtime.instruction_layers import agent_key_for_harness
        from omnigent.server.routes._sessions.helpers import (
            _resolve_harness_impl,
            _resolve_llm_model,
        )
        from omnigent.server.session_choice import ordered_links

        settings = read_collab_settings(self.preferences, owner).session_choice
        meters: dict[tuple[str, str], set[str | None]] = defaultdict(set)
        for link in ordered_links(settings):
            probe = Conversation(
                id="", created_at=0, updated_at=0, root_conversation_id="", agent_id=link.agent_id
            )
            product = agent_key_for_harness(
                _resolve_harness_impl(
                    probe, agent_store=self.agent_store, agent_cache=self.agent_cache
                )
            )
            model = link.model or _resolve_llm_model(
                probe, agent_store=self.agent_store, agent_cache=self.agent_cache
            )
            if product is not None:
                meters[(product, model or "")].add(link.meter)
        return dict(meters)

    def activity(
        self,
        owner: str,
        *,
        range_key: RangeKey = "d7",
        tz: str = "UTC",
        from_date: date | None = None,
        to_date: date | None = None,
        account: str | None = None,
        include_models: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        zone = ZoneInfo(tz)
        overview = self.overview(owner, now=now, include_marks=False)
        views = {item.id: item for item in overview.accounts}
        workspace = current_workspace_id()
        with self._lock:
            records = {
                key[2]: deepcopy(record)
                for key, record in self._accounts.items()
                if key[:2] == (workspace, owner)
            }
            display_records = [
                (key[2], key[3], deepcopy(record))
                for key, record in self._display.items()
                if key[:2] == (workspace, owner)
            ]
        # Native tiles may be hidden by a linked reading while still owning
        # unclaimed model rows. Keep that remainder available to the board.
        from omnigent.harness_plugins import native_agents

        settings = read_collab_settings(self.preferences, owner)
        tiers = QuotaTiers(
            tight_below_pct=cast(float, overview.tiers["tight_below_pct"]),
            low_below_pct=cast(float, overview.tiers["low_below_pct"]),
            floor_pct=overview.tiers.get("floor_pct"),
        )
        for native in native_agents():
            if native.key not in views:
                views[native.key] = self._native_account(
                    native.key,
                    [item for item in display_records if item[1] == native.key],
                    tiers,
                    settings,
                    now,
                )
        spans = {
            key: range_spans(
                range_key, now, zone, from_date=from_date, to_date=to_date, period=view.period
            )
            for key, view in views.items()
        }
        since = min(
            [int((now - 86400) // 3600) * 3600, *(prev.counted_from for _, prev in spans.values())]
        )
        rows = self.hours_store.read(owner, since, int(now) + 1) if self.hours_store else []
        projected = attributed_rows(rows, records, self._activity_meters(owner), list(views))
        result = []
        for key, view in views.items():
            if account is not None and key != account:
                continue
            if key not in {item.id for item in overview.accounts} and key not in projected:
                continue
            current, previous = spans[key]
            own_rows = projected.get(key, [])
            amount = summarize(own_rows, current, zone)
            before = summarize(own_rows, previous, zone)
            selected = [
                row for row in own_rows if current.counted_from <= row.hour_utc < current.until
            ]
            mark = error_mark(own_rows, now, view.freshness == "fresh", self.mark_settings)
            item = {
                "account": key,
                **amount,
                "error_mark": mark,
                "previous": before if before["series"] else None,
            }
            if account is not None or include_models:
                item["models"] = [
                    {"model": model, **totals([row for row in selected if row.model == model])}
                    for model in sorted({row.model for row in selected})
                ]
                item["hosts"] = [
                    {"host_id": host, **totals([row for row in selected if row.host_id == host])}
                    for host in sorted({row.host_id for row in selected})
                ]
            result.append(item)
        return {"generated_at": now, "range": range_key, "tz": tz, "accounts": result}

    def issues(
        self, owner: str, *, now: float | None = None, **range_options: Any
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        activity = self.activity(owner, now=now, **range_options)
        failures = []
        unpriced = []
        sources = []
        workspace = current_workspace_id()
        with self._lock:
            records = [
                (key[2], deepcopy(record))
                for key, record in self._accounts.items()
                if key[:2] == (workspace, owner)
            ]
        amounts = {item["account"]: item for item in activity["accounts"]}
        for account, item in amounts.items():
            value = item["totals"]
            if value["failed"]:
                failures.append({"account": account, **value})
            for model in item.get("models", []):
                if model["unpriced"]:
                    unpriced.append({"account": account, **model})
        for account, record in records:
            item = amounts.get(account)
            if item is None:
                continue
            span = item["span"]
            for entry in record.get("report_log") or []:
                if span["counted_from"] <= entry["at"] < span["until"]:
                    sources.append({"account": account, **entry})
            read_at = record.get("read_at")
            if (
                read_at is not None
                and now - read_at > 3600
                and (span["until"] >= now or span["counted_from"] <= read_at < span["until"])
            ):
                sources.append({"account": account, "kind": "silent", "at": read_at})
        return {"failures": failures, "unpriced_models": unpriced, "source_reports": sources}

    def usage_issues(self, owner: str, *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        recent = self.activity(owner, range_key="h24", now=now)
        issues = []
        for item in recent["accounts"]:
            mark = item["error_mark"]
            if mark is not None:
                issues.append(
                    {
                        "account": item["account"],
                        "level": mark["level"],
                        "what": "failures",
                        "last_at": item["totals"]["last_at"],
                    }
                )
            if item["totals"]["unpriced"]:
                issues.append(
                    {
                        "account": item["account"],
                        "level": "notice",
                        "what": "unpriced",
                        "last_at": item["totals"]["last_at"],
                    }
                )
        with self._lock:
            for (workspace, user, account), record in self._accounts.items():
                if (workspace, user) != (current_workspace_id(), owner):
                    continue
                last_at = record.get("read_at")
                latest = (record.get("report_log") or [None])[-1]
                if latest and latest["at"] > (last_at or 0):
                    issues.append(
                        {
                            "account": account,
                            "level": "notice",
                            "what": latest["kind"],
                            "last_at": latest["at"],
                        }
                    )
                if last_at is not None and now - last_at > 3600:
                    issues.append(
                        {
                            "account": account,
                            "level": "notice",
                            "what": "silent",
                            "last_at": last_at,
                        }
                    )
        return issues

    def pool_readings(
        self, context: QuotaContext, *, now: float | None = None
    ) -> dict[str, UsageAccountView]:
        """Read the same tiles as the board, including reset data for selection."""
        now = time.time() if now is None else now
        if self.usage_enabled:
            return {tile.id: tile for tile in self.overview(context.owner_id, now=now).accounts}
        tiers, quotas = self._quotas(context, now=now, all_hosts=context.host_id is None)
        settings = read_collab_settings(self.preferences, context.owner_id)
        workspace = current_workspace_id()
        with self._lock:
            snapshots = {
                product: max(
                    (
                        record
                        for (ws, owner, host, item), record in self._snapshots.items()
                        if ws == workspace
                        and owner == context.owner_id
                        and (context.host_id is None or host in {None, context.host_id})
                        and item == product
                    ),
                    key=lambda record: record["captured_at"],
                    default=None,
                )
                for product in quotas
            }
        result = {}
        for product, snapshot in snapshots.items():
            if snapshot is not None:
                record = {
                    "read_at": snapshot["captured_at"],
                    "fresh_until": snapshot.get("fresh_until"),
                    "last_attempt_status": "available",
                    "pinned_window": snapshot.get("pinned_period"),
                }
                result[product] = self._account_view(
                    account_id=product,
                    kind="native",
                    record=record,
                    windows=_native_windows(snapshot, product),
                    tiers=tiers,
                    settings=settings,
                    now=now,
                    product=product,
                    display_name=product,
                )
        return result

    def _native_account(
        self,
        product: str,
        records: list[tuple[str | None, str, dict[str, Any]]],
        tiers: QuotaTiers,
        settings: Any,
        now: float,
    ) -> UsageAccountView:
        newest = max(records, key=lambda item: item[2].get("read_at") or 0, default=None)
        record = (
            newest[2]
            if newest is not None
            else {
                "snapshot": None,
                "read_at": None,
                "last_attempt_status": None,
                "reset_marks": {},
            }
        )
        snapshot = record.get("snapshot") or {}
        windows = _native_windows(snapshot, product) if snapshot else []
        view = self._account_view(
            account_id=product,
            kind="native",
            record=record,
            windows=windows,
            tiers=tiers,
            settings=settings,
            now=now,
            product=product,
            display_name=snapshot.get("display_name") or product,
        )
        view.hosts = [
            UsageHostView(
                host_id=host_id or "",
                name=self._host_name(host_id),
                read_at=item.get("read_at"),
                windows=self._window_views(
                    _native_windows(item.get("snapshot") or {}, product),
                    self._freshness(item, now) == "fresh",
                    now,
                    tiers,
                    None,
                )[0],
            )
            for host_id, _, item in sorted(records, key=lambda entry: entry[0] or "")
        ]
        return view

    def _reported_account(
        self,
        account_id: str,
        record: dict[str, Any],
        native_records: list[tuple[str | None, str, dict[str, Any]]],
        tiers: QuotaTiers,
        settings: Any,
        now: float,
    ) -> UsageAccountView:
        raw = record.get("report")
        report = parse_usage_report(raw) if raw is not None else None
        latest_native = max(
            native_records, key=lambda item: item[2].get("read_at") or 0, default=None
        )
        base_record = record
        if report is not None and (
            latest_native is None
            or (record.get("read_at") or 0) >= (latest_native[2].get("read_at") or 0)
        ):
            windows = report.windows
        elif latest_native is not None:
            base_record = latest_native[2]
            windows = _native_windows(
                latest_native[2].get("snapshot") or {}, str(record["product"])
            )
        else:
            windows = []
        view = self._account_view(
            account_id=account_id,
            kind="account",
            record=base_record,
            windows=windows,
            tiers=tiers,
            settings=settings,
            now=now,
            product=record.get("product"),
            display_name=(report.display_name if report else None) or account_id,
        )
        if report is not None:
            view.plan = report.plan
            view.band = report.band
            view.details_url = str(report.details_url) if report.details_url else None
            view.renews_at = report.renews_at
        if isinstance(report, UsageReportV2):
            view.balances = report.balances
            section = (record.get("sections") or {}).get("balances") or {}
            view.balance_read_at = section.get("read_at")
            view.balance_freshness = self._freshness(section, now)
            view.source_type = report.source_type
        view.source = record.get("source")
        view.source_at = record.get("source_at")
        host_id = record.get("host_id")
        host_readings = {
            native_host: (
                native_record,
                _native_windows(native_record.get("snapshot") or {}, str(record["product"])),
            )
            for native_host, _, native_record in native_records
        }
        if report is not None and isinstance(host_id, str):
            native_record = host_readings.get(host_id, ({}, []))[0]
            if (record.get("read_at") or 0) >= (native_record.get("read_at") or 0):
                host_readings[host_id] = (record, report.windows)
        view.hosts = [
            UsageHostView(
                host_id=native_host or "",
                name=self._host_name(native_host),
                read_at=native_record.get("read_at"),
                windows=self._window_views(
                    windows,
                    self._freshness(native_record, now) == "fresh",
                    now,
                    tiers,
                    None,
                )[0],
            )
            for native_host, (native_record, windows) in sorted(
                host_readings.items(),
                key=lambda entry: entry[0] or "",
            )
        ]
        return view

    def _account_view(
        self,
        *,
        account_id: str,
        kind: Literal["native", "account"],
        record: dict[str, Any],
        windows: list[UsageWindow],
        tiers: QuotaTiers,
        settings: Any,
        now: float,
        product: str | None,
        display_name: str,
    ) -> UsageAccountView:
        freshness = self._freshness(record, now)
        fresh = freshness == "fresh"
        product = product if isinstance(product, str) else None
        deciding = (
            settings.quota_periods.get(product)
            or settings.quota_period_default
            or record.get("pinned_window")
            if product
            else record.get("pinned_window")
        )
        window_views, quota = self._window_views(windows, fresh, now, tiers, deciding)
        status = record.get("last_attempt_status")
        reason = None
        state = quota.state if fresh else "unknown"
        deciding_window = quota.deciding_window if fresh else None
        pace_pct = quota.pace_pct if fresh else None
        if record.get("read_at") is None and status is None:
            reason = "no_report"
        elif status == "unavailable":
            reason = "empty_report"
        elif not fresh:
            reason = "stale_report"
        elif not windows:
            state = "unknown"
            reason = "empty_report"
        period = period_of(windows, now) if record.get("read_at") is not None else None
        period_view = None
        if period is not None:
            start = period.start
            moved_from = None
            mark = (record.get("reset_marks") or {}).get(period.window_id)
            selected = next((window for window in windows if window.id == period.window_id), None)
            if mark is not None:
                moved_from = mark.get("moved_from")
                if selected is not None and float(selected.resets_at or 0) > now:
                    start = float(mark["start"])
            period_view = UsagePeriodView(start=start, days=period.days, moved_from=moved_from)
        read_at = record.get("read_at")
        return UsageAccountView(
            id=account_id,
            kind=kind,
            display_name=display_name,
            product=product,
            state=state,
            reason=reason,
            deciding_window=deciding_window,
            pace_pct=pace_pct,
            read_at=read_at,
            age_s=max(0.0, now - read_at) if read_at is not None else None,
            freshness=freshness,
            period=period_view,
            windows=window_views,
            models=(record.get("report") or {}).get("models") or [],
        )

    @staticmethod
    def _freshness(record: dict[str, Any], now: float) -> Literal["fresh", "old", "none"]:
        read_at = record.get("read_at")
        if read_at is None:
            return "none"
        if record.get("last_attempt_status") != "available":
            return "old"
        return (
            "fresh"
            if _fresh({"captured_at": read_at, "fresh_until": record.get("fresh_until")}, now)
            else "old"
        )

    @staticmethod
    def _window_views(
        windows: list[UsageWindow],
        fresh: bool,
        now: float,
        tiers: QuotaTiers,
        deciding: str | None,
    ) -> tuple[list[UsageWindowView], AgentQuota]:
        captured = now
        quota_windows = [
            Window(
                window.id,
                window.percent_used,
                float(window.window_minutes or 0) * 60,
                window.resets_at,
                captured,
            )
            for window in windows
        ]
        quota = agent_quota(quota_windows, tiers, now, deciding_period=deciding)
        by_id = {window.label: window for window in quota.windows}
        views: list[UsageWindowView] = []
        for window in windows:
            reset = float(window.resets_at) if window.resets_at is not None else None
            past_reset = reset is not None and reset <= now
            pace = by_id.get(window.id) if fresh and not past_reset else None
            percent = None if past_reset else window.percent_used
            remaining = 100 - percent if percent is not None else None
            time_left = max(0.0, reset - now) if reset is not None and not past_reset else None
            ahead = None
            if pace is not None and remaining is not None and window.window_minutes and time_left:
                ahead = remaining - 100 * time_left / (window.window_minutes * 60)
            state = (
                "past_reset"
                if past_reset
                else pace.state
                if pace
                else "stale"
                if not fresh
                else "unknown"
            )
            views.append(
                UsageWindowView(
                    id=window.id,
                    kind=window.kind,
                    label=window.label,
                    group=window.group,
                    percent_used=percent,
                    remaining_pct=remaining,
                    window_minutes=window.window_minutes,
                    resets_at=reset,
                    time_left_s=time_left,
                    pace_pct=pace.pace_pct if pace else None,
                    state=state,
                    ahead_pts=ahead,
                    used_value=None if past_reset else window.used_value,
                    limit_value=None if past_reset else window.limit_value,
                    unit=window.unit,
                    note=window.note,
                    past_reset=past_reset,
                )
            )
        return views, quota

    def _host_name(self, host_id: str | None) -> str:
        if host_id is None:
            return "Global"
        if self.host_store is not None:
            try:
                host = self.host_store.get_host(host_id)
                if host is not None:
                    return host.name
            except Exception:
                _logger.warning("Could not read usage host %s", host_id, exc_info=True)
        return host_id

    @staticmethod
    def _merge_hydrated_record(
        current: dict[str, Any] | None,
        stored: dict[str, Any],
        good_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        if current is None:
            return stored
        merged = deepcopy(current)
        if float(stored.get("last_attempt_at") or 0) >= float(current.get("last_attempt_at") or 0):
            for field in (
                "last_attempt_at",
                "last_attempt_status",
                "routing_snapshot",
                "project_snapshot",
                "product",
                "host_id",
                "report_log",
                "has_activity",
            ):
                if field in stored:
                    merged[field] = deepcopy(stored[field])
        if stored.get(good_fields[0]) is not None and (
            current.get(good_fields[0]) is None
            or float(stored.get("read_at") or 0) >= float(current.get("read_at") or 0)
        ):
            for field in good_fields:
                merged[field] = deepcopy(stored.get(field))
        return merged

    def _hydrate(self, owner: str) -> None:
        if not self.usage_enabled or self.snapshot_store is None:
            return
        scope = (current_workspace_id(), owner)
        with self._lock:
            if scope in self._hydrated:
                return
            self._hydrated.add(scope)
        try:
            rows = self.snapshot_store.read(owner)
        except Exception:
            _logger.warning("Could not hydrate usage readings for %s", owner, exc_info=True)
            return
        with self._lock:
            for storage_key, record in rows.items():
                if record.get("schema_version") != 1:
                    continue
                dirty_key = (scope[0], owner, storage_key)
                self._persisted_windows[dirty_key] = self._window_signature(record)
                if storage_key.startswith("usage.account."):
                    account_id = record.get("account_id") or storage_key.removeprefix(
                        "usage.account."
                    )
                    key = (scope[0], owner, account_id)
                    self._accounts[key] = self._merge_hydrated_record(
                        self._accounts.get(key),
                        record,
                        ("report", "read_at", "fresh_until", "pinned_window", "reset_marks"),
                    )
                    continue
                if not storage_key.startswith("usage.agent."):
                    continue
                product = record.get("product")
                if not isinstance(product, str):
                    continue
                host_id = record.get("host_id")
                key = (scope[0], owner, host_id, product)
                self._display[key] = self._merge_hydrated_record(
                    self._display.get(key),
                    record,
                    ("snapshot", "read_at", "fresh_until", "pinned_window", "reset_marks"),
                )
                if dirty_key in self._dirty:
                    self._dirty[dirty_key] = self._display[key]
            slots = {
                (key[2], key[3])
                for key, record in self._display.items()
                if key[:2] == scope and isinstance(record.get("routing_snapshot"), dict)
            }
            slots.update(
                (record.get("host_id"), record["product"])
                for key, record in self._accounts.items()
                if key[:2] == scope and isinstance(record.get("product"), str)
            )
            for host_id, product in slots:
                self._rebuild_projection(scope[0], owner, host_id, product)

    @staticmethod
    def _window_signature(record: dict[str, Any]) -> str:
        report = record.get("report")
        snapshot = record.get("snapshot")
        windows = (
            report.get("windows")
            if isinstance(report, dict)
            else snapshot.get("windows", snapshot.get("limits"))
            if isinstance(snapshot, dict)
            else None
        )
        return json.dumps(windows, sort_keys=True, separators=(",", ":"), default=str)

    def flush(self, *, now: float | None = None, force: bool = False) -> int:
        """Write due dirty rows; a failure stays dirty and never reaches the status path."""
        if not self.usage_enabled or self.snapshot_store is None:
            return 0
        now = time.time() if now is None else now
        with self._lock:
            pending = list(self._dirty.items())
        written = 0
        for (workspace, owner, storage_key), record in pending:
            dirty_key = (workspace, owner, storage_key)
            signature = "" if record is _DELETE else self._window_signature(record)
            if (
                not force
                and record is not _DELETE
                and dirty_key in self._last_written
                and signature == self._persisted_windows.get(dirty_key)
                and now - self._last_written[dirty_key] < 300
            ):
                continue
            try:
                with workspace_scope(workspace):
                    if record is _DELETE:
                        self.snapshot_store.delete(owner, storage_key)
                    else:
                        self.snapshot_store.write(
                            owner, storage_key, cast("dict[str, Any]", record)
                        )
            except Exception:
                _logger.warning("Could not persist usage reading %s", storage_key, exc_info=True)
                continue
            with self._lock:
                self._persisted_windows[dirty_key] = signature
                self._last_written[dirty_key] = now
                if self._dirty.get(dirty_key) is record:
                    self._dirty.pop(dirty_key, None)
                    if record is _DELETE:
                        self._persisted_windows.pop(dirty_key, None)
                        self._last_written.pop(dirty_key, None)
            written += 1
        return written

    def context_line(self, context: QuotaContext) -> str:
        now = time.time()
        tiers, quotas = self.quotas(context, now=now)
        return quota_line(quotas, tiers, now=now) or ""

    def session_choice_line(self, conversation: Conversation) -> str:
        from omnigent.server.session_choice import (
            active_on,
            floor_off_active,
            generated_line,
            ordered_links,
        )

        text = ""
        try:
            context = self.context(conversation)
            settings = read_collab_settings(self.preferences, context.owner_id).session_choice
            if not active_on(settings, context.host_id or ""):
                return ""
            text = generated_line(settings)
            readings = self.pool_readings(context)
            pools = dict.fromkeys(link.meter for link in ordered_links(settings) if link.meter)
            now = time.time()
            floors = (
                self.preferences.choice_floor_off(context.owner_id) if self.preferences else {}
            )
            quota = []
            released = []
            use_up = []
            for pool in pools:
                reading = readings.get(pool)
                if (
                    reading is not None
                    and reading.freshness == "fresh"
                    and reading.state not in {"ok", "unknown"}
                ):
                    window = next(
                        (item for item in reading.windows if item.id == reading.deciding_window),
                        None,
                    )
                    remaining = (
                        f" {window.remaining_pct:g}%"
                        if window and window.remaining_pct is not None
                        else ""
                    )
                    detail = f"{reading.deciding_window or ''}{remaining}".strip()
                    quota.append(f"{pool} {reading.state} {detail}".strip())
                entry = floors.get(pool)
                if entry is not None and floor_off_active(entry, reading, now):
                    (use_up if entry.until == "manual" else released).append(pool)
            parts = [text]
            if quota:
                parts.append("Quota: " + "; ".join(quota) + ".")
            if use_up:
                parts.append("Use up: " + ", ".join(use_up) + ".")
            if released:
                parts.append("Floor off: " + ", ".join(released) + " until reset.")
            return " ".join(parts)
        except Exception:
            _logger.warning(
                "Session choice instructions unavailable for %s", conversation.id, exc_info=True
            )
            return text

    def line(self, conversation: Conversation, owner_hint: str | None = None) -> str:
        """An advisory-source failure must leave the user's instruction text intact."""
        try:
            return self.context_line(self.context(conversation, owner_hint))
        except Exception:
            _logger.warning("Quota snapshot unavailable for %s", conversation.id, exc_info=True)
            return ""
