"""Keep-warm sweeper — one quiet turn per provider cache window.

Idle children in the active zone lose their provider prompt cache when
the cache window (Claude 55 min, Codex 25 min) elapses, so the next real
turn pays a full prompt rewrite. This background loop keeps an eligible
child warm by posting one short ``[quiet]`` turn at last-cache-touch +
interval, for at most the configured cap after the child's last real
turn. It runs server-side (independent of any browser or mother
connection) and gates every ping on the owner's collaboration settings.

State lives in one compact-JSON conversation label (``omnigent.keep_warm``)
so it survives restarts and rides the existing label machinery — no new
table, no migration. A ping's reply is silence by contract: the child's
last assistant line must be ``[quiet]``; anything else counts as a
failure, so a non-complying child costs at most three mother wakes before
warming pauses. Two consecutive readings that miss the prompt cache pause
warming too (a harness whose cache is shorter than the assumed window).

Warming follows the child's direct parent: a child is kept warm only while
its parent has recent life — a running turn that is not the parent's own
keep-warm ping, or a real turn inside the parent's own interval. An absent
or unreadable parent turns the child cold (``why = mom``) with no notice,
because the notice itself would wake the absent mother.

The loop is shaped like :class:`~omnigent.server.peer_sweeper.PeerSweeper`:
``start``/``shutdown`` own one task, ``_run`` loops ``_tick`` plus
``asyncio.sleep`` swallowing errors, and tests drive ``_tick`` directly
with an injected clock. Notices to the mother reuse the peer sweeper's
``notify_line`` so they park, batch and drop for archived mothers exactly
like every other server notice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from starlette.requests import Request

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.entities.conversation import ConversationItem, MessageData
from omnigent.native.native_coding_agents import NativeCodingAgent, public_agent_name
from omnigent.server.routes._sessions.common import (
    _KEEP_WARM_LABEL_KEY,
    _LAST_CACHE_LABEL_KEY,
    _session_status_cache,
)
from omnigent.server.routes._sessions.helpers import (
    Placement,
    SessionLiveness,
    _child_summary_identity,
    _effective_placement,
    _inherited_placement,
    _native_coding_agent_for_session,
    _publish_child_status_to_parent,
    _session_status_from_cache,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.server.user_preferences_store import (
    CollabSettings,
    clamp_keep_warm,
    read_collab_settings,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

_logger = logging.getLogger(__name__)

KEEP_WARM_LABEL = _KEEP_WARM_LABEL_KEY
LAST_CACHE_LABEL = _LAST_CACHE_LABEL_KEY

#: The one line a keep-warm turn asks the child to answer with.
PING = (
    "[System: keep-warm check from Omnigent. No action is needed; reply with only "
    "this line: [quiet]]"
)

_QUIET_LINE = "[quiet]"
_SLACK_S = 300
_TICK_INTERVAL_S = 60.0
_PING_TIMEOUT_S = 120.0
_PING_TURN_MAX_S = 180
_RUNNING_SINCE_LEAD_S = 5
_SETTLE_USAGE_GRACE_S = 60
_FAILURE_PAUSE_THRESHOLD = 3
_MISS_PAUSE_THRESHOLD = 2
_PAGE_LIMIT = 200
_MAX_PAGES = 100
_MAX_PARENT_HOPS = 32
_CANDIDATE_WINDOW_S = 59 * 60 + _SLACK_S
#: Only Claude and Codex children have a provider cache worth keeping.
_SUPPORTED_HARNESSES: dict[str, str] = {
    "claude-native": "claude",
    "codex-native": "codex",
}
#: Reasons a cold / paused label may carry: ``mom`` = the parent went away.
_WHY_CODES = frozenset({"cap", "exp", "fail", "pol", "miss", "rev", "mom"})
#: Mirrored native sub-agent rows describe another harness's internal
#: sub-agent, not an Omnigent session with its own cache.
_MIRRORED_WRAPPER_LABELS = frozenset(
    {"claude-code-native-ui-subagent", "codex-native-ui-subagent"}
)
_WRAPPER_LABEL_KEY = "omnigent.wrapper"
#: Placement stand-in for a child whose parent pointer is missing.
_EMPTY_PLACEMENT = Placement(None, None, None)


def _as_int(value: object) -> int | None:
    """Return *value* when it is a genuine int (never a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass
class _WarmState:
    """
    One child's parsed ``omnigent.keep_warm`` label.

    Short keys keep the serialized label well under the 256-character
    label bound: ``s`` state (``w`` warm / ``c`` cold / ``p`` paused),
    ``why`` reason, ``t`` last-seen ``running_since``, ``c`` episode start,
    ``u`` last cache touch, ``p`` ping attempt time, ``q`` pending flag,
    ``f`` failures, ``m`` misses, ``b`` usage baseline, ``v``
    ``archive_revision`` at a miss pause, ``w`` warm-until.

    :param s: Episode state.
    :param why: Reason for a cold / paused state — one of ``cap``, ``exp``,
        ``fail``, ``pol``, ``miss``, ``rev``, ``mom``.
    :param t: ``running_since`` of the last turn seen.
    :param c: Epoch seconds the last real turn was seen settled.
    :param u: Epoch seconds the last turn (real or ping) was seen settled.
    :param p: Epoch seconds of the current ping attempt.
    :param q: ``True`` while that attempt is pending.
    :param f: Consecutive failures.
    :param m: Consecutive observed cache misses.
    :param b: Usage baseline at ping time — the Claude reading's
        ``observed_at``, or Codex's cumulative ``[input, cached]``.
    :param v: ``archive_revision`` at a miss pause.
    :param w: Epoch seconds the current interval window ends.
    """

    s: str
    why: str | None = None
    t: int | None = None
    c: int | None = None
    u: int | None = None
    p: int | None = None
    q: bool = False
    f: int = 0
    m: int = 0
    b: int | list[int] | None = None
    v: int | None = None
    w: int | None = None

    def to_label(self) -> str:
        """Serialize to the compact JSON label value."""
        data: dict[str, object] = {"s": self.s}
        if self.why is not None:
            data["why"] = self.why
        if self.t is not None:
            data["t"] = self.t
        if self.c is not None:
            data["c"] = self.c
        if self.u is not None:
            data["u"] = self.u
        if self.p is not None:
            data["p"] = self.p
        if self.q:
            data["q"] = 1
        if self.f:
            data["f"] = self.f
        if self.m:
            data["m"] = self.m
        if self.b is not None:
            data["b"] = self.b
        if self.v is not None:
            data["v"] = self.v
        if self.w is not None:
            data["w"] = self.w
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def parse(cls, raw: str | None) -> _WarmState | None:
        """
        Parse a stored label, tolerating malformed or partial values.

        :param raw: Raw label value, or ``None``.
        :returns: The parsed state, or ``None`` when unusable.
        """
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        state = data.get("s")
        if state not in ("w", "c", "p"):
            return None
        why = data.get("why")
        baseline = data.get("b")
        if (
            not isinstance(baseline, (int, list))
            or isinstance(baseline, bool)
            or (isinstance(baseline, list) and not all(_as_int(v) is not None for v in baseline))
        ):
            baseline = None
        return cls(
            s=state,
            why=why if isinstance(why, str) and why in _WHY_CODES else None,
            t=_as_int(data.get("t")),
            c=_as_int(data.get("c")),
            u=_as_int(data.get("u")),
            p=_as_int(data.get("p")),
            q=bool(data.get("q")),
            f=_as_int(data.get("f")) or 0,
            m=_as_int(data.get("m")) or 0,
            b=baseline,
            v=_as_int(data.get("v")),
            w=_as_int(data.get("w")),
        )


def warm_state_from_label(
    raw: str | None,
    *,
    archived: bool,
    harness: str | None,
    busy: bool,
    now: int,
) -> Literal["warm", "cold"] | None:
    """
    Derive the rail pill state from a child's keep-warm label.

    No settings read: an archived child, an unsupported harness, or no
    label reads ``None``. A warm label stays ``warm`` while the child is
    busy (its next turn will touch the prompt anyway) or the window has not
    passed; anything else is ``cold``.

    :param raw: The ``omnigent.keep_warm`` label value, or ``None``.
    :param archived: Whether the child row itself is archived.
    :param harness: The child's canonical harness, e.g. ``"claude-native"``.
    :param busy: Whether the child's status is ``running`` / ``waiting``.
    :param now: Current epoch seconds.
    :returns: ``"warm"``, ``"cold"``, or ``None``.
    """
    if archived or harness not in _SUPPORTED_HARNESSES:
        return None
    state = _WarmState.parse(raw)
    if state is None:
        return None
    if state.s == "w" and (busy or (state.w is not None and now <= state.w)):
        return "warm"
    return "cold"


class ChildKeepWarmSweeper:
    """Post one quiet turn per cache window for eligible active-zone children."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
        post_event_impl: Callable[..., Awaitable[Any]],
        notify_line: Callable[[str, str], Awaitable[None]],
        interval: float = _TICK_INTERVAL_S,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        """
        :param conversation_store: Store for child rows and label writes.
        :param permission_store: Permission store for owner resolution
            (``None`` in single-user local mode).
        :param liveness_lookup: Bulk session-liveness lookup — the ping idle
            gate reads ``runner_online`` from it, never ``_true_state``.
        :param post_event_impl: The raw events-post callable the ping uses.
        :param notify_line: ``PeerSweeper.notify_line`` — the mother's parked
            notice queue.
        :param interval: Seconds between ticks.
        :param clock: Epoch-seconds clock, overridable for tests.
        """
        self._conversation_store = conversation_store
        self._permission_store = permission_store
        self._liveness_lookup = liveness_lookup
        self._post_event_impl = post_event_impl
        self._notify_line = notify_line
        self._interval = interval
        self._clock = clock
        # child_id -> warm_until for every label that says state = warm.
        self._tracked: dict[str, int] = {}
        # child_id -> post outcome recorded by the ping task, applied on the
        # next tick so a slow post never stalls the loop.
        self._outcomes: dict[str, str] = {}
        # child_id -> tick time the ping's turn was first seen settled, so
        # the cache check waits for the usage post to land.
        self._settled_seen: dict[str, int] = {}
        self._ping_tasks: set[asyncio.Task[None]] = set()
        self._app: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, app: Any) -> None:
        """Seed the tracked warm set, then start the loop."""
        if self._task is not None and not self._task.done():
            return
        self._app = app
        await self._seed_tracked()
        self._task = asyncio.create_task(self._run(), name="child-keep-warm")

    async def shutdown(self) -> None:
        """Stop the loop and any in-flight ping posts."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for ping_task in list(self._ping_tasks):
            ping_task.cancel()
        if self._ping_tasks:
            await asyncio.gather(*self._ping_tasks, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Child keep-warm tick failed; retrying later")
            await asyncio.sleep(self._interval)

    async def _seed_tracked(self) -> None:
        """Seed the tracked map from non-archived rows carrying a warm label."""
        try:
            children = await asyncio.to_thread(self._scan_labeled_children)
        except Exception:
            _logger.exception("Child keep-warm failed to seed its tracked set")
            return
        for conv in children:
            state = _WarmState.parse(conv.labels.get(KEEP_WARM_LABEL))
            if state is not None and state.s == "w":
                self._tracked[conv.id] = state.w or 0

    def _scan_labeled_children(self) -> list[Conversation]:
        """Page every non-archived sub-agent row for the startup seed."""
        children: list[Conversation] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._conversation_store.list_conversations(
                limit=_PAGE_LIMIT,
                after=after,
                kind="sub_agent",
                include_archived=False,
            )
            children.extend(page.data)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return children

    async def _tick(self) -> None:
        """One sweep over ping candidates plus tracked episodes; never raises."""
        assert self._app is not None, "ChildKeepWarmSweeper.start() must run before _tick()"
        now = self._clock()
        candidate_ids: set[str] = set()
        try:
            candidate_ids = await asyncio.to_thread(self._list_ping_candidates, now)
        except Exception:
            _logger.exception("Child keep-warm failed to list ping candidates")
        settings_cache: dict[str, CollabSettings] = {}
        agent_cache: dict[str, NativeCodingAgent | None] = {}
        parent_cache: dict[str, bool] = {}
        for child_id in sorted(candidate_ids | set(self._tracked)):
            try:
                await self._process_child(child_id, now, settings_cache, agent_cache, parent_cache)
            except Exception:
                _logger.exception("Child keep-warm failed to process child %s", child_id)

    def _list_ping_candidates(self, now: int) -> set[str]:
        """Return children updated inside the candidate window (paged)."""
        ids: set[str] = set()
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._conversation_store.list_conversations(
                limit=_PAGE_LIMIT,
                after=after,
                kind="sub_agent",
                include_archived=False,
                updated_after=now - _CANDIDATE_WINDOW_S,
                sort_by="updated_at",
            )
            ids.update(conv.id for conv in page.data)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return ids

    async def _process_child(
        self,
        child_id: str,
        now: int,
        settings_cache: dict[str, CollabSettings],
        agent_cache: dict[str, NativeCodingAgent | None],
        parent_cache: dict[str, bool],
    ) -> None:
        """Read one child fresh and apply the state-machine rules in order."""
        store = self._conversation_store
        conv = await asyncio.to_thread(store.get_conversation, child_id)
        if conv is None:
            self._drop_tracking(child_id)
            return

        raw_label = conv.labels.get(KEEP_WARM_LABEL)
        state = _WarmState.parse(raw_label)
        running_since = _running_since(conv)
        status = _session_status_from_cache(conv.id, conv.live_status)
        busy = status == "running"

        # Rule 1: the child itself archived — drop it; the pill reads None
        # and the label goes inert. An archived mother keeps the child
        # tracked so rule 7 can settle it cold silently.
        if conv.archived:
            self._drop_tracking(child_id)
            return

        agent = await self._native_agent_for(conv, agent_cache)
        mirrored = conv.labels.get(_WRAPPER_LABEL_KEY) in _MIRRORED_WRAPPER_LABELS
        inspectable = agent is not None and not mirrored
        harness = agent.harness if agent is not None else None
        family = _SUPPORTED_HARNESSES.get(harness or "")
        if not inspectable and state is None:
            self._drop_tracking(child_id)
            return

        warm_before = warm_state_from_label(
            raw_label, archived=False, harness=harness, busy=busy, now=now
        )
        # A label that said warm may already DERIVE cold once its window
        # passed (the rail still shows the last publish), so leaving a warm
        # label is itself a pill transition worth publishing.
        state_warm_before = state is not None and state.s == "w"

        owner = await asyncio.to_thread(collab_owner_for, conv, store, self._permission_store)
        settings = settings_cache.get(owner)
        if settings is None:
            preferences_store = getattr(
                getattr(self._app, "state", None), "user_preferences_store", None
            )
            settings = await asyncio.to_thread(read_collab_settings, preferences_store, owner)
            settings_cache[owner] = settings
        claude_interval, codex_interval, max_s = clamp_keep_warm(settings)
        switch_on = bool(settings.enabled and settings.keep_warm_enabled)
        interval = codex_interval if family == "codex" else claude_interval
        ancestor_archived = await asyncio.to_thread(self._ancestor_archived, conv)
        runner_online = self._runner_online(child_id)
        base_eligible = (
            inspectable
            and running_since is not None
            and not is_session_closed(conv.labels, conv.title)
            and not ancestor_archived
            and switch_on
            and runner_online is True
            and status == "idle"
        )

        notices: list[tuple[str, dict[str, int]]] = []
        # A failed attempt is retried on a LATER tick, never in the same pass
        # that recorded its failure.
        retry_deferred = False

        # A ping task's outcome applies on the tick after it was recorded.
        outcome = self._outcomes.pop(child_id, None)
        if state is not None and state.q and outcome is not None:
            if outcome == "denied":
                state.s, state.why, state.q = "p", "pol", False
                self._settled_seen.pop(child_id, None)
                notices.append(("pol", {}))
            elif outcome == "error":
                state.f += 1
                state.q = False
                retry_deferred = True
                self._settled_seen.pop(child_id, None)

        # Rule 2: a sticky miss pause lifts once the child left and
        # re-entered the active zone (its archive revision moved). The
        # watermark moves to the current turn so rule 3 only re-arms on a
        # later real turn, never on the cold cache the pause was about.
        if state is not None and state.s == "p" and state.why == "miss":
            if state.v != conv.archive_revision:
                state.s, state.why = "c", "rev"
                if running_since is not None:
                    state.t = running_since

        # Rule 3: a new real turn settled starts a fresh episode. Only the
        # not-yet-revised miss pause is sticky — a cold ``cap`` / ``mom``
        # label re-arms here.
        if inspectable and running_since is not None and status == "idle":
            ours = _turn_is_ours(state, running_since)
            new_episode = state is None or (running_since != state.t and not ours)
            if new_episode:
                sticky_miss = (
                    state is not None
                    and state.s == "p"
                    and state.why == "miss"
                    and state.v == conv.archive_revision
                )
                if not sticky_miss:
                    settle = conv.updated_at if conv.updated_at >= running_since else now
                    state = _WarmState(
                        s="w",
                        t=running_since,
                        c=settle,
                        u=settle,
                        w=settle + interval + _SLACK_S,
                    )
                    self._settled_seen.pop(child_id, None)

        # Rule 4: a pending attempt never posts again; it settles once the
        # ping's own turn is over and its usage post has had 60 s to land,
        # or fails after 180 s with no turn of ours having started.
        if state is not None and state.q and state.p is not None:
            ours = _turn_is_ours(state, running_since)
            if ours and status in ("idle", "failed") and running_since is not None:
                first_seen = self._settled_seen.setdefault(child_id, now)
                if now - first_seen >= _SETTLE_USAGE_GRACE_S:
                    self._settled_seen.pop(child_id, None)
                    notices.extend(
                        await self._settle_ping(conv, state, running_since, now, interval, family)
                    )
            elif not ours and now - state.p > _PING_TURN_MAX_S:
                state.f += 1
                state.q = False
                state.b = None
                retry_deferred = True
                self._settled_seen.pop(child_id, None)

        # Rule 6: repeated failures pause warming until the next real turn.
        if (
            state is not None
            and state.f >= _FAILURE_PAUSE_THRESHOLD
            and not (state.s == "p" and state.why == "fail")
        ):
            state.s, state.why = "p", "fail"
            notices.append(("fail", {}))

        # Rule 7 and 7b need the tracked flag; 7b, rule 8 and any notice
        # also need the direct parent's presence. A notice may never reach
        # an absent mother, so a child with collected notices is read too;
        # children the sweeper would not touch skip the parent read.
        tracked = child_id in self._tracked or (state is not None and state.s == "w")
        parent_present = True
        if base_eligible or tracked or notices:
            parent_present = await self._parent_present(
                conv, now, claude_interval, codex_interval, parent_cache, agent_cache
            )
        eligible = base_eligible and parent_present

        # Rule 7: cap and expiry settle tracked children whether or not they
        # are eligible to be pinged. An expired window under an archived
        # mother or a switched-off zone goes cold silently; an absent parent
        # turns cap / expiry into a silent ``mom`` cold.
        if state is not None and state.s == "w" and tracked:
            if state.c is not None and now - state.c >= max_s:
                state.s = "c"
                state.why = "cap" if parent_present else "mom"
                if parent_present:
                    notices.append(("cap", {"hours": max(1, round(max_s / 3600))}))
            elif state.w is not None and now > state.w and not busy:
                state.s = "c"
                state.why = "exp" if parent_present else "mom"
                if parent_present and not ancestor_archived and switch_on:
                    notices.append(("exp", {}))

        # Rule 7b: the mother is gone — stop paying to keep this child warm.
        # No notice: a notice would wake the absent mother.
        if state is not None and state.s == "w" and tracked and not busy and not parent_present:
            state.s, state.why = "c", "mom"

        # No notice may reach an absent parent (it would wake her): drop
        # every notice collected for this child this tick.
        if notices and not parent_present:
            notices = []

        # Rule 8: due — write the attempt into the label first, then post.
        ping = False
        if (
            eligible
            and state is not None
            and state.s == "w"
            and not state.q
            and not retry_deferred
        ):
            if state.u is not None and now >= state.u + interval:
                state.p = now
                state.q = True
                state.b = self._usage_baseline(conv, family)
                ping = True

        new_label = state.to_label() if state is not None else None
        if new_label is not None and new_label != raw_label:
            await asyncio.to_thread(store.set_labels, child_id, {KEEP_WARM_LABEL: new_label})
            warm_after = warm_state_from_label(
                new_label, archived=False, harness=harness, busy=busy, now=now
            )
            state_warm_after = state is not None and state.s == "w"
            if warm_after != warm_before or (state_warm_before and not state_warm_after):
                _publish_child_status_to_parent(child_id, None)
        if state is not None and state.s == "w":
            self._tracked[child_id] = state.w or 0
        else:
            self._tracked.pop(child_id, None)

        if notices:
            await self._send_notices(conv, notices)
        if ping:
            self._spawn_ping(child_id, owner)

    async def _settle_ping(
        self,
        conv: Conversation,
        state: _WarmState,
        running_since: int,
        now: int,
        interval: int,
        family: str | None,
    ) -> list[tuple[str, dict[str, int]]]:
        """Rule 5: evaluate one settled keep-warm turn, mutating *state*."""
        notices: list[tuple[str, dict[str, int]]] = []
        state.t = running_since
        settle = conv.updated_at if conv.updated_at >= running_since else now
        state.u = settle
        state.w = settle + interval + _SLACK_S
        state.q = False
        state.p = None
        complied = await asyncio.to_thread(self._ping_reply_complied, conv.id)
        turn_failed = _session_status_from_cache(conv.id, conv.live_status) == "failed"
        if turn_failed or not complied:
            state.f += 1
        else:
            state.f = 0
            reading = self._cache_reading(conv, state, running_since, family)
            if reading is not None:
                kind, read_tokens, creation_tokens = reading
                if kind == "miss":
                    state.m += 1
                    if state.m >= _MISS_PAUSE_THRESHOLD:
                        state.s, state.why = "p", "miss"
                        state.v = conv.archive_revision
                        notices.append(
                            ("miss", {"read": read_tokens, "creation": creation_tokens})
                        )
                else:
                    state.m = 0
        state.b = None
        return notices

    def _cache_reading(
        self,
        conv: Conversation,
        state: _WarmState,
        ping_running_since: int,
        family: str | None,
    ) -> tuple[str, int, int] | None:
        """Classify the settled ping's cache use; ``None`` is unknown."""
        if family == "claude":
            parsed = _parse_last_cache(conv.labels.get(LAST_CACHE_LABEL))
            if parsed is None:
                return None
            read, creation, observed_at = parsed
            baseline = state.b if isinstance(state.b, int) and not isinstance(state.b, bool) else 0
            if observed_at <= ping_running_since or observed_at <= baseline:
                return None
            return ("miss" if creation > read else "hit"), read, creation
        if family == "codex":
            baseline = state.b
            if not (
                isinstance(baseline, list)
                and len(baseline) == 2
                and all(_as_int(value) is not None for value in baseline)
            ):
                return None
            usage = conv.session_usage or {}
            current_input = _as_int(usage.get("input_tokens")) or 0
            current_cached = _as_int(usage.get("cache_read_input_tokens")) or 0
            delta_input = max(0, current_input - int(baseline[0]))
            delta_cached = max(0, current_cached - int(baseline[1]))
            total = delta_input + delta_cached
            if total <= 0:
                return None
            if delta_cached / total < 0.5:
                return "miss", delta_cached, delta_input
            return "hit", delta_cached, delta_input
        return None

    def _usage_baseline(self, conv: Conversation, family: str | None) -> int | list[int]:
        """Capture the ping-time baseline the next cache reading compares to."""
        if family == "codex":
            usage = conv.session_usage or {}
            return [
                _as_int(usage.get("input_tokens")) or 0,
                _as_int(usage.get("cache_read_input_tokens")) or 0,
            ]
        parsed = _parse_last_cache(conv.labels.get(LAST_CACHE_LABEL))
        return parsed[2] if parsed is not None else 0

    def _ping_reply_complied(self, child_id: str) -> bool:
        """Whether the ping's turn ended with the ``[quiet]`` line."""
        items_by_child = self._conversation_store.list_latest_message_items_for_conversations(
            [child_id], 10
        )
        text = _last_assistant_text(items_by_child.get(child_id, []))
        if text is None:
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return bool(lines) and lines[-1] == _QUIET_LINE

    async def _native_agent_for(
        self, conv: Conversation, cache: dict[str, NativeCodingAgent | None]
    ) -> NativeCodingAgent | None:
        """Resolve the child's native coding agent, cached for this tick."""
        if conv.id not in cache:
            agent = await asyncio.to_thread(_native_coding_agent_for_session, conv)
            cache[conv.id] = (
                agent if agent is not None and agent.harness in _SUPPORTED_HARNESSES else None
            )
        return cache[conv.id]

    def _ancestor_archived(self, conv: Conversation) -> bool:
        """Walk the parent chain for any archived ancestor (mother included)."""
        seen = {conv.id}
        current = conv
        for _ in range(_MAX_PARENT_HOPS):
            parent_id = current.parent_conversation_id
            if parent_id is None or parent_id in seen:
                return False
            parent = self._conversation_store.get_conversation(parent_id)
            if parent is None:
                return False
            if parent.archived:
                return True
            seen.add(parent_id)
            current = parent
        return False

    async def _parent_present(
        self,
        conv: Conversation,
        now: int,
        claude_interval: int,
        codex_interval: int,
        parent_cache: dict[str, bool],
        agent_cache: dict[str, NativeCodingAgent | None],
    ) -> bool:
        """
        Whether the child's direct parent shows recent life (design §2.14).

        A missing / unreadable parent row and an unknown parent status fail
        toward not spending. A running parent is present unless its turn is
        its own keep-warm ping; otherwise the parent is present while its
        last real turn is inside the parent's own harness interval.
        """
        parent_id = conv.parent_conversation_id
        if parent_id is None:
            return False
        if parent_id in parent_cache:
            return parent_cache[parent_id]
        present = False
        try:
            parent = await asyncio.to_thread(self._conversation_store.get_conversation, parent_id)
        except Exception:  # noqa: BLE001 — an unreadable parent fails toward cold
            parent = None
        if parent is not None and (
            _session_status_cache.get(parent.id) is not None or parent.live_status is not None
        ):
            status = _session_status_from_cache(parent.id, parent.live_status)
            state = _WarmState.parse(parent.labels.get(KEEP_WARM_LABEL))
            ours = state is not None and state.q and _turn_is_ours(state, _running_since(parent))
            if status == "running" and not ours:
                present = True
            else:
                last_real = (
                    state.c if state is not None and state.c is not None else parent.updated_at
                )
                agent = await self._native_agent_for(parent, agent_cache)
                family = _SUPPORTED_HARNESSES.get(agent.harness) if agent is not None else None
                interval = codex_interval if family == "codex" else claude_interval
                present = now - last_real <= interval
        parent_cache[parent_id] = present
        return present

    def _runner_online(self, child_id: str) -> bool | None:
        """Strict runner reachability; ``None`` when no lookup is wired."""
        if self._liveness_lookup is None:
            return None
        try:
            liveness = self._liveness_lookup([child_id]).get(child_id)
        except Exception:
            _logger.exception("Child keep-warm liveness lookup failed")
            return None
        return liveness.runner_online if liveness is not None else None

    def _drop_tracking(self, child_id: str) -> None:
        self._tracked.pop(child_id, None)
        self._outcomes.pop(child_id, None)
        self._settled_seen.pop(child_id, None)

    def _spawn_ping(self, child_id: str, owner: str) -> None:
        """Run the ping post in its own task so a slow post never stalls a tick."""
        task = asyncio.create_task(
            self._post_ping(child_id, owner), name=f"child-keep-warm-ping-{child_id}"
        )
        self._ping_tasks.add(task)
        task.add_done_callback(self._ping_tasks.discard)

    async def _post_ping(self, child_id: str, owner: str) -> None:
        """Post one keep-warm turn and record its outcome for the next tick."""
        outcome = "error"
        try:
            request = self._synthetic_request(child_id, self._app)
            result = await asyncio.wait_for(
                self._post_event_impl(
                    request,
                    child_id,
                    SessionEventInput(
                        type="message",
                        data={
                            "role": "user",
                            "content": [{"type": "input_text", "text": PING}],
                        },
                    ),
                    acting_user_id=owner,
                ),
                timeout=_PING_TIMEOUT_S,
            )
        except TimeoutError:
            # An ASK policy can stall the post; treat the timeout like a
            # denial so it pauses instead of retrying an unattended turn.
            outcome = "denied"
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Child keep-warm ping failed for %s", child_id)
        else:
            if isinstance(result, dict) and result.get("denied") is True:
                outcome = "denied"
            elif isinstance(result, dict) and result.get("queued") is False:
                outcome = "error"
            else:
                outcome = "forwarded"
        if outcome != "forwarded":
            self._outcomes[child_id] = outcome

    async def _send_notices(
        self, conv: Conversation, notices: list[tuple[str, dict[str, int]]]
    ) -> None:
        """Send one ``[System: ...]`` line per stop event to the mother."""
        parent_id = conv.parent_conversation_id
        if parent_id is None:
            return
        for kind, detail in notices:
            try:
                line = await asyncio.to_thread(self._format_notice, conv, kind, detail)
            except Exception:
                _logger.exception("Child keep-warm failed to format a notice for %s", conv.id)
                continue
            try:
                await self._notify_line(parent_id, line)
            except Exception:
                _logger.exception("Child keep-warm notice failed for %s", conv.id)

    def _format_notice(self, conv: Conversation, kind: str, detail: dict[str, int]) -> str:
        """Render one notice line naming the child's title, agent, host, cwd."""
        parent_id = conv.parent_conversation_id
        agent_name, _harness = _child_summary_identity(conv, None, {})
        inherited = (
            _inherited_placement(self._conversation_store, parent_id)
            if parent_id is not None
            else None
        )
        placement = _effective_placement(conv, inherited or _EMPTY_PLACEMENT)
        display = public_agent_name(agent_name) or conv.sub_agent_name or "agent"
        where = f"{display} · {placement.host_id or 'local'} · {placement.cwd or 'unknown'}"
        title = title_without_closed_marker(conv.title) or conv.id
        if kind == "cap":
            hours = detail.get("hours", 8)
            body = (
                f"reached the {hours} h limit; it is now cold. "
                "It restarts after the child's next real turn."
            )
            prefix = "keep-warm stopped"
        elif kind == "exp":
            body = (
                "its runner was offline past the keep-warm window; "
                "it restarts after the child's next real turn."
            )
            prefix = "keep-warm paused"
        elif kind == "fail":
            body = (
                "3 keep-warm turns failed or did not answer [quiet]; "
                "it restarts after the child's next real turn."
            )
            prefix = "keep-warm paused"
        elif kind == "pol":
            body = (
                "a policy did not allow the unattended keep-warm turn; "
                "it restarts after the child's next real turn."
            )
            prefix = "keep-warm paused"
        else:
            read = detail.get("read", 0)
            creation = detail.get("creation", 0)
            body = (
                "two keep-warm turns did not read the prompt cache "
                f"(last: read {read}, written {creation} tokens) — its harness "
                "likely keeps a shorter cache. Warming stays off for this child "
                "until it is moved to past and back."
            )
            prefix = "keep-warm paused"
        return f'[System: {prefix} for child "{title}" ({where}): {body}]'

    @staticmethod
    def _synthetic_request(session_id: str, app: Any) -> Request:
        """Build the one synthetic ``Request`` the events path needs."""
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/v1/sessions/{session_id}/events",
                "headers": [],
                "query_string": b"",
                "app": app,
                "scheme": "http",
                "root_path": "",
                "client": None,
                "server": None,
            }
        )


def _parse_last_cache(raw: str | None) -> tuple[int, int, int] | None:
    """
    Parse ``omnigent.last_cache`` — ``"<read>,<creation>,<observed_at>"``.

    :param raw: Raw label value, or ``None``.
    :returns: The three ints, or ``None`` when malformed.
    """
    if not isinstance(raw, str):
        return None
    parts = raw.split(",")
    if len(parts) != 3:
        return None
    try:
        read, creation, observed_at = (int(part) for part in parts)
    except ValueError:
        return None
    if read < 0 or creation < 0 or observed_at < 0:
        return None
    return read, creation, observed_at


def _turn_is_ours(state: _WarmState | None, running_since: int | None) -> bool:
    """
    Whether *running_since* belongs to the ping attempt in *state*.

    A turn is the ping's own when it started within a small lead of the
    attempt and no later than the turn budget.
    """
    if state is None or state.p is None or running_since is None:
        return False
    return state.p - _RUNNING_SINCE_LEAD_S <= running_since <= state.p + _PING_TURN_MAX_S


def _running_since(conv: Conversation) -> int | None:
    """Return the persisted ``omnigent.running_since`` stamp, if any."""
    raw = conv.labels.get(RUNNING_SINCE_LABEL_KEY)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _last_assistant_text(items: list[ConversationItem]) -> str | None:
    """
    Return the newest assistant message's text, or ``None``.

    Newest-first items; hidden meta messages and non-assistant items are
    skipped, and an assistant item with no text blocks does not mask an
    earlier text reply.
    """
    for item in items:
        data = item.data
        if not isinstance(data, MessageData) or data.is_meta or data.role != "assistant":
            continue
        parts: list[str] = []
        for block in data.content:
            if block.get("type") in ("output_text", "text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
        joined = "\n".join(parts).strip()
        if joined:
            return joined
    return None


__all__ = [
    "KEEP_WARM_LABEL",
    "LAST_CACHE_LABEL",
    "PING",
    "ChildKeepWarmSweeper",
    "warm_state_from_label",
]
