"""Runner-local registry for asynchronous sub-agent dispatches.

Tracks each ``sys_session_send`` dispatch from launch to terminal status,
delivers terminal results to the parent session inbox, wakes the parent,
recovers undrained results after a runner restart, and records the
child→parent mapping used to mirror child status onto the parent stream.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse

from omnigent.debug_logging import runner_primary_session_id
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.runner.policy_proxy import _ASK_GATE_DELIVERY_TIMEOUT
from omnigent.util.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.runner.native.interrupt import MarkSubagentTerminalAndWake

_logger = logging.getLogger("omnigent.runner.app")

_SUBAGENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped", "killed"})


# Bound how long a sub-agent dispatch can wait for a start acknowledgment.
# A timeout reports uncertain launch status, not proof that the process is dead.
_SUBAGENT_LAUNCH_TIMEOUT_S_ENV = "OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S"
_DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S = 180.0
# Interval for the background sweep in the runner entrypoint.
SUBAGENT_LAUNCH_REAP_INTERVAL_S = 30.0


def resolve_subagent_launch_timeout_s() -> float:
    """
    Resolve the sub-agent launch liveness budget in seconds.

    Values ``<= 0`` disable the reaper. A non-numeric override is rejected
    with a warning and falls back to the default.

    :returns: The budget in seconds, e.g. ``180.0``.
    """
    raw = os.environ.get(_SUBAGENT_LAUNCH_TIMEOUT_S_ENV, "").strip()
    if not raw:
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = None
    # Non-finite values (nan/inf) would silently disable reaping without the
    # explicit ``<= 0`` "disabled" intent — reject them like non-numeric input.
    if value is None or not math.isfinite(value):
        _logger.warning(
            "Invalid %s=%r; using default %ss",
            _SUBAGENT_LAUNCH_TIMEOUT_S_ENV,
            raw,
            _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S,
        )
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    return value


_SUBAGENT_DELIVERY_DELIVERED = "delivered"
_SUBAGENT_DELIVERY_ALREADY_DELIVERED = "already_delivered"
_SUBAGENT_DELIVERY_UNTRACKED = "untracked"
_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY = "missing_work_entry"
_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX = "missing_parent_inbox"
# A result whose own text marks it routine (``[quiet]``): recorded as delivered
# with its key stored, but no inbox entry or wake is raised for it.
_SUBAGENT_DELIVERY_QUIET = "quiet"
# A delayed terminal op (an interrupt's grace-timer cancel) whose originating
# dispatch has since been replaced by a newer send on the reused child session.
# Dropped without touching the newer dispatch so an old timer never cancels it.
_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH = "superseded_dispatch"
# Runner-owned labels on a child session that make sub-agent result delivery
# durable across a runner restart. The dispatch id is stamped when a turn is
# sent to the child; the delivered id is the receipt the parent's
# ``sys_read_inbox`` drain writes once it has consumed that turn's result.
SUBAGENT_DISPATCH_ID_LABEL_KEY = "omnigent.subagent.dispatch_id"
SUBAGENT_DELIVERED_ID_LABEL_KEY = "omnigent.subagent.delivered_id"
SUBAGENT_TERMINAL_STATUS_LABEL_KEY = "omnigent.subagent.terminal_status"
# Pins an undrained mother result across peer turns: "<work_id>:<status>:<item_id>".
# Restart recovery uses its outcome and item instead of the newer peer result.
SUBAGENT_RESULT_ITEM_LABEL_KEY = "omnigent.subagent.result_item"


# Bounded retry budget for the sub-agent wake POST. The wake is the sole
# delivery signal for the last child of a fan-out, and Omnigent routinely
# returns a transient 503 RUNNER_UNAVAILABLE while the parent's runner tunnel
# is reconnecting, so a single attempt can strand the parent silently.
_WAKE_POST_MAX_ATTEMPTS = 3
_WAKE_POST_RETRY_BASE_DELAY_S = 0.5
_WAKE_POST_RETRY_MAX_DELAY_S = 4.0


# 4xx statuses that are transient and worth retrying (mirrors the forwarder's
# classification): everything else in 4xx is a permanent client-side rejection.
_WAKE_POST_TRANSIENT_4XX = frozenset({408, 409, 425, 429})


@dataclasses.dataclass
class _SubagentWorkEntry:
    """
    Runner-local state for one asynchronous ``sys_session_send`` dispatch.

    :param parent_session_id: Parent session id that invoked
        ``sys_session_send``, e.g. ``"conv_parent123"``.
    :param child_session_id: Child session id used as the work handle,
        e.g. ``"conv_child456"``.
    :param work_id: Unique id for this dispatch to the child session,
        e.g. ``"subagent_a1b2c3"``.
    :param agent: Sub-agent name from the parent spec, e.g.
        ``"researcher"``.
    :param title: Caller-provided child instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional terminal wrapper label from the
        child session, e.g. ``"codex-native-ui"`` for codex-native
        native sub-agents.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param status: Current work status, e.g. ``"launching"`` or
        ``"running"``.
    :param output: Terminal child output or error text. ``None``
        while the work is still running.
    :param created_at: Unix timestamp when the dispatch was registered.
    :param completed_at: Unix timestamp when the dispatch reached a
        terminal status, or ``None`` while running.
    :param delivered: Whether the terminal payload has been pushed to
        the parent's inbox.
    :param cancellation_confirmed: Whether a native terminal edge confirmed
        an abort, rather than an interrupt merely being requested.
    :param remote: Whether the child runs on another host (cross-host
        member). Its ``running``/terminal status edges are emitted on the
        child's own runner and never reach this one.
    :param host_id: Host running the child when ``remote``, e.g.
        ``"host_a1b2c3"``; ``None`` for a same-host child.
    :param placement_label: Where the child runs, as ``"<host> · <cwd>"``,
        e.g. ``"fn · ~/projects/app"``; ``None`` when unknown.
    :param delivered_result_key: Server item id of the last assistant message
        whose result was delivered from this entry, or ``None`` when no key
        was readable. A terminal edge carrying the same key is a duplicate;
        a different key is the child's next turn.
    :param registered_by: The surface that registered the entry, e.g.
        ``"sys_session_create"`` or ``"sys_session_send"``. ``None`` for a
        recovery/forwarder-registered entry (a codex-internal thread).
    :param started_monotonic: Runner-local monotonic instant this dispatch
        was registered, used by the remote-member liveness interval.
    :param last_remote_check_monotonic: Runner-local monotonic instant of the
        last remote-member liveness check, or ``None`` until first checked.
    :param launch_timed_out: Whether the recorded ``failed`` came from the
        launch-liveness reaper rather than from the child itself. Such a
        failure is a guess ("no start acknowledgment"), so a genuine
        terminal edge from the child afterwards must replace it.
    :param recovered: Whether recovery rebuilt this entry for a turn the
        mother did not dispatch. Such an entry never reads as a mother
        dispatch, so a peer turn it is parented to is still kept as a copy.
    """

    parent_session_id: str
    child_session_id: str
    work_id: str
    agent: str
    title: str
    wrapper_label: str | None = None
    created_by: str | None = None
    status: str = "launching"
    output: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)
    completed_at: float | None = None
    delivered: bool = False
    cancellation_confirmed: bool = False
    remote: bool = False
    host_id: str | None = None
    placement_label: str | None = None
    delivered_result_key: str | None = None
    registered_by: str | None = None
    started_monotonic: float = dataclasses.field(default_factory=time.monotonic)
    last_remote_check_monotonic: float | None = None
    launch_timed_out: bool = False
    recovered: bool = False


@dataclasses.dataclass(frozen=True)
class _SubagentDeliveryAck:
    """
    Result of attempting to deliver a terminal sub-agent payload.

    :param entry: Work entry whose delivery was attempted, or ``None``
        when the child session is not tracked in the work registry.
    :param delivered: Whether the payload is confirmed delivered to the
        parent inbox. True for both first delivery and already-delivered
        duplicate terminal reports.
    :param delivered_now: Whether this attempt pushed a new payload into
        the parent inbox.
    :param reason: Machine-readable outcome, e.g. ``"delivered"`` or
        ``"missing_parent_inbox"``.
    """

    entry: _SubagentWorkEntry | None
    delivered: bool
    delivered_now: bool
    reason: str


_subagent_work_by_child: dict[str, _SubagentWorkEntry] = {}
_subagent_work_by_parent: dict[str, set[str]] = {}
# Old session id → its successor, seen by this runner. A work entry registered
# from a parent snapshot read before the server moved the children is rewritten
# through this map, closing the read-before-move race.
_succeeded_parents: dict[str, str] = {}
# Successor session id → inbox items held until the server posts the opening
# message and calls ``/succession/release``. Completions for a held successor
# land here instead of its live inbox and never wake it.
_held_successions: dict[str, list[_JsonObject]] = {}
# Drained children's last delivered result keys. A later terminal edge whose
# result key matches is a duplicate; a different key is the child's next turn.
_drained_delivered_subagent_results: dict[str, str | None] = {}
# Per-child origin (``_SubagentWorkEntry.registered_by``) kept after the entry
# is drained, so a recovery-rebuilt entry still knows the child was dispatched
# by an Omnigent tool and a Codex mother is woken for its later turns.
_subagent_work_origins: dict[str, str] = {}
# Parent owning each child's retained state (drained result key or origin)
# after its entry is gone. A drain removes the child from
# ``_subagent_work_by_parent``, so parent cleanup finds the remaining
# per-child entries through this owner map.
_subagent_retained_state_parents: dict[str, str] = {}
# Parents whose restart-recovery scan completed in this process, plus a
# per-parent lock so an init racing a sys_read_inbox drain cannot run two
# scans that both pass the registry check and queue one result twice.
_subagent_recovery_done: set[str] = set()
_subagent_recovery_locks: dict[str, asyncio.Lock] = {}

# Peer-turn copies: a settled child turn that answered a peer message from a
# session other than its mother. Kept on the mother's runner as silent copies —
# never a sub-agent result and never a wake — that a later inbox drain renders.
# In-memory only: unread copies vanish on a restart (the mother's own undrained
# result survives through the result-item label).
_peer_copies: dict[str, list[_JsonObject]] = {}
# Copies dropped by the cap since the last pop, per parent.
_peer_copy_dropped: dict[str, int] = {}
# (child, result key) -> status of copies already popped, per parent, bounded so
# a correction after a read is recognised without unbounded memory.
_peer_copy_seen: dict[str, dict[tuple[str, str], str]] = {}

_PEER_COPY_MAX_UNREAD = 20
_PEER_COPY_SEEN_MAX = 100
_PEER_COPY_OUTPUT_MAX_CHARS = 600

# Per-(parent, agent_type) monotonic ordinal counter for structured
# sub-agent names (e.g. "researcher-1", "researcher-2").
_subagent_ordinal_counters: dict[tuple[str, str], int] = {}


def next_subagent_ordinal(parent_session_id: str, agent_type: str) -> int:
    """Return the next ordinal for a (parent, agent_type) pair and bump the counter."""
    key = (parent_session_id, agent_type)
    ordinal = _subagent_ordinal_counters.get(key, 0) + 1
    _subagent_ordinal_counters[key] = ordinal
    return ordinal


def recover_subagent_ordinals(
    parent_session_id: str,
    agent_type: str,
    existing_children: list[dict[str, object]],
) -> None:
    """Set the ordinal high-water mark from existing children after a runner restart."""
    import re

    key = (parent_session_id, agent_type)
    if key in _subagent_ordinal_counters:
        return
    pattern = re.compile(rf"^{re.escape(agent_type)}-(\d+)$")
    max_ordinal = 0
    for child in existing_children:
        session_name = child.get("session_name")
        if isinstance(session_name, str):
            m = pattern.match(session_name)
            if m:
                max_ordinal = max(max_ordinal, int(m.group(1)))
    _subagent_ordinal_counters[key] = max_ordinal


def new_subagent_work_id() -> str:
    """
    Mint the id of one sub-agent dispatch, e.g. ``"subagent_a1b2c3d4e5f6"``.

    :returns: A fresh dispatch id.
    """
    return f"subagent_{uuid.uuid4().hex[:12]}"


# Per-child locks serializing the classify+register step of an in-flight
# sub-agent send (see ``tool_dispatch._send_to_in_flight_child``), so two
# concurrent sends to one child can't install divergent work entries. Co-located
# with the work registries so it is torn down alongside them — otherwise a
# long-lived runner would accumulate one lock per steered child forever.
_in_flight_send_locks: dict[str, asyncio.Lock] = {}


def in_flight_send_lock(child_session_id: str) -> asyncio.Lock:
    """
    Return (creating on first use) the per-child in-flight-send lock.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The lock guarding that child's in-flight-send bookkeeping.
    """
    lock = _in_flight_send_locks.get(child_session_id)
    if lock is None:
        lock = asyncio.Lock()
        _in_flight_send_locks[child_session_id] = lock
    return lock


# A succession chain is at most a few rotations deep; the bound only stops a
# cyclic map from looping forever.
_SUCCESSOR_MAX_HOPS = 8


def _resolve_succeeded_parent(parent_session_id: str) -> str:
    """Follow the succession chain from a stale parent to its live successor."""
    seen = {parent_session_id}
    for _ in range(_SUCCESSOR_MAX_HOPS):
        successor = _succeeded_parents.get(parent_session_id)
        if successor is None or successor in seen:
            break
        parent_session_id = successor
        seen.add(successor)
    return parent_session_id


def register_subagent_work(
    *,
    parent_session_id: str,
    child_session_id: str,
    agent: str,
    title: str,
    wrapper_label: str | None = None,
    created_by: str | None = None,
    work_id: str | None = None,
    remote: bool = False,
    host_id: str | None = None,
    placement_label: str | None = None,
    registered_by: str | None = None,
    flow_neutral: bool = False,
    recovered: bool = False,
) -> _SubagentWorkEntry:
    """
    Register one running sub-agent dispatch.

    Re-registering the same child replaces the prior entry so a
    repeated send to an existing child represents the latest turn.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param child_session_id: Child session id, e.g.
        ``"conv_child456"``.
    :param agent: Sub-agent name, e.g. ``"researcher"``.
    :param title: Sub-agent instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional child ``omnigent.wrapper``
        label, e.g. ``"claude-code-native-ui"``.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param work_id: Dispatch id already stamped on the child session,
        e.g. ``"subagent_a1b2c3d4e5f6"``; ``None`` mints a new one.
    :param remote: Whether the child runs on another host; see
        :attr:`_SubagentWorkEntry.remote`.
    :param host_id: Host running the child when ``remote``, e.g.
        ``"host_a1b2c3"``; see :attr:`_SubagentWorkEntry.host_id`.
    :param placement_label: Where the child runs, as ``"<host> · <cwd>"``;
        see :attr:`_SubagentWorkEntry.placement_label`.
    :param registered_by: The surface that registered the entry, e.g.
        ``"sys_session_create"`` or ``"sys_session_send"``; see
        :attr:`_SubagentWorkEntry.registered_by`.
    :param flow_neutral: When ``True``, a fresh entry does not join the
        parent's running flow. Recovery registers turns the mother did not
        dispatch this way, so an undispatched result is held only when the
        child already belonged to the flow.
    :param recovered: When ``True``, the entry is rebuilt for a turn the
        mother did not dispatch; see :attr:`_SubagentWorkEntry.recovered`.
    :returns: The registered work entry.
    """
    parent_session_id = _resolve_succeeded_parent(parent_session_id)
    prior = _subagent_work_by_child.get(child_session_id)
    if prior is not None:
        children = _subagent_work_by_parent.get(prior.parent_session_id)
        if children is not None:
            children.discard(child_session_id)
            if not children:
                _subagent_work_by_parent.pop(prior.parent_session_id, None)
    # The last delivered result key survives a re-dispatch: it is what makes
    # a delayed or retried terminal edge for the prior turn a duplicate
    # instead of a cancellation of the new one (D9).
    previous_result_key = (
        prior.delivered_result_key
        if prior is not None and prior.delivered
        else _drained_delivered_subagent_results.get(child_session_id)
    )

    entry = _SubagentWorkEntry(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        work_id=work_id or new_subagent_work_id(),
        agent=agent,
        title=title,
        wrapper_label=wrapper_label,
        created_by=created_by,
        remote=remote,
        host_id=host_id,
        placement_label=placement_label,
        delivered_result_key=previous_result_key,
        registered_by=registered_by,
        recovered=recovered,
    )
    _drained_delivered_subagent_results.pop(child_session_id, None)
    if registered_by is not None:
        # The origin outlives the entry: recovery re-registers a drained child
        # without it and must still know an Omnigent tool dispatched it.
        _subagent_work_origins[child_session_id] = registered_by
    if child_session_id in _subagent_work_origins:
        _subagent_retained_state_parents[child_session_id] = parent_session_id
    else:
        _subagent_retained_state_parents.pop(child_session_id, None)
    _subagent_work_by_child[child_session_id] = entry
    _subagent_work_by_parent.setdefault(parent_session_id, set()).add(child_session_id)
    if not flow_neutral:
        from omnigent.runner.flows import note_child_dispatch

        note_child_dispatch(parent_session_id, child_session_id)
    return entry


def get_subagent_work(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Return registered sub-agent work by child session id.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The work entry, or ``None`` if the child is not tracked.
    """
    return _subagent_work_by_child.get(child_session_id)


def record_peer_copy(
    parent_session_id: str,
    child_session_id: str,
    key: str,
    payload: _JsonObject,
) -> None:
    """
    Record or amend one silent peer-turn copy for a parent.

    Copies are keyed by ``(child, key)`` where ``key`` is the peer turn's result
    item id. An unread copy with the same key is amended in place to the new
    status/output; a replay with an unchanged status is a no-op; a copy already
    popped whose status has since changed is appended again as a correction. At
    most :data:`_PEER_COPY_MAX_UNREAD` unread copies are kept per parent, the
    oldest dropped first and counted for the next drain.

    :param parent_session_id: The mother session that receives the copy.
    :param child_session_id: The child that answered the peer, e.g.
        ``"conv_child456"``.
    :param key: The peer turn's result item id.
    :param payload: The fully built copy payload.
    :returns: None.
    """
    parent_session_id = _resolve_succeeded_parent(parent_session_id)
    identity = (child_session_id, key)
    status = payload.get("status")
    copies = _peer_copies.setdefault(parent_session_id, [])
    for existing in copies:
        if (existing.get("child_session_id"), existing.get("result_item_id")) == identity:
            if existing.get("status") == status:
                return
            existing["status"] = status
            existing["output"] = payload.get("output")
            return
    seen = _peer_copy_seen.get(parent_session_id)
    if seen is not None and identity in seen:
        if seen[identity] == status:
            return
        payload = {**payload, "correction": True}
    copies.append(payload)
    if len(copies) > _PEER_COPY_MAX_UNREAD:
        copies.pop(0)
        _peer_copy_dropped[parent_session_id] = _peer_copy_dropped.get(parent_session_id, 0) + 1


def pop_peer_copies(parent_session_id: str) -> tuple[list[_JsonObject], int]:
    """
    Drain a parent's unread peer copies oldest first.

    Pops the copies and the count dropped by the cap since the previous pop,
    remembering each popped copy's status (bounded) so a later status change is
    recorded as a correction.

    :param parent_session_id: The mother session, e.g. ``"conv_parent123"``.
    :returns: ``(copies, dropped)`` — the unread copies oldest first and the
        number of copies dropped by the cap since the last pop.
    """
    copies = _peer_copies.pop(parent_session_id, [])
    dropped = _peer_copy_dropped.pop(parent_session_id, 0)
    if copies:
        seen = _peer_copy_seen.setdefault(parent_session_id, {})
        for copy in copies:
            child_id = copy.get("child_session_id")
            result_key = copy.get("result_item_id")
            copy_status = copy.get("status")
            if not isinstance(child_id, str) or not isinstance(result_key, str):
                continue
            identity = (child_id, result_key)
            seen.pop(identity, None)
            seen[identity] = copy_status if isinstance(copy_status, str) else ""
        while len(seen) > _PEER_COPY_SEEN_MAX:
            seen.pop(next(iter(seen)))
    return copies, dropped


def move_peer_copies(old_parent_id: str, new_parent_id: str) -> None:
    """
    Move a retired parent's peer-copy state onto its successor.

    A successor may already hold copies. The retired parent's copies are older,
    so identities keep their earliest position with the successor's newer
    payload. The cap drops from the front and drop counts add. Successor seen
    identities keep their newer status and move last before trimming.

    :param old_parent_id: The retired parent session, e.g. ``"conv_old123"``.
    :param new_parent_id: Its successor, e.g. ``"conv_new456"``.
    :returns: None.
    """
    old_copies = _peer_copies.pop(old_parent_id, None)
    if old_copies:
        combined = list(
            {
                (copy.get("child_session_id"), copy.get("result_item_id")): copy
                for copy in old_copies + _peer_copies.get(new_parent_id, [])
            }.values()
        )
        overflow = len(combined) - _PEER_COPY_MAX_UNREAD
        if overflow > 0:
            combined = combined[overflow:]
            _peer_copy_dropped[new_parent_id] = _peer_copy_dropped.get(new_parent_id, 0) + overflow
        _peer_copies[new_parent_id] = combined
    old_dropped = _peer_copy_dropped.pop(old_parent_id, None)
    if old_dropped:
        _peer_copy_dropped[new_parent_id] = _peer_copy_dropped.get(new_parent_id, 0) + old_dropped
    old_seen = _peer_copy_seen.pop(old_parent_id, None)
    if old_seen:
        merged = old_seen
        for identity, status in _peer_copy_seen.get(new_parent_id, {}).items():
            merged.pop(identity, None)
            merged[identity] = status
        while len(merged) > _PEER_COPY_SEEN_MAX:
            merged.pop(next(iter(merged)))
        _peer_copy_seen[new_parent_id] = merged


def clear_peer_copies(session_id: str) -> None:
    """
    Drop a deleted session's peer-copy state.

    :param session_id: Session id being deleted, e.g. ``"conv_parent123"``.
    :returns: None.
    """
    _peer_copies.pop(session_id, None)
    _peer_copy_dropped.pop(session_id, None)
    _peer_copy_seen.pop(session_id, None)


def mark_subagent_work_started(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Promote a sub-agent dispatch from launch bookkeeping to real execution.

    ``sys_session_send`` creates the child session and registers work before
    the child harness has proven it started. The first child
    ``session.status:running`` / ``waiting`` edge is that proof.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The updated work entry, or ``None`` if the child is untracked.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return None
    if entry.status in {"launching", "waiting"}:
        entry.status = "running"
    return entry


def unregister_subagent_work(
    child_session_id: str,
    *,
    work_id: str | None = None,
    remember_drained_delivery: bool = False,
) -> None:
    """
    Remove sub-agent work tracking for a child session.

    Used when the child-message POST fails before a handle has been
    returned to the LLM.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param work_id: Optional dispatch id guard. When provided, the
        current registry entry is removed only if it still belongs to
        that dispatch.
    :param remember_drained_delivery: Whether to retain a delivered entry's
        result key, or the prior drained key carried by a recovered non-terminal
        placeholder, so replayed results remain acknowledged as drained.
    :returns: None.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return
    if work_id is not None and entry.work_id != work_id:
        return
    if remember_drained_delivery and (
        entry.delivered
        or (
            entry.recovered
            and entry.status not in _SUBAGENT_TERMINAL_STATUSES
            and entry.delivered_result_key is not None
        )
    ):
        _drained_delivered_subagent_results[child_session_id] = entry.delivered_result_key
        _subagent_retained_state_parents[child_session_id] = entry.parent_session_id
    _subagent_work_by_child.pop(child_session_id, None)
    _in_flight_send_locks.pop(child_session_id, None)
    children = _subagent_work_by_parent.get(entry.parent_session_id)
    if children is None:
        return
    children.discard(child_session_id)
    if not children:
        _subagent_work_by_parent.pop(entry.parent_session_id, None)


def unregister_subagent_work_for_session(session_id: str) -> None:
    """
    Remove sub-agent work associated with a deleted session.

    A deleted session can be either the child work handle itself or
    the parent that owns several child handles. Both indexes are
    cleaned so runner-local state cannot outlive the session tree.

    :param session_id: Session id being deleted, e.g.
        ``"conv_parent123"`` or ``"conv_child456"``.
    :returns: None.
    """
    unregister_subagent_work(session_id)
    _drained_delivered_subagent_results.pop(session_id, None)
    _subagent_work_origins.pop(session_id, None)
    _subagent_retained_state_parents.pop(session_id, None)
    _in_flight_send_locks.pop(session_id, None)
    for child_id in list(_subagent_work_by_parent.get(session_id, set())):
        _subagent_work_by_child.pop(child_id, None)
        _drained_delivered_subagent_results.pop(child_id, None)
        _subagent_work_origins.pop(child_id, None)
        _subagent_retained_state_parents.pop(child_id, None)
        _in_flight_send_locks.pop(child_id, None)
    # A drained child left the parent index above, so its retained state is
    # found through the owner map instead.
    for child_id, parent_id in list(_subagent_retained_state_parents.items()):
        if parent_id != session_id:
            continue
        _drained_delivered_subagent_results.pop(child_id, None)
        _subagent_work_origins.pop(child_id, None)
        _subagent_retained_state_parents.pop(child_id, None)
        _in_flight_send_locks.pop(child_id, None)
    _subagent_work_by_parent.pop(session_id, None)
    clear_peer_copies(session_id)


def list_subagent_work(parent_session_id: str) -> list[_SubagentWorkEntry]:
    """
    List sub-agent work registered by a parent session.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Work entries ordered by creation time.
    """
    child_ids = _subagent_work_by_parent.get(parent_session_id, set())
    entries = [
        entry
        for child_id in child_ids
        if (entry := _subagent_work_by_child.get(child_id)) is not None
    ]
    return sorted(entries, key=lambda entry: entry.created_at)


# Harness whose sub-agents live as threads inside the parent's own app-server.
_CODEX_NATIVE_HARNESS = "codex-native"


def is_codex_native_subagent_wrapper(wrapper_label: str | None) -> bool:
    """
    Whether a child's wrapper label marks it a codex-native sub-agent.

    Covers both a codex-spawned sub-agent and a ``/side`` side chat: each is a
    thread inside the parent's own app-server, so codex consumes its result in
    the thread tree and the parent is never waiting on the Omnigent inbox for it.

    :param wrapper_label: The child's ``omnigent.wrapper`` label, or ``None``.
    :returns: ``True`` when the child is a codex-native sub-agent.
    """
    if wrapper_label is None:
        return False
    agent = native_coding_agent_for_harness(_CODEX_NATIVE_HARNESS)
    return agent is not None and wrapper_label == agent.subagent_wrapper_label


def undelivered_subagent_dispatch_id(labels: Mapping[str, object]) -> str | None:
    """
    Return the dispatch id of a child turn whose result the parent never drained.

    :param labels: Child session labels, e.g.
        ``{"omnigent.subagent.dispatch_id": "subagent_a1b2c3d4e5f6"}``.
    :returns: The dispatch id when the delivered-id receipt is missing or
        names an earlier turn; ``None`` for a drained turn, or for a child
        created before dispatch ids were stamped.
    """
    dispatch_id = labels.get(SUBAGENT_DISPATCH_ID_LABEL_KEY)
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    if labels.get(SUBAGENT_DELIVERED_ID_LABEL_KEY) == dispatch_id:
        return None
    return dispatch_id


def _stamped_result_item_id(
    labels: Mapping[str, object], dispatch_id: str
) -> tuple[str, str] | None:
    """
    Return the terminal status and item id a peer-copy restart label pins.

    The label reads ``"<work_id>:<status>:<item_id>"`` and only applies to the
    undelivered dispatch. Malformed labels and non-terminal statuses are inert.

    :param labels: Child session labels, e.g.
        ``{"omnigent.subagent.result_item": "subagent_a1b2:cancelled:item_c3d4"}``.
    :param dispatch_id: The undelivered dispatch id, e.g. ``"subagent_a1b2"``.
    :returns: The pinned ``(status, item_id)``, or ``None`` for an inert label.
    """
    raw = labels.get(SUBAGENT_RESULT_ITEM_LABEL_KEY)
    if not isinstance(raw, str):
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    work_id, status, item_id = parts
    if (
        work_id != dispatch_id
        or status not in _SUBAGENT_TERMINAL_STATUSES
        or not item_id
        or ":" in item_id
    ):
        return None
    return status, item_id


class _SubagentRecoveryReadError(Exception):
    """A sessions API read needed by restart recovery returned a non-200."""


async def _get_recovery_page(
    server_client: httpx.AsyncClient, path: str, params: dict[str, str]
) -> Any:
    """
    Read one page of a sessions API listing for restart recovery.

    :param server_client: HTTP client connected to the Omnigent server.
    :param path: Sessions API path, e.g. ``"/v1/sessions/conv_p/child_sessions"``.
    :param params: Query parameters, e.g. ``{"limit": "1000"}``.
    :returns: The decoded JSON page.
    :raises _SubagentRecoveryReadError: When the server returns a non-200.
    """
    response = await server_client.get(path, params=params, timeout=10.0)
    if response.status_code != 200:
        raise _SubagentRecoveryReadError(f"{path} returned {response.status_code}")
    return response.json()


async def _list_child_sessions(
    server_client: httpx.AsyncClient, parent_id: str
) -> list[_JsonObject]:
    """
    Return every child-session summary of a parent, following pagination.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session id, e.g. ``"conv_parent123"``.
    :returns: Child summaries as returned by the sessions API.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    children: list[_JsonObject] = []
    params: dict[str, str] = {"limit": "1000"}
    while True:
        page = await _get_recovery_page(
            server_client, f"/v1/sessions/{parent_id}/child_sessions", params
        )
        children.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("last_id"):
            return children
        params["after"] = page["last_id"]


async def _fetch_latest_assistant_item(
    server_client: httpx.AsyncClient, session_id: str
) -> tuple[str | None, str] | None:
    """
    Return the latest turn's newest assistant message as ``(id, text)``.

    :param server_client: HTTP client connected to the Omnigent server.
    :param session_id: Session to read, e.g. ``"conv_child456"``.
    Reading newest first stops at the first non-meta user message or tool item:
    crossing that boundary would reuse an assistant answer from an older turn.
    Meta messages do not start a turn and are skipped.

    :returns: The server item id (``None`` when the page omitted it) and the
        joined text blocks of the latest turn's newest assistant message
        (empty when that message carries no text, matching live delivery), or
        ``None`` when the latest turn has no assistant message.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    params: dict[str, str] = {"limit": "100", "order": "desc"}
    while True:
        page = await _get_recovery_page(server_client, f"/v1/sessions/{session_id}/items", params)
        for item in page.get("data", []):
            item_type = item.get("type")
            if item_type == "message" and item.get("is_meta") is True:
                continue
            if item_type == "message":
                if item.get("role") == "assistant":
                    raw_id = item.get("id")
                    item_id = raw_id if isinstance(raw_id, str) and raw_id else None
                    return item_id, "\n".join(
                        block["text"]
                        for block in item.get("content", [])
                        if block.get("type") in {"output_text", "text"} and block.get("text")
                    )
                if item.get("role") == "user":
                    return None
                continue
            if item_type in {"function_call", "function_call_output"}:
                return None
        if not page.get("has_more") or not page.get("last_id"):
            return None
        params["after"] = page["last_id"]


async def _fetch_item_text_by_id(
    server_client: httpx.AsyncClient, session_id: str, item_id: str
) -> str | None:
    """
    Return the joined text of the assistant item with *item_id*, or ``None``.

    Reads the bounded item window around the anchor instead of paging the whole
    transcript. A 404, a 400 stale cursor, or a non-assistant item reads as
    ``None`` so recovery skips only that child instead of using a newer item.

    :param server_client: HTTP client connected to the Omnigent server.
    :param session_id: Session to read, e.g. ``"conv_child456"``.
    :param item_id: The server item id to find, e.g. ``"item_c3d4"``.
    :returns: The joined text blocks of the item, or ``None`` when it is not
        found or carries no assistant text.
    :raises _SubagentRecoveryReadError: When a window read returns a non-200
        other than 404 or 400 with error code ``stale_cursor``.
    """
    response = await server_client.get(
        f"/v1/sessions/{session_id}/items/window",
        params={"anchor_id": item_id, "before": "1", "after": "1"},
        timeout=10.0,
    )
    if response.status_code == 404:
        return None
    if response.status_code == 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("code") == "stale_cursor":
            return None
    if response.status_code != 200:
        raise _SubagentRecoveryReadError(
            f"/v1/sessions/{session_id}/items/window returned {response.status_code}"
        )
    for item in response.json().get("data", []):
        if item.get("id") != item_id:
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            return None
        return "\n".join(
            block["text"]
            for block in item.get("content", [])
            if block.get("type") in {"output_text", "text"} and block.get("text")
        )
    return None


async def _recover_subagent_results_from_server(
    *,
    server_client: httpx.AsyncClient,
    parent_id: str,
    schedule_wake: Callable[[_SubagentWorkEntry], None],
) -> None:
    """
    Re-queue terminal child results whose delivery receipt is missing.

    A child turn is stamped with a dispatch id when it is sent, and the
    parent's ``sys_read_inbox`` drain writes that id back as the delivered
    id. A terminal child whose two ids differ was never drained, so its
    result is rebuilt from the child transcript and queued again under the
    same dispatch id, letting the eventual drain close the loop. A peer-copy
    label pins the mother's terminal status and result item across later turns.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session whose inbox was recreated, e.g.
        ``"conv_parent123"``.
    :param schedule_wake: Callback that posts the parent wake notice.
    :raises _SubagentRecoveryReadError: When a server read returns a
        non-200; the caller retries on the next drain.
    """
    for child in await _list_child_sessions(server_client, parent_id):
        child_id = child.get("id")
        status = child.get("current_task_status")
        if not isinstance(child_id, str) or not isinstance(status, str):
            continue
        error = child.get("last_task_error")
        interrupted = status == "in_progress" or (
            status == "failed"
            and isinstance(error, dict)
            and error.get("code") in {"runner_disconnected", "runner_failed_to_start"}
        )
        if status not in _SUBAGENT_TERMINAL_STATUSES and not interrupted:
            continue
        existing = get_subagent_work(child_id)
        if (existing is not None and existing.status != "waiting") or (
            child_id in _drained_delivered_subagent_results
        ):
            continue
        labels = child.get("labels")
        label_map = labels if isinstance(labels, dict) else {}
        dispatch_id = undelivered_subagent_dispatch_id(label_map)
        if dispatch_id is None or (existing is not None and existing.work_id != dispatch_id):
            continue
        output: str | None = None
        result_key: str | None = None
        recover_status = status
        # A pin preserves the mother's outcome across later peer turns.
        # A missing item skips only this child instead of using a newer answer.
        stamped_result = _stamped_result_item_id(label_map, dispatch_id)
        if stamped_result is not None:
            recover_status, stamped_id = stamped_result
            text = await _fetch_item_text_by_id(server_client, child_id, stamped_id)
            if text is None:
                _logger.warning(
                    "Recovery result-item label names a missing item for child=%s "
                    "dispatch=%s item=%s; skipping.",
                    child_id,
                    dispatch_id,
                    stamped_id,
                )
                continue
            result_key, output = stamped_id, text
            interrupted = False
        elif status == "failed":
            error = child.get("last_task_error")
            message = error.get("message") if isinstance(error, dict) else None
            output = message if isinstance(message, str) else None
        elif not interrupted:
            item = await _fetch_latest_assistant_item(server_client, child_id)
            if item is not None:
                result_key, output = item
            if output is None and status == "stopped":
                output = "Sub-agent stopped before producing a reliable final result."
            elif output is None and status == "killed":
                output = "Sub-agent was killed before producing a reliable final result."
        # A forwarded completion or newer dispatch may arrive during the history read.
        if (
            get_subagent_work(child_id) is not existing
            or (existing is not None and existing.status != "waiting")
            or child_id in _drained_delivered_subagent_results
        ):
            continue
        entry = existing or register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent=str(child.get("tool") or child.get("agent_name") or "sub-agent"),
            title=str(child.get("session_name") or ""),
            work_id=dispatch_id,
            # Only an Omnigent dispatch stamps a dispatch id, so the rebuilt
            # entry may wake a Codex mother even after a restart wiped the
            # origin map.
            registered_by=_subagent_work_origins.get(child_id) or "recovery",
        )
        if interrupted:
            # This dispatch already existed; a local launch timeout cannot judge it.
            entry.status = "waiting"
            continue
        ack = mark_subagent_work_terminal(
            child_id, status=recover_status, output=output, result_key=result_key
        )
        if ack.delivered_now:
            schedule_wake(entry)


def _is_quiet_output(output: str | None) -> bool:
    """Whether a result's last non-empty line is exactly ``[quiet]`` (D9)."""
    if output is None:
        return False
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped == "[quiet]"
    return False


def mark_subagent_work_terminal(
    child_session_id: str,
    *,
    status: str,
    output: str | None,
    result_key: str | None = None,
    only_if_work_id: str | None = None,
) -> _SubagentDeliveryAck:
    """
    Mark a sub-agent dispatch terminal and notify the parent inbox.

    Delivery is keyed on the result, not the child: a terminal edge whose
    ``result_key`` (the last assistant item's server id) matches the last
    delivered one is a duplicate, while a different key is the child's next
    turn and is delivered — even after the mother drained the prior result.
    A missing key falls back to the old child-level dedup. Distinguishing a
    confirmed completion from a bare quiescence idle is the caller's job (the
    events route only reports ``completed`` for a confirmed turn-end or a
    legacy harness).

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param status: Terminal status: ``"completed"``, ``"failed"``,
        ``"cancelled"``, ``"stopped"``, or ``"killed"``.
    :param output: Child output or error text. ``None`` means the
        completion had no assistant text to deliver.
        If an earlier terminal report could not be delivered, a later
        report for the same child replaces the undelivered status and
        output before retrying parent inbox delivery.
    :param result_key: Server item id of this result's last assistant
        message, or ``None`` when no key was readable.
    :param only_if_work_id: When set, apply this report only if the current
        entry is still that dispatch. A delayed op (an interrupt's grace-timer
        cancel) bound to the dispatch it was raised for is dropped when a newer
        send has replaced the entry, so an old timer never cancels new work.
    :returns: Delivery acknowledgement for this terminal report.
    :raises ValueError: If ``status`` is not terminal.
    """
    if status not in _SUBAGENT_TERMINAL_STATUSES:
        raise ValueError(
            f"sub-agent terminal status must be one of "
            f"{sorted(_SUBAGENT_TERMINAL_STATUSES)}; got {status!r}"
        )
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        if child_session_id in _drained_delivered_subagent_results:
            drained_key = _drained_delivered_subagent_results[child_session_id]
            if result_key is None or result_key == drained_key:
                return _SubagentDeliveryAck(
                    entry=None,
                    delivered=True,
                    delivered_now=False,
                    reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
                )
            # A new result whose entry is gone should have been reopened by
            # ``_ensure_subagent_work_entry``; arriving here means the caller
            # skipped it, so report it as untracked rather than swallowing it.
            return _SubagentDeliveryAck(
                entry=None,
                delivered=False,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_UNTRACKED,
            )
        return _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        )
    if only_if_work_id is not None and entry.work_id != only_if_work_id:
        # A delayed op raised for an earlier dispatch: a newer send replaced the
        # entry on this reused child session. Drop it untouched so an old
        # interrupt's grace timer can never cancel the new dispatch.
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH,
        )
    # A retried or delayed terminal edge for an already-delivered result:
    # its key was stored at delivery, so a repeat never re-opens the turn
    # (and never terminates a newer turn the child has since started). A
    # failure report for that same result still escalates below: the key
    # names the turn, and a completed record for it may be the watcher's
    # quiescence edge that the real failure must replace.
    same_key = result_key is not None and entry.delivered_result_key == result_key
    failure_escalates = (
        same_key and status in {"failed", "stopped", "killed"} and entry.status == "completed"
    )
    if same_key and not failure_escalates:
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
        )
    if entry.status in _SUBAGENT_TERMINAL_STATUSES:
        # ``failed`` outranks ``completed``: a quiescence-derived ``completed``
        # (the watcher's ``idle`` edge) can be recorded — and delivered — before
        # the turn's real ``failed`` edge lands. The failure must replace it and
        # be re-delivered, or the parent is left believing the turn succeeded
        # and the error text is silently dropped. A parent may act on the false
        # success before the re-delivery arrives — that window is inherent to
        # the edge race; re-delivery is the mitigation, not a prevention.
        if status in {"failed", "stopped", "killed"} and entry.status == "completed":
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
            entry.delivered = False
            return _deliver_subagent_completion(entry, result_key)
        # A child-reported terminal state supersedes the reaper's provisional failure.
        if entry.launch_timed_out and status in ("completed", "failed"):
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
            entry.delivered = False
            entry.launch_timed_out = False
            return _deliver_subagent_completion(entry, result_key)
        if status == entry.status and output and output != entry.output:
            entry.output = output
            entry.completed_at = time.time()
            entry.delivered = False
            return _deliver_subagent_completion(entry, result_key)
        if entry.delivered:
            # A new key (checked above) on a delivered terminal entry is the
            # child's next turn: reopen and deliver (D9).
            if result_key is not None:
                entry.status = status
                entry.output = output
                entry.completed_at = time.time()
                entry.delivered = False
                return _deliver_subagent_completion(entry, result_key)
            return _SubagentDeliveryAck(
                entry=entry,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        # A late stop_session-driven "cancelled" must not downgrade an
        # already-recorded "completed"/"failed" still awaiting delivery, and a
        # trailing quiescence "completed" must not launder a recorded "failed".
        keep_recorded = (status == "cancelled" and entry.status != "cancelled") or (
            status == "completed" and entry.status in {"failed", "stopped", "killed"}
        )
        if not keep_recorded:
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
        return _deliver_subagent_completion(entry, result_key)
    entry.status = status
    entry.output = output
    entry.completed_at = time.time()
    return _deliver_subagent_completion(entry, result_key)


def _deliver_subagent_completion(
    entry: _SubagentWorkEntry, result_key: str | None = None
) -> _SubagentDeliveryAck:
    """
    Push a terminal sub-agent payload into the parent session inbox.

    A quiet result — output whose last non-empty line is ``[quiet]`` — is
    recorded as delivered (key stored) without an inbox entry or wake: the
    child marked its own turn as routine.

    :param entry: Terminal sub-agent work entry to deliver.
    :param result_key: The result's identity, stored on delivery.
    :returns: Delivery acknowledgement describing whether the payload is
        confirmed in the parent inbox.
    """
    if entry.delivered:
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
        )
    if _is_quiet_output(entry.output):
        entry.delivered = True
        entry.delivered_result_key = result_key
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_QUIET,
        )
    output = entry.output
    if output is None:
        # Only a completion needs the explicit no-output marker; a cancelled
        # dispatch legitimately has nothing to report.
        output = (
            "[System: sub-agent completed with no output]" if entry.status == "completed" else ""
        )
    payload: _JsonObject = {
        "type": "sub_agent",
        "work_id": entry.work_id,
        "task_id": entry.child_session_id,
        "handle_id": entry.child_session_id,
        "conversation_id": entry.child_session_id,
        "tool_name": entry.agent,
        "agent": entry.agent,
        "title": entry.title,
        "status": entry.status,
        "output": output,
        "placement_label": entry.placement_label,
    }
    held = _held_successions.get(entry.parent_session_id)
    if held is not None:
        # A successor still waiting for its opening message must not be woken
        # by a child result; the held buffer drains on release.
        held.append(payload)
        entry.delivered = True
        entry.delivered_result_key = result_key
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_DELIVERED,
        )
    inbox = _session_inboxes_ref.get(entry.parent_session_id)
    if inbox is None:
        _logger.warning(
            "Sub-agent work completed but parent inbox is missing; parent=%s child=%s",
            entry.parent_session_id,
            entry.child_session_id,
        )
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
        )
    inbox.put_nowait(payload)
    entry.delivered = True
    entry.delivered_result_key = result_key
    return _SubagentDeliveryAck(
        entry=entry,
        delivered=True,
        delivered_now=True,
        reason=_SUBAGENT_DELIVERY_DELIVERED,
    )


def reap_stalled_subagent_launches(
    *,
    now: float | None = None,
    timeout_s: float | None = None,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
) -> list[_SubagentWorkEntry]:
    """
    Fail sub-agent dispatches stuck in ``launching`` beyond the liveness budget.

    A dispatch with no running/waiting/terminal status acknowledgment can
    otherwise remain pending forever. Missing acknowledgment does not prove
    that the child process never started. Each reaped entry is
    marked ``failed`` and its failure is delivered to the parent inbox through
    ``mark_terminal``.

    :param now: Clock override for tests, e.g. ``time.time()``.
    :param timeout_s: Budget override for tests; defaults to
        :func:`resolve_subagent_launch_timeout_s`.
    :param mark_terminal: Terminal-delivery callback. Production passes the
        app's ``mark_subagent_terminal_and_wake`` seam so the reaped failure
        also schedules the parent wake POST — the sole signal that rouses an
        idle parent to drain its inbox. Defaults to the inbox-only
        :func:`mark_subagent_work_terminal`.
    :returns: The entries that were failed by this sweep.
    """
    budget = resolve_subagent_launch_timeout_s() if timeout_s is None else timeout_s
    if budget <= 0:
        return []
    deliver = mark_subagent_work_terminal if mark_terminal is None else mark_terminal
    current = time.time() if now is None else now
    reaped: list[_SubagentWorkEntry] = []
    for entry in list(_subagent_work_by_child.values()):
        if entry.status != "launching":
            continue
        if current - entry.created_at < budget:
            continue
        _logger.warning(
            "Sub-agent dispatch stuck in launching for %.0fs; failing it: parent=%s child=%s",
            current - entry.created_at,
            entry.parent_session_id,
            entry.child_session_id,
        )
        entry.launch_timed_out = True
        deliver(
            entry.child_session_id,
            status="failed",
            output=(
                f"Error: no start acknowledgment for sub-agent {entry.agent!r} "
                f"title {entry.title!r} within {budget:.0f}s of dispatch. "
                "The child may still be running; inspect its session before retrying."
            ),
        )
        reaped.append(entry)
    return reaped


async def run_subagent_launch_reaper(
    *,
    interval_s: float = SUBAGENT_LAUNCH_REAP_INTERVAL_S,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
    reconcile_pending: Callable[[], Awaitable[None]] | None = None,
    check_remote_members: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """
    Periodically sweep for sub-agent dispatches wedged in ``launching``.

    Runs until cancelled; started by the runner entrypoint alongside the
    process manager. Sweep errors are logged and never end the loop.

    :param interval_s: Seconds between sweeps, e.g. ``30.0``.
    :param mark_terminal: Terminal-delivery callback forwarded to each sweep;
        the entrypoint passes the app's wake-scheduling seam so a reaped
        failure wakes the parent, not just its inbox.
    :param reconcile_pending: Refresh recovered work awaiting remote completion.
    :param check_remote_members: Slow liveness check for remote members that
        ended without reporting a result.
    :returns: None.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            reap_stalled_subagent_launches(mark_terminal=mark_terminal)
            if reconcile_pending is not None:
                await reconcile_pending()
            if check_remote_members is not None:
                await check_remote_members()
        except Exception:  # noqa: BLE001 — the sweep is a backstop; never die.
            _logger.warning("sub-agent launch reaper sweep failed", exc_info=True)


async def _wake_retry_sleep(seconds: float) -> None:
    """
    Sleep between sub-agent wake-POST retries.

    Indirection point so tests can stub the backoff without clobbering the
    process-wide ``asyncio.sleep`` (the ``no-global-asyncio-patch`` lint
    hook bans patching the module singleton).

    :param seconds: Seconds to wait before the next retry, e.g. ``0.5``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


def _wake_post_is_retryable(exc: httpx.HTTPError) -> bool:
    """
    Return whether a failed wake POST should be retried.

    Transport-level failures (connect/read errors, timeouts) are always
    retryable. A non-2xx response surfaces as :class:`httpx.HTTPStatusError`:
    5xx statuses are transient (notably the 503 ``RUNNER_UNAVAILABLE`` that
    Omnigent returns while the parent's runner tunnel is reconnecting), as
    are a few 4xx codes; every other 4xx is a permanent client-side rejection
    that retrying cannot fix.

    :param exc: HTTP error raised by the wake POST or ``raise_for_status``,
        e.g. an ``httpx.HTTPStatusError`` wrapping a 503 response.
    :returns: ``True`` if a bounded retry is worthwhile, else ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        # Transport failure — the POST may never have reached Omnigent.
        return True
    status_code = exc.response.status_code
    if status_code >= 500:
        return True
    return status_code in _WAKE_POST_TRANSIENT_4XX


async def _deliver_subagent_wake_post(
    server_client: httpx.AsyncClient,
    parent_id: str,
    notice: str,
    *,
    created_by: str | None = None,
) -> bool:
    """
    POST a sub-agent wake notice with a bounded retry on transient failure.

    httpx does not raise on a non-2xx response, so a real 503
    ``RUNNER_UNAVAILABLE`` JSON response (routine while the parent's runner
    tunnel reconnects) would otherwise be treated as a successful delivery.
    This calls ``raise_for_status`` to turn any non-2xx into a failure and
    retries transient failures up to :data:`_WAKE_POST_MAX_ATTEMPTS` with
    exponential backoff, because the wake is the sole delivery signal for
    the last child of a fan-out. Permanent 4xx rejections stop immediately.

    :param server_client: Omnigent HTTP client for the runner subprocess.
    :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
    :param notice: The ``[System: ...]`` notice text to inject.
    :param created_by: Human actor that dispatched the completed child
        turn, if known.
    :returns: ``True`` if a 2xx was confirmed, ``False`` if every attempt
        failed (transport error, timeout, or non-2xx response).
    """
    attribution_created_by = created_by
    for attempt in range(1, _WAKE_POST_MAX_ATTEMPTS + 1):
        try:
            resp = await server_client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": notice}],
                    },
                    **(
                        {"created_by": attribution_created_by}
                        if attribution_created_by is not None
                        else {}
                    ),
                },
                # The server gates this injected wake at the parent's REQUEST
                # phase, which can PARK on a human ASK (e.g. session_cost_budget)
                # for up to the deciding policy's ``ask_timeout`` (default one
                # day). A 30s read budget severed that park after 30s → the
                # TimeoutError below retried → each retry re-posted the notice
                # and parked ANOTHER gate → duplicate approval cards, and the
                # gate never cleanly blocked. Hold the read budget at one day so
                # this POST waits for the real verdict (one held connection, one
                # card); fast connect so an unreachable parent runner still
                # fails out into the bounded retry below.
                timeout=_ASK_GATE_DELIVERY_TIMEOUT,
            )
            # Treat a non-2xx RESPONSE (e.g. a genuine 503 JSONResponse) as a
            # failure — httpx does not raise on status by itself.
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if (
                attribution_created_by is not None
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 403
            ):
                _logger.debug(
                    "Sub-agent wake POST attribution rejected for parent=%s; "
                    "retrying without actor",
                    parent_id,
                    extra={"session_id": runner_primary_session_id()},
                )
                attribution_created_by = None
                continue
            last_attempt = attempt >= _WAKE_POST_MAX_ATTEMPTS
            retryable = isinstance(exc, asyncio.TimeoutError) or _wake_post_is_retryable(exc)
            _logger.debug(
                "Sub-agent wake POST attempt %d/%d for parent=%s failed (retryable=%s): %r",
                attempt,
                _WAKE_POST_MAX_ATTEMPTS,
                parent_id,
                retryable,
                exc,
                extra={"session_id": runner_primary_session_id()},
            )
            if last_attempt or not retryable:
                return False
            delay_s = min(
                _WAKE_POST_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)),
                _WAKE_POST_RETRY_MAX_DELAY_S,
            )
            await _wake_retry_sleep(delay_s)
    return False


def _subagent_delivery_not_confirmed_response(
    ack: _SubagentDeliveryAck,
    *,
    is_runner_known_subagent: bool,
) -> JSONResponse | None:
    """
    Build a 503 response when a known sub-agent result was not delivered.

    Top-level sessions also post terminal status but have no parent inbox, so
    an untracked status remains a no-op unless the runner knows this session
    was created as a sub-agent. For known sub-agents, Omnigent must not receive a
    2xx acknowledgement unless the terminal payload is confirmed in the
    parent's inbox — except a tracked entry whose parent is itself a
    sub-agent, which ``post_session_events`` acknowledges before calling here;
    the entry is retained and delivered only if this runner ever creates that
    parent's inbox.

    :param ack: Delivery acknowledgement returned by
        ``mark_subagent_work_terminal``.
    :param is_runner_known_subagent: Whether runner session state identifies
        the status sender as a sub-agent child.
    :returns: A 503 JSON response when delivery is not confirmed, or ``None``
        when the status can be acknowledged.
    """
    if ack.delivered:
        return None
    if ack.reason == _SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH:
        # A delayed op intentionally dropped because a newer send replaced the
        # dispatch: acknowledge so the forwarder does not retry, and never touch
        # the newer dispatch. Not a delivery failure.
        return None
    if ack.entry is None and not is_runner_known_subagent:
        return None
    reason = _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY if ack.entry is None else ack.reason
    detail_by_reason = {
        _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY: (
            "Sub-agent terminal status arrived, but the runner has no "
            "tracked work entry to deliver to the parent inbox."
        ),
        _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX: (
            "Sub-agent terminal status arrived, but the parent inbox is missing on this runner."
        ),
    }
    detail = detail_by_reason[reason]
    return JSONResponse(
        status_code=503,
        content={
            "error": "subagent_delivery_not_confirmed",
            "reason": reason,
            "detail": detail,
        },
    )


def _format_subagent_wake_notice(
    *,
    agent: str,
    title: str,
    status: str,
    pending: int,
    placement_label: str | None = None,
) -> str:
    """
    Build the framework notice that wakes a parent after a child finishes.

    :param agent: Sub-agent name from the parent spec, e.g. ``"researcher"``.
    :param title: Child instance title supplied at dispatch, e.g. ``"auth"``.
    :param status: Terminal child status, e.g. ``"completed"``, ``"failed"``,
        or ``"cancelled"``.
    :param pending: Number of undrained items in the parent inbox, e.g. ``3``.
    :param placement_label: Where the child ran, e.g. ``"fn · ~/projects/app"``;
        appended in brackets after the identity when known.
    :returns: A ``[System: ...]`` notice string, e.g. ``"[System: sub-agent
        researcher/auth finished (completed) — 1 result waiting in inbox. Call
        sys_read_inbox to collect.]"``.
    """
    noun = "result" if pending == 1 else "results"
    identity = f"{agent}/{title}"
    if placement_label:
        identity = f"{identity} [{placement_label}]"
    return (
        f"[System: sub-agent {identity} finished ({status}) — "
        f"{pending} {noun} waiting in inbox. Call sys_read_inbox to collect.]"
    )


# Max length of a child message preview mirrored to the parent stream.
# Matches the server-side ``_latest_message_preview`` truncation so the
# live runner-pushed preview and the snapshot preview look the same.
_CHILD_PREVIEW_MAX_CHARS = 150


@dataclasses.dataclass
class _ChildParentMeta:
    """Fan-out metadata for one child sub-agent session.

    Lets the runner mirror a child's status/preview deltas onto the
    PARENT's SSE stream — the child's own relay isn't running when only
    the parent is viewed, and the runner runs the child turn (affinity).

    :param parent_id: Parent session id whose stream receives the deltas.
    :param title: Child title ``"{tool}:{session_name}"`` — carried in
        status deltas so even a cold update has a display name.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    :param last_busy: Last busy value fanned out, used to coalesce
        duplicate status deltas. ``None`` until first publish.
    :param last_task_status: Last child-rail task status fanned out, e.g.
        ``"completed"``. Tracked separately so ``idle`` → ``failed`` emits
        even though both states are non-busy.
    :param last_error: Last child failure detail fanned out, used to emit a
        new parent update when only the error changes, and to clear stale
        errors on a later running/waiting edge.
    """

    parent_id: str
    title: str
    tool: str
    session_name: str
    last_busy: bool | None = None
    last_task_status: str | None = None
    last_error: tuple[str, str] | None = None


# child_session_id -> :class:`_ChildParentMeta`. Populated at spawn (see
# tool_dispatch._execute_subagent_tool), dropped when the child ends.
_child_session_parents: dict[str, _ChildParentMeta] = {}


def register_child_session(
    child_session_id: str,
    *,
    parent_session_id: str,
    title: str,
    tool: str,
    session_name: str,
) -> None:
    """
    Record a child→parent mapping for SSE status/preview fan-out.

    :param child_session_id: Child session id, e.g. ``"conv_child123"``.
    :param parent_session_id: Parent session id whose stream should
        receive the child's deltas, e.g. ``"conv_parent987"``.
    :param title: Child title, ``"{tool}:{session_name}"``.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    """
    _child_session_parents[child_session_id] = _ChildParentMeta(
        parent_id=parent_session_id,
        title=title,
        tool=tool,
        session_name=session_name,
    )


def unregister_child_session(child_session_id: str) -> None:
    """
    Drop a child→parent mapping when the child session ends.

    :param child_session_id: Child session id to forget.
    """
    _child_session_parents.pop(child_session_id, None)


def _session_status_to_task_status(status: object) -> str | None:
    """
    Map a ``session.status`` value to a child summary ``current_task_status``.

    The two vocabularies differ (session status vs. task status); this
    keeps the child rail's status text roughly in sync as ``busy`` flips.

    :param status: A ``session.status`` value, e.g. ``"running"``.
    :returns: ``"launching"`` / ``"in_progress"`` / ``"completed"`` /
        ``"failed"`` / ``"cancelled"`` / ``"stopped"`` / ``"killed"``, or
        ``None`` for an unrecognized status (caller omits the field).
    """
    if status == "launching":
        return "launching"
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status in ("failed", "cancelled"):
        return str(status)
    if status in ("completed", "stopped", "killed"):
        return status
    return None


def _truncate_child_preview(text: str) -> str:
    """
    Truncate a child message preview to the cap with an ellipsis.

    Matches the server-side ``_latest_message_preview`` truncation so the
    live runner-pushed preview and the snapshot preview look the same.

    :param text: The child's latest assistant reply text.
    :returns: ``text`` truncated to :data:`_CHILD_PREVIEW_MAX_CHARS` with
        a trailing ellipsis when longer, else ``text`` unchanged.
    """
    if len(text) > _CHILD_PREVIEW_MAX_CHARS:
        return text[:_CHILD_PREVIEW_MAX_CHARS].rstrip() + "…"
    return text


# Module-level ref to _session_inboxes. Populated inside create_runner_app;
# used by the sub-agent work registry to deliver completions to the parent.
_session_inboxes_ref: dict[str, asyncio.Queue[_JsonObject]] = {}
