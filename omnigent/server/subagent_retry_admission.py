"""Read retry preconditions and reconcile stable execution receipts.

Preflight is advisory: dispatch must still claim the journal input and perform
the runner's final admission guard. A healthy snapshot never proves execution.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from omnigent.entities.subagent_retry import InboxOwnerFence, RetryChain, RetryInput
from omnigent.server.host_registry import HostRegistry
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.subagent_retry_store import SubagentRetryStore

_RECONCILIATION_TIMEOUT_S = 5


class RetryCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool = False
    reason: Literal[
        "ready",
        "host_offline",
        "child_retired",
        "superseded",
        "stale_owner",
        "chain_changed",
        "invalid_ancestry",
        "retry_state_unavailable",
        "server_unreachable",
    ]
    effective_host_id: str | None = None
    host_online: bool | None = None


class RetryInputResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal[
        "accepted", "pending", "closed", "rejected", "stale_owner", "not_found", "unavailable"
    ]
    receipt: RetryInput | None = None
    reason: str | None = None


class SubagentRetryAdmission:
    """Server-side reads share the journal's workspace and owner fences."""

    def __init__(
        self,
        journal: SubagentRetryStore,
        conversations: ConversationStore,
        *,
        host_store: HostStore | None,
        host_registry: HostRegistry | None,
    ) -> None:
        self.journal = journal
        self.conversations = conversations
        self.host_store = host_store
        self.host_registry = host_registry

    def _owns(self, chain: RetryChain, owner: InboxOwnerFence) -> bool:
        parent = self.conversations.get_conversation(owner.parent_id)
        return bool(
            chain.owner == owner
            and chain.parent_id == owner.parent_id
            and parent is not None
            and parent.runner_id == owner.runner_id
            and self.journal.get_owner(owner.parent_id) == owner
        )

    def _check(self, expected: RetryChain) -> RetryCheck:
        chain = self.journal.get_chain(expected.chain_id)
        if chain is None:
            return RetryCheck(reason="retry_state_unavailable")
        if not self._owns(chain, expected.owner):
            return RetryCheck(reason="stale_owner")
        control = self.journal.get_control(chain.child_id)
        if control is None:
            return RetryCheck(reason="retry_state_unavailable")
        if (control.turn_id, control.generation) != (
            expected.expected_turn_id,
            expected.expected_input_generation,
        ):
            return RetryCheck(reason="superseded")
        if (
            chain != expected
            or chain.retry_closed
            or chain.phase not in {"waiting", "submitting", "resolving"}
            or chain.latest_terminal.status != "failed"
            or chain.latest_terminal.turn_id != chain.expected_turn_id
        ):
            return RetryCheck(reason="chain_changed")

        seen: set[str] = set()
        session_id: str | None = chain.child_id
        effective_host_id: str | None = None
        while session_id is not None:
            if session_id in seen or len(seen) >= 256:
                return RetryCheck(reason="invalid_ancestry")
            seen.add(session_id)
            conv = self.conversations.get_conversation(session_id)
            if conv is None:
                return RetryCheck(reason="invalid_ancestry")
            if session_id == chain.child_id and conv.parent_conversation_id != chain.parent_id:
                return RetryCheck(reason="invalid_ancestry")
            if (
                conv.archived
                or conv.archive_close_requested_revision is not None
                or conv.archive_close_claimed
            ):
                return RetryCheck(reason="child_retired")
            if effective_host_id is None and conv.host_id is not None:
                effective_host_id = conv.host_id
            session_id = conv.parent_conversation_id

        if effective_host_id is None:
            return RetryCheck(ready=True, reason="ready", host_online=True)
        if self.host_store is not None:
            host = self.host_store.get_host(effective_host_id)
            online = host is not None and host.deleted_at is None and host_is_live(host)
        elif self.host_registry is not None:
            online = self.host_registry.get(effective_host_id) is not None
        else:
            return RetryCheck(
                reason="retry_state_unavailable", effective_host_id=effective_host_id
            )
        return RetryCheck(
            ready=online,
            reason="ready" if online else "host_offline",
            effective_host_id=effective_host_id,
            host_online=online,
        )

    async def check_before_retry(self, chain: RetryChain) -> RetryCheck:
        """Bound the entire ancestry/health read, including cross-replica storage."""
        try:
            async with asyncio.timeout(_RECONCILIATION_TIMEOUT_S):
                return await asyncio.to_thread(self._check, chain)
        except TimeoutError:
            return RetryCheck(reason="server_unreachable")
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            return RetryCheck(reason="retry_state_unavailable")

    def _resolve(
        self,
        input_id: str,
        owner: InboxOwnerFence,
        expected_version: int | None,
        reason: dict[str, Any],
    ) -> RetryInputResolution:
        receipt = self.journal.get_input(input_id)
        chain = self.journal.get_chain(receipt.chain_id) if receipt and receipt.chain_id else None
        if chain is None or receipt is None:
            return RetryInputResolution(state="not_found")
        if not self._owns(chain, owner):
            return RetryInputResolution(state="stale_owner", reason="stale_owner")
        if expected_version is not None and receipt.state in {"prepared", "dispatching"}:
            closed = self.journal.close_input(
                input_id, owner=owner, expected_version=expected_version, reason=reason
            )
            if closed is None:
                # Acceptance, closure, or an owner change may have won the CAS.
                current = self.journal.get_chain(chain.chain_id)
                if current is None or not self._owns(current, owner):
                    return RetryInputResolution(state="stale_owner", reason="stale_owner")
                receipt = self.journal.get_input(input_id)
                if receipt is None:
                    return RetryInputResolution(state="not_found")
            else:
                receipt = closed
        return RetryInputResolution(
            state="pending" if receipt.state in {"prepared", "dispatching"} else receipt.state,
            receipt=receipt,
        )

    async def lookup_or_close_input(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int | None = None,
        reason: dict[str, Any] | None = None,
    ) -> RetryInputResolution:
        """Reconcile one ID; a stale version never authorizes another dispatch."""
        try:
            async with asyncio.timeout(_RECONCILIATION_TIMEOUT_S):
                return await asyncio.to_thread(
                    self._resolve,
                    input_id,
                    owner,
                    expected_version,
                    reason if reason is not None else {"code": "acceptance_unknown"},
                )
        except TimeoutError:
            return RetryInputResolution(state="unavailable", reason="server_unreachable")
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            return RetryInputResolution(state="unavailable", reason="retry_state_unavailable")
