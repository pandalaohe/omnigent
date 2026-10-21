"""Owner and host scoped quota snapshots; pace is computed only when read."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from omnigent.codex_rate_limits import fresh_codex_rate_limits
from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Conversation
from omnigent.quota_pace import (
    AgentQuota,
    QuotaTiers,
    accept_snapshot,
    agent_quota,
    claude_windows,
    codex_windows,
    quota_line,
    rate_limit_quota,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostRegistry
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaContext:
    owner_id: str
    host_id: str | None


class AgentQuotaService:
    """One server's advisory cache, isolated by workspace, owner, host and product."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        preferences_store: SqlAlchemyUserPreferencesStore | None,
        host_registry: HostRegistry | None,
        host_store: HostStore | None,
    ) -> None:
        self.conversations = conversation_store
        self.permissions = permission_store
        self.preferences = preferences_store
        self.host_registry = host_registry
        self.host_store = host_store
        self._snapshots: dict[tuple[int, str, str | None, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

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

    def quotas(
        self, context: QuotaContext, *, now: float | None = None
    ) -> tuple[QuotaTiers, dict[str, AgentQuota]]:
        now = time.time() if now is None else now
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
            for agent in native_agents():
                for host in {None, context.host_id}:
                    snapshot = self._snapshots.get((workspace, context.owner_id, host, agent.key))
                    if snapshot is not None:
                        candidates.setdefault(agent.key, []).append(snapshot)
        if context.host_id and self.host_registry is not None:
            conn = self.host_registry.get(context.host_id)
            # The live tunnel already records its authenticated owner.
            if conn is not None and (self.permissions is None or conn.owner == context.owner_id):
                native = conn.hello.codex_rate_limits
                if native is not None:
                    candidates.setdefault("codex", []).append(native)
        result: dict[str, AgentQuota] = {}
        for product, snapshots in candidates.items():
            fresh = [s for s in snapshots if accept_snapshot(s.get("captured_at"), now=now)]
            newest = max(fresh or snapshots, key=lambda s: s["captured_at"])
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
            period = settings.quota_periods.get(product) or newest.get("pinned_period")
            result[product] = agent_quota(windows, tiers, now=now, deciding_period=period)
        return tiers, result

    def payload(self, conversation: Conversation) -> dict[str, Any]:
        tiers, quotas = self.quotas(self.context(conversation))
        return {"tiers": asdict(tiers), "agents": {k: asdict(v) for k, v in quotas.items()}}

    def context_line(self, context: QuotaContext) -> str:
        now = time.time()
        tiers, quotas = self.quotas(context, now=now)
        return quota_line(quotas, tiers, now=now) or ""

    def line(self, conversation: Conversation, owner_hint: str | None = None) -> str:
        """An advisory-source failure must leave the user's instruction text intact."""
        try:
            return self.context_line(self.context(conversation, owner_hint))
        except Exception:  # noqa: BLE001
            _logger.warning("Quota snapshot unavailable for %s", conversation.id, exc_info=True)
            return ""
