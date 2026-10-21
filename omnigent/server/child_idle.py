"""Threshold-based child reminders, batched per mother with opt-in recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.runner.launch_failure import classify_native_turn_error
from omnigent.runtime import turn_activity
from omnigent.runtime.pending_elicitations import snapshot_for
from omnigent.server.routes._sessions.helpers import (
    SessionLiveness,
    _child_summary_identity,
    _effective_placement,
    _inherited_placement,
    _latest_assistant_tail,
    _session_status_from_cache,
)
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.server.session_overrides import (
    AGENT_TEMPLATE_ID_LABEL,
    harness_for_session,
    resolve_override,
)
from omnigent.server.user_preferences_store import (
    ChildFreezeSettings,
    ChildIdleSettings,
    read_child_idle_settings,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

_logger = logging.getLogger(__name__)
CHILD_IDLE_LABEL = "omnigent.child_idle"
CHILD_IDLE_BATCH_LABEL = "omnigent.child_idle_batch"
CHILD_RESUME_LABEL = "omnigent.child_idle_resume"
_BATCH_LEASE_S = 300
_POST_TIMEOUT_S = 120
NoticeOutcome = Literal["accepted", "deferred", "rejected", "uncertain"]
_MIRRORS = {"claude-code-native-ui-subagent", "codex-native-ui-subagent"}


def transient_child_error(conv: Conversation) -> bool:
    error_runner = conv.labels.get("omnigent.last_task_error_runner_id")
    if error_runner and error_runner != conv.runner_id:
        return False
    code = conv.labels.get("omnigent.last_task_error_code", "").lower()
    message = conv.labels.get("omnigent.last_task_error_message", "").lower()
    classified = classify_native_turn_error(code or "native_turn_error", message)
    return classified in {
        "model_at_capacity",
        "overloaded",
        "approval_service_timeout",
        "transient_upstream_error",
        "rate_limit_exceeded",
    } or any(
        phrase in message
        for phrase in (
            "model is at capacity",
            "model at capacity",
            "approval service timeout",
            "approval-service timeout",
            "approval service timed out",
        )
    )


def batch_claim_valid(parent: Conversation, claim: str, now: int) -> bool:
    """A stale replica cannot post after another observer takes its batch lease."""
    if parent.labels.get(CHILD_IDLE_BATCH_LABEL) != claim:
        return False
    try:
        value = json.loads(claim)
        return isinstance(value, dict) and type(value.get("until")) is int and value["until"] > now
    except (ValueError, TypeError):
        return False


def has_pending_card(conv: Conversation) -> bool:
    """Any own pending card blocks reminders, including asynchronous questions."""
    try:
        events = snapshot_for(conv.id)
    except Exception:  # noqa: BLE001 — an unreadable card index must suppress reminders
        return True
    own = []
    for event in events:
        params = event.get("params")
        if not isinstance(params, dict):
            return True
        if not params.get("target_session_id"):
            own.append(event)
    return bool(own) or bool(conv.pending_elicitation_count)


@dataclass
class _IdleState:
    turn: str | None
    since: int
    queued: int = 0
    attempted: int = 0
    signal: str = "idle"
    activity: str = ""
    checked: int = 0

    @classmethod
    def parse(cls, raw: str | None) -> _IdleState | None:
        try:
            data = json.loads(raw or "null")
            if not isinstance(data, dict):
                return None
            if not all(type(data.get(k)) is int and data[k] >= 0 for k in ("i", "q", "n")):
                return None
            turn = data.get("t")
            if turn is not None and not isinstance(turn, str):
                return None
            signal, activity, checked = data.get("s", "idle"), data.get("a", ""), data.get("c", 0)
            if (
                not isinstance(signal, str)
                or not isinstance(activity, str)
                or type(checked) is not int
            ):
                return None
            return cls(turn, data["i"], data["q"], data["n"], signal, activity, checked)
        except (ValueError, TypeError):
            return None

    def label(self) -> str:
        return json.dumps(
            {
                "t": self.turn,
                "i": self.since,
                "q": self.queued,
                "n": self.attempted,
                "s": self.signal,
                "a": self.activity,
                "c": self.checked,
            },
            separators=(",", ":"),
        )


class ChildIdleSweeper:
    """Observe only mothers whose runner tunnel belongs to this replica."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        owns_parent: Callable[[Conversation], bool],
        post_notice: Callable[[str, str, str], Awaitable[NoticeOutcome]],
        liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
        resume_child: Callable[[Conversation, str], Awaitable[NoticeOutcome]] | None = None,
        clock: Callable[[], int] = now_epoch,
        interval: float = 60,
    ) -> None:
        self._store = conversation_store
        self._permissions = permission_store
        self._owns_parent = owns_parent
        self._post_notice = post_notice
        self._liveness_lookup = liveness_lookup
        self._resume_child = resume_child
        self._clock = clock
        self._interval = interval
        self._app: Any = None
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()

    async def start(self, app: Any) -> None:
        if self._task is None or self._task.done():
            self._app = app
            self._task = asyncio.create_task(self._run(), name="child-idle-sweeper")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Child-idle sweep failed")
            await asyncio.sleep(self._interval)

    def _children(self) -> list[Conversation]:
        children: list[Conversation] = []
        after = None
        while True:
            page = self._store.list_conversations(
                kind="sub_agent", include_archived=False, limit=200, after=after
            )
            children.extend(page.data)
            if not page.has_more or page.last_id is None:
                return children
            after = page.last_id

    def _parent(self, conv: Conversation) -> Conversation | None:
        if conv.parent_conversation_id is None:
            return None
        parent = self._store.get_conversation(conv.parent_conversation_id)
        current = parent
        seen = {conv.id}
        for _ in range(32):
            if current is None or current.id in seen or current.archived:
                return None
            if is_session_closed(current.labels, current.title):
                return None
            seen.add(current.id)
            if current.parent_conversation_id is None:
                return parent if parent is not None and self._owns_parent(parent) else None
            current = self._store.get_conversation(current.parent_conversation_id)
        return None

    def _running_has_open_tool(self, conv: Conversation) -> bool:
        live = turn_activity.snapshot_for(conv.id)
        return live is None or not live.active or bool(live.tools)

    def _freeze_for(
        self,
        conv: Conversation,
        settings: ChildIdleSettings,
        harness_cache: dict[str, str | None],
    ) -> ChildFreezeSettings:
        return resolve_override(
            settings.agents,
            settings.freeze_default,
            template_id=conv.labels.get(AGENT_TEMPLATE_ID_LABEL),
            agent_id=conv.agent_id,
            harness=harness_for_session(conv, harness_cache),
        )

    def _signal(
        self, conv: Conversation, settings: ChildIdleSettings, row: ChildFreezeSettings
    ) -> str | None:
        status = _session_status_from_cache(conv.id, conv.live_status)
        if row.enabled:
            try:
                live = (
                    self._liveness_lookup([conv.id]).get(conv.id)
                    if self._liveness_lookup
                    else None
                )
            except Exception:  # noqa: BLE001 — unknown reachability suppresses this observation
                _logger.warning("Child runner liveness unavailable for %s", conv.id, exc_info=True)
                return None
            if live is not None and live.runner_online is False:
                return "runner_offline"
            settled = conv.live_status in ("idle", "failed") and status in ("idle", "failed")
            if settled and transient_child_error(conv):
                return "transient_error"
            if (
                conv.live_status == status == "running"
                and live is not None
                and live.runner_online is True
            ):
                return "tool_running" if self._running_has_open_tool(conv) else "running_quiet"
        if (
            settings.enabled
            and conv.live_status in ("idle", "failed")
            and status in ("idle", "failed")
        ):
            return "idle"
        return None

    def _observe(
        self,
        conv: Conversation,
        settings: ChildIdleSettings,
        now: int,
        harness_cache: dict[str, str | None],
    ) -> _IdleState | None:
        fresh = self._store.get_conversation(conv.id)
        if fresh is None or self._parent(fresh) is None:
            return None
        conv = fresh
        raw = conv.labels.get(CHILD_IDLE_LABEL)
        state = _IdleState.parse(raw)
        row = self._freeze_for(conv, settings, harness_cache)
        eligible = (
            (settings.enabled or row.enabled)
            and not conv.archived
            and conv.labels.get("omnigent.wrapper") not in _MIRRORS
            and not is_session_closed(conv.labels, conv.title)
            and not has_pending_card(conv)
        )
        live = turn_activity.snapshot_for(conv.id)
        turn = conv.labels.get(RUNNING_SINCE_LABEL_KEY)
        activity = hashlib.sha256(
            f"{conv.updated_at}:{conv.live_status}:"
            f"{_session_status_from_cache(conv.id, conv.live_status)}:"
            f"{conv.labels.get('omnigent.last_task_error_code', '')}:"
            f"{conv.labels.get('omnigent.last_task_error_message', '')}:"
            f"{live.updated if live and live.active else ''}".encode()
        ).hexdigest()[:16]
        if (
            eligible
            and row.enabled
            and state is not None
            and state.signal != "idle"
            and state.turn == turn
            and state.activity == activity
            and 0 <= now - state.checked < row.check_interval_s
            and not state.queued
        ):
            return None
        signal = self._signal(conv, settings, row) if eligible else None
        eligible = eligible and signal is not None
        if not eligible:
            if raw is not None:
                self._store.compare_and_set_label(conv.id, CHILD_IDLE_LABEL, raw, None)
            return None
        if signal == "idle":
            activity = ""
        if (
            state is None
            or state.turn != turn
            or state.since > now
            or state.signal != signal
            or state.activity != activity
        ):
            state = _IdleState(turn, now, signal=signal or "idle", activity=activity)
        state.checked = now
        threshold = settings.idle_threshold_s if signal == "idle" else row.freeze_threshold_s
        if state.attempted:
            due = settings.repeat_s > 0 and now >= state.attempted + settings.repeat_s
        else:
            due = now >= state.since + threshold
        if signal == "tool_running":
            due = False
        if not due:
            state.queued = 0
        if due and not state.queued:
            state.queued = (
                state.attempted + settings.repeat_s if state.attempted else state.since + threshold
            )
        label = state.label()
        if raw is None:
            if self._store.insert_label_if_absent(conv.id, CHILD_IDLE_LABEL, label) != label:
                return None
        elif raw != label and not self._store.compare_and_set_label(
            conv.id, CHILD_IDLE_LABEL, raw, label
        ):
            return None
        return state

    async def _tick(self) -> None:
        async with self._tick_lock:
            harness_cache: dict[str, str | None] = {}
            groups: dict[str, list[Conversation]] = {}
            settings_by_owner: dict[str, ChildIdleSettings] = {}
            settings_by_parent: dict[str, ChildIdleSettings] = {}
            prefs = getattr(self._app.state, "user_preferences_store", None)
            for child in await asyncio.to_thread(self._children):
                try:
                    parent = await asyncio.to_thread(self._parent, child)
                    if parent is None:
                        continue
                    owner = await asyncio.to_thread(
                        collab_owner_for, parent, self._store, self._permissions
                    )
                    if owner not in settings_by_owner:
                        settings_by_owner[owner] = await asyncio.to_thread(
                            read_child_idle_settings, prefs, owner
                        )
                    settings = settings_by_owner[owner]
                    if (
                        settings.enabled
                        or settings.freeze_default.enabled
                        or any(row.enabled for row in settings.agents.values())
                        or CHILD_IDLE_LABEL in child.labels
                        or CHILD_RESUME_LABEL in child.labels
                    ):
                        groups.setdefault(parent.id, []).append(child)
                        settings_by_parent[parent.id] = settings
                except Exception:
                    _logger.exception("Could not inspect idle child %s", child.id)
            for parent_id, children in groups.items():
                settings = settings_by_parent[parent_id]
                try:
                    await self._notify(parent_id, children, settings, harness_cache)
                except Exception:
                    _logger.exception("Could not notify mother %s of idle children", parent_id)

    async def _notify(
        self,
        parent_id: str,
        children: list[Conversation],
        settings: ChildIdleSettings,
        harness_cache: dict[str, str | None],
    ) -> None:
        claim = await asyncio.to_thread(self._claim_batch, parent_id)
        if claim is None:
            return
        try:
            await self._notify_claimed(parent_id, children, settings, claim, harness_cache)
        finally:
            await asyncio.to_thread(
                self._store.compare_and_set_label, parent_id, CHILD_IDLE_BATCH_LABEL, claim, None
            )

    def _claim_batch(self, parent_id: str) -> str | None:
        parent = self._store.get_conversation(parent_id)
        if parent is None or parent.archived or not self._owns_parent(parent):
            return None
        now = self._clock()
        raw = parent.labels.get(CHILD_IDLE_BATCH_LABEL)
        if raw is not None and batch_claim_valid(parent, raw, now):
            return None
        claim = json.dumps({"id": secrets.token_hex(8), "until": now + _BATCH_LEASE_S})
        if raw is None:
            stored = self._store.insert_label_if_absent(parent_id, CHILD_IDLE_BATCH_LABEL, claim)
            return claim if stored == claim else None
        if self._store.compare_and_set_label(parent_id, CHILD_IDLE_BATCH_LABEL, raw, claim):
            return claim
        return None

    async def _notify_claimed(
        self,
        parent_id: str,
        children: list[Conversation],
        settings: ChildIdleSettings,
        claim: str,
        harness_cache: dict[str, str | None],
    ) -> None:
        now = self._clock()
        current: list[tuple[Conversation, _IdleState]] = []
        for child in children:
            fresh = await asyncio.to_thread(self._store.get_conversation, child.id)
            if fresh is None or fresh.parent_conversation_id != parent_id:
                continue
            parent = await asyncio.to_thread(self._parent, fresh)
            if parent is None:
                continue
            state = await asyncio.to_thread(self._observe, fresh, settings, now, harness_cache)
            if state is not None and state.queued:
                row = await asyncio.to_thread(self._freeze_for, fresh, settings, harness_cache)
                if (
                    state.signal == "transient_error"
                    and row.resume_once
                    and self._resume_child is not None
                    and CHILD_RESUME_LABEL not in fresh.labels
                ):
                    # One automatic attempt per child, across turns and replicas.
                    reservation = secrets.token_hex(8)
                    stored = await asyncio.to_thread(
                        self._store.insert_label_if_absent,
                        fresh.id,
                        CHILD_RESUME_LABEL,
                        reservation,
                    )
                    if stored == reservation:
                        await asyncio.wait_for(
                            self._resume_child(fresh, claim), timeout=_POST_TIMEOUT_S
                        )
                        # Re-observe next tick: the post may already have started a turn.
                        continue
                current.append((fresh, state))
        if not current:
            return
        if now < min(state.queued for _, state in current) + settings.merge_window_s:
            return
        items = await asyncio.to_thread(
            self._store.list_latest_message_items_for_conversations,
            [child.id for child, _ in current],
        )
        lines: dict[str, str] = {}
        for child, state in current:
            agent, _harness = _child_summary_identity(child, None, {})
            placement = _effective_placement(child, _inherited_placement(self._store, parent_id))
            tail = _latest_assistant_tail(items.get(child.id, []), limit_chars=400)
            lines[child.id] = json.dumps(
                {
                    "session_id": child.id,
                    "title": title_without_closed_marker(child.title) or child.id,
                    "agent": agent,
                    "host": placement.host_id,
                    "directory": placement.cwd,
                    "status": _session_status_from_cache(child.id, child.live_status),
                    "idle_minutes": max(0, now - state.since) // 60,
                    "signal": state.signal,
                    "signal_minutes": max(0, now - state.since) // 60,
                    "resume_attempted": CHILD_RESUME_LABEL in child.labels,
                    "last_assistant_reply_tail": tail,
                },
                ensure_ascii=False,
            )
        previous: list[tuple[str, str, str]] = []
        # Reserve the attempt before posting: a lost response must not wake the
        # mother again at the next tick. Definite non-delivery alone refunds it.
        for child, state in current:
            old_label = state.label()
            state.attempted, state.queued = now, 0
            reserved = state.label()
            if await asyncio.to_thread(
                self._store.compare_and_set_label, child.id, CHILD_IDLE_LABEL, old_label, reserved
            ):
                previous.append((child.id, old_label, reserved))
        if not previous:
            return
        try:
            text = "\n".join(
                ["[System: idle child check-in]"] + [lines[sid] for sid, _, _ in previous]
            )
            outcome = await asyncio.wait_for(
                self._post_notice(parent_id, text, claim), timeout=_POST_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — retain the attempt budget after uncertain delivery
            _logger.warning("Idle-child notice outcome uncertain for %s", parent_id, exc_info=True)
            return
        if outcome in ("deferred", "rejected"):
            for child_id, label, reserved in previous:
                await asyncio.to_thread(
                    self._store.compare_and_set_label, child_id, CHILD_IDLE_LABEL, reserved, label
                )
