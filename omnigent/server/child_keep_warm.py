"""Keep-warm sweeper — one control ping per provider cache window.

Idle sessions in the active zone lose their provider prompt cache when the
cache window (Claude 1 h, Codex ~25 min) elapses, so the next real turn pays
a full prompt rewrite. This background loop keeps an eligible session warm by
forwarding one ``keep_warm_ping`` control to its runner at last-cache-touch +
interval, for at most the configured cap after the session's last real turn.
It covers main sessions and sub-agent children on harnesses with a keep-warm
channel (``_SUPPORTED_HARNESSES``), runs server-side (independent of any
browser connection), and gates every ping on the owner's per-agent keep-warm
settings, the runner's liveness, the host's ``keep_warm_v1`` capability, and
a server pre-filter that skips sessions with a pending synchronous card.

Pings never travel as conversation messages: the control is forwarded to the
runner and the runner reports the outcome back as an
``external_keep_warm_receipt`` event, settled by :meth:`settle_receipt`. State
lives in two compact-JSON conversation labels — ``omnigent.keep_warm``
(episode state, bounded) and ``omnigent.keep_warm_stats`` (pings, cost, last
return) — so it survives restarts and rides the existing label machinery.
Three consecutive failures pause warming until the next real turn; two
consecutive measured cache misses pause it stickily (a harness whose cache is
shorter than the assumed window), lifting only after the session leaves and
re-enters the active zone.

Every tenth tick the sweeper also archives live sub-agent children whose
effective host has been offline past the owner's ``host_offline_archive_s``
setting (``0`` disables), stamping ``omnigent.archive_reason`` /
``omnigent.archived_by`` provenance. A user unarchive of such a row pins
``omnigent.archive_exempt_since`` to the host's last-seen stamp, so the same
offline spell never re-archives it; a reconnect bumps the stamp and re-arms
the pass.

The loop is shaped like :class:`~omnigent.server.peer_sweeper.PeerSweeper`:
``start``/``shutdown`` own one task, ``_run`` loops ``_tick`` plus
``asyncio.sleep`` swallowing errors, and tests drive ``_tick`` directly with
an injected clock. Notices to the mother reuse the peer sweeper's
``notify_line`` when wired, so they park, batch and drop for archived mothers
exactly like every other server notice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.native.native_coding_agents import public_agent_name
from omnigent.runtime.pending_elicitations import snapshot_for
from omnigent.server.routes._sessions.common import (
    _ARCHIVE_EXEMPT_SINCE_LABEL_KEY,
    _ARCHIVE_REASON_HOST_OFFLINE,
    _ARCHIVE_REASON_LABEL_KEY,
    _ARCHIVED_BY_KEEP_WARM,
    _ARCHIVED_BY_LABEL_KEY,
    _KEEP_WARM_LABEL_KEY,
    _KEEP_WARM_STATS_LABEL_KEY,
    _LAST_CACHE_LABEL_KEY,
)
from omnigent.server.routes._sessions.helpers import (
    Placement,
    SessionLiveness,
    _child_summary_identity,
    _effective_placement,
    _inherited_placement,
    _prune_session_read_state,
    _publish_child_status_to_parent,
    _resolve_harness,
    _session_status_from_cache,
    effective_host_id,
)
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.server.user_preferences_store import (
    KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_S,
    KEEP_WARM_CODEX_DEFAULT_INTERVAL_S,
    KEEP_WARM_DEFAULT_MAX_S,
    KEEP_WARM_MAX_BOUNDS_S,
    KeepWarmSettings,
    keep_warm_for_agent,
    migrate_legacy_keep_warm,
    read_keep_warm_settings,
)
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import host_is_live
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

_logger = logging.getLogger(__name__)

KEEP_WARM_LABEL = _KEEP_WARM_LABEL_KEY
KEEP_WARM_STATS_LABEL = _KEEP_WARM_STATS_LABEL_KEY
LAST_CACHE_LABEL = _LAST_CACHE_LABEL_KEY
ARCHIVE_REASON_LABEL = _ARCHIVE_REASON_LABEL_KEY
ARCHIVED_BY_LABEL = _ARCHIVED_BY_LABEL_KEY
ARCHIVE_EXEMPT_SINCE_LABEL = _ARCHIVE_EXEMPT_SINCE_LABEL_KEY

_SLACK_S = 300
_TICK_INTERVAL_S = 60.0
_ATTEMPT_TIMEOUT_S = 180
_FAILURE_PAUSE_THRESHOLD = 3
_MISS_PAUSE_THRESHOLD = 2
_PAGE_LIMIT = 200
_MAX_PAGES = 100
_MAX_PARENT_HOPS = 32
#: Claude's provider cache TTL: a session reads warm on the clock alone.
_CLAUDE_TTL_S = 3600
#: Codex reports no cache TTL; an observation older than this proves nothing.
_CODEX_DEFAULT_STALENESS_S = 1800
#: Only Claude and Codex sessions have a provider cache worth keeping. This
#: map is the single place a later keep-warm channel registers.
_SUPPORTED_HARNESSES: dict[str, Literal["claude", "codex"]] = {
    "claude-native": "claude",
    "claude-sdk": "claude",
    "codex-native": "codex",
    "codex": "codex",
}
#: The legacy settings predate keep-warm channels beyond the two native
#: CLIs, so the one-time migration only ever covers those agents — never an
#: SDK harness that joined ``_SUPPORTED_HARNESSES`` later.
_MIGRATION_HARNESSES: dict[str, Literal["claude", "codex"]] = {
    "claude-native": "claude",
    "codex-native": "codex",
}
#: Reasons a cold / paused label may carry. ``pol`` and ``mom`` are legacy
#: (SCC19) codes — never written any more, still parsed from old labels.
_WHY_CODES = frozenset({"cap", "exp", "fail", "pol", "miss", "rev", "mom"})
_RECEIPT_OUTCOMES = frozenset({"ok", "skipped", "failed"})
#: Mirrored native sub-agent rows describe another harness's internal
#: sub-agent, not an Omnigent session with its own cache.
_MIRRORED_WRAPPER_LABELS = frozenset(
    {"claude-code-native-ui-subagent", "codex-native-ui-subagent"}
)
_WRAPPER_LABEL_KEY = "omnigent.wrapper"
#: Placement stand-in for a child whose parent pointer is missing.
_EMPTY_PLACEMENT = Placement(None, None, None)
#: A turn whose usage reading never lands stops retrying this long after the
#: episode opened.
_LATE_USAGE_GRACE_S = 300
#: The host-offline archive pass runs on every Nth tick: it scans every live
#: child, so it does not need the ping pass's per-tick cadence.
_ARCHIVE_PASS_TICK_INTERVAL = 10
#: Bound on archives per pass: a dead host with a huge child fleet spreads
#: the writes across passes instead of one burst.
_ARCHIVE_PASS_MAX_ARCHIVES = 50
#: Receipt stop reasons store as one of these bounded ASCII codes (``other``
#: otherwise) so a long or non-ASCII reason cannot break the 256-char label.
_STOP_REASON_CODES = frozenset(
    {
        "card",
        "unknown",
        "busy",
        "preempted",
        "composer_draft",
        "composer_changed",
        "user_active",
        "no_live_client",
        "btw_unavailable",
        "dismiss_failed",
        "aborted",
        "tool_attempt",
        "timeout",
        "harness_error",
        "unsupported",
    }
)


def _as_int(value: object) -> int | None:
    """Return *value* when it is a genuine int (never a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_number(value: object) -> float | None:
    """Return *value* as a float when it is numeric (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class _WarmState:
    """
    One session's parsed ``omnigent.keep_warm`` label.

    Short keys keep the serialized label well under the 256-character
    label bound: ``s`` state (``w`` warm / ``c`` cold / ``p`` paused),
    ``why`` reason, ``y`` model family, ``t`` last-seen ``running_since``,
    ``c`` episode start, ``u`` last cache touch, ``p`` ping attempt time,
    ``a`` pending attempt id, ``f`` failures, ``m`` misses, ``b`` usage
    baseline, ``v`` ``archive_revision`` at a miss pause, ``w``
    warm-until, ``o`` latest cache observation ``[epoch, 1=hit|0=miss]``,
    ``r`` late-usage retry pending, ``e`` pending late reading may count
    as a miss, ``k`` stop reason.

    :param s: Episode state.
    :param why: Reason for a cold / paused state — one of ``cap``, ``exp``,
        ``fail``, ``miss``, ``rev`` (``pol`` / ``mom`` parse from SCC19
        labels but are never written).
    :param y: Model family (``claude`` / ``codex``), written on every label
        write so readers can derive the warm rule without a harness lookup.
    :param t: ``running_since`` of the last turn seen.
    :param c: Epoch seconds the last real turn was seen settled.
    :param u: Epoch seconds the last turn (real or ok ping) was seen settled.
    :param p: Epoch seconds of the current ping attempt.
    :param a: Pending attempt id the receipt must match, else ``None``.
    :param f: Consecutive failures.
    :param m: Consecutive observed cache misses.
    :param b: Usage baseline marker — the last consumed Claude reading's
        ``observed_at``, or Codex's cumulative ``[input, cached]``.
    :param v: ``archive_revision`` at a miss pause.
    :param w: Epoch seconds the current interval window ends.
    :param o: Latest cache observation ``[epoch, hit]`` — the Codex
        warm_state evidence.
    :param r: Whether the episode-opening turn still owes a cache reading
        (the harness reported no usage in time); later ticks retry it.
    :param e: Whether the pending late reading may count as a miss —
        the episode-opening eligibility, stored because it cannot be
        re-derived once the episode's own state replaces the previous one.
    :param k: Why warming currently does not ping — the first failing
        eligibility gate or a receipt's reason code, else ``None``.
    """

    s: str
    why: str | None = None
    y: Literal["claude", "codex"] | None = None
    t: int | None = None
    c: int | None = None
    u: int | None = None
    p: int | None = None
    a: str | None = None
    f: int = 0
    m: int = 0
    b: int | list[int] | None = None
    v: int | None = None
    w: int | None = None
    o: list[int] | None = None
    r: bool = False
    e: bool = False
    k: str | None = None

    def to_label(self) -> str:
        """Serialize to the compact JSON label value."""
        data: dict[str, object] = {"s": self.s}
        if self.why is not None:
            data["why"] = self.why
        if self.y is not None:
            data["y"] = self.y
        if self.t is not None:
            data["t"] = self.t
        if self.c is not None:
            data["c"] = self.c
        if self.u is not None:
            data["u"] = self.u
        if self.p is not None:
            data["p"] = self.p
        if self.a is not None:
            data["a"] = self.a
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
        if self.o is not None:
            data["o"] = self.o
        if self.r:
            data["r"] = 1
        if self.e:
            data["e"] = 1
        if self.k is not None:
            data["k"] = self.k
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
        family = data.get("y")
        parsed_family: Literal["claude", "codex"] | None = (
            family if family == "claude" or family == "codex" else None
        )
        baseline = data.get("b")
        if (
            not isinstance(baseline, (int, list))
            or isinstance(baseline, bool)
            or (isinstance(baseline, list) and not all(_as_int(v) is not None for v in baseline))
        ):
            baseline = None
        observation = data.get("o")
        if (
            not isinstance(observation, list)
            or len(observation) != 2
            or any(_as_int(v) is None for v in observation)
        ):
            observation = None
        attempt_id = data.get("a")
        stop_reason = data.get("k")
        return cls(
            s=state,
            why=why if isinstance(why, str) and why in _WHY_CODES else None,
            y=parsed_family,
            t=_as_int(data.get("t")),
            c=_as_int(data.get("c")),
            u=_as_int(data.get("u")),
            p=_as_int(data.get("p")),
            a=attempt_id if isinstance(attempt_id, str) else None,
            f=_as_int(data.get("f")) or 0,
            m=_as_int(data.get("m")) or 0,
            b=baseline,
            v=_as_int(data.get("v")),
            w=_as_int(data.get("w")),
            o=observation,
            r=bool(data.get("r")),
            e=bool(data.get("e")),
            k=stop_reason if isinstance(stop_reason, str) else None,
        )


@dataclass
class _WarmStats:
    """
    One session's parsed ``omnigent.keep_warm_stats`` label.

    Counters live apart from the episode label so the 256-character bound
    holds. Costs are integer micro-USD. ``e`` flags that some summed ping was
    an estimate (a channel that reports no usage).

    :param ep_p: Pings in the current episode (since the last real turn).
    :param ep_c: Episode cost in micro-USD.
    :param ep_e: Whether any episode ping was an estimate.
    :param ep_s: Episode start epoch seconds.
    :param tot_p: Lifetime pings.
    :param tot_c: Lifetime cost in micro-USD.
    :param tot_e: Whether any lifetime ping was an estimate.
    :param lr_at: Epoch seconds of the last return-after-absence real turn.
    :param lr_r: Its cache result — ``hit`` / ``miss`` / ``unknown``.
    """

    ep_p: int = 0
    ep_c: int = 0
    ep_e: bool = False
    ep_s: int | None = None
    tot_p: int = 0
    tot_c: int = 0
    tot_e: bool = False
    lr_at: int | None = None
    lr_r: str | None = None

    def to_label(self) -> str:
        """Serialize to the compact JSON label value."""
        data: dict[str, object] = {}
        episode: dict[str, object] = {}
        if self.ep_p:
            episode["p"] = self.ep_p
        if self.ep_c:
            episode["c"] = self.ep_c
        if self.ep_e:
            episode["e"] = 1
        if self.ep_s is not None:
            episode["s"] = self.ep_s
        if episode:
            data["ep"] = episode
        total: dict[str, object] = {}
        if self.tot_p:
            total["p"] = self.tot_p
        if self.tot_c:
            total["c"] = self.tot_c
        if self.tot_e:
            total["e"] = 1
        if total:
            data["tot"] = total
        if self.lr_at is not None:
            data["lr"] = [self.lr_at, self.lr_r or "unknown"]
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def parse(cls, raw: str | None) -> _WarmStats:
        """Parse a stored label, returning zeros for anything unusable."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        episode = data.get("ep")
        episode = episode if isinstance(episode, dict) else {}
        total = data.get("tot")
        total = total if isinstance(total, dict) else {}
        last_return = data.get("lr")
        lr_at: int | None = None
        lr_r: str | None = None
        if (
            isinstance(last_return, list)
            and len(last_return) == 2
            and _as_int(last_return[0]) is not None
        ):
            lr_at = _as_int(last_return[0])
            lr_r = last_return[1] if last_return[1] in ("hit", "miss", "unknown") else None
        return cls(
            ep_p=_as_int(episode.get("p")) or 0,
            ep_c=_as_int(episode.get("c")) or 0,
            ep_e=bool(episode.get("e")),
            ep_s=_as_int(episode.get("s")),
            tot_p=_as_int(total.get("p")) or 0,
            tot_c=_as_int(total.get("c")) or 0,
            tot_e=bool(total.get("e")),
            lr_at=lr_at,
            lr_r=lr_r,
        )


class _TickResult(NamedTuple):
    """One locked tick pass's deferred effects, applied after the lock.

    :param conv: The session row the pass read.
    :param notices: Stop notices to send the mother.
    :param touch: Whether to forward the episode-start touch.
    :param ping: ``(attempt_id, family, harness)`` when a ping goes out.
    """

    conv: Conversation
    notices: list[tuple[str, dict[str, int]]]
    touch: bool
    ping: tuple[str, str | None, str | None] | None


class _ReceiptResult(NamedTuple):
    """One locked receipt settle's outcome, applied after the lock.

    :param matched: Whether the receipt matched the pending attempt.
    :param conv: The session row, when the receipt matched.
    :param notices: Stop notices to send the mother.
    """

    matched: bool
    conv: Conversation | None
    notices: list[tuple[str, dict[str, int]]]


def warm_state_from_label(
    raw: str | None,
    *,
    archived: bool,
    harness: str | None,
    busy: bool,
    now: int,
    family: Literal["claude", "codex"] | None = None,
    codex_staleness_s: int = _CODEX_DEFAULT_STALENESS_S,
    cold_after_s: int | None = None,
) -> Literal["warm", "cold"] | None:
    """
    Derive the rail pill state from a session's keep-warm label.

    No settings read: an archived session or an unsupported harness reads
    ``None``. A busy session is ``warm`` whatever the label says (its running
    turn touches the provider cache). A positive ``cold_after_s`` replaces the
    per-family rule with that clock alone: ``warm`` while the last cache touch
    is inside it, ``cold`` for a missing / unparsable label or a stale touch;
    ``0`` reads ``warm`` unconditionally. Otherwise the rule is per family:
    Claude is a clock estimate (``warm`` while the episode is warm and the
    last touch is inside the 1 h cache window); Codex is observation-based
    (``warm`` while the latest usage observation — ping receipt or real turn —
    is a hit younger than the staleness bound, the agent's interval + 300 s;
    with no observation at all, a real turn settled inside that bound also
    reads warm). With ``cold_after_s`` ``None`` an idle session with no usable
    label reads ``None``.

    :param raw: The ``omnigent.keep_warm`` label value, or ``None``.
    :param archived: Whether the session row itself is archived.
    :param harness: The session's canonical harness, e.g. ``"claude-native"``.
    :param busy: Whether the session's status is ``running`` / ``waiting``.
    :param now: Current epoch seconds.
    :param family: Model family; derived from *harness* when omitted.
    :param codex_staleness_s: Codex observation staleness bound in seconds.
    :param cold_after_s: Agent's idle seconds to cold, ``0`` to never read
        cold, or ``None`` for the per-family rule.
    :returns: ``"warm"``, ``"cold"``, or ``None``.
    """
    if archived:
        return None
    if family is None:
        if harness not in _SUPPORTED_HARNESSES:
            return None
        family = _SUPPORTED_HARNESSES[harness]
    if cold_after_s == 0:
        return "warm"
    if busy:
        return "warm"
    state = _WarmState.parse(raw)
    if cold_after_s is not None and cold_after_s > 0:
        if state is not None and state.u is not None and now - state.u < cold_after_s:
            return "warm"
        return "cold"
    if state is None:
        return None
    if family == "codex":
        observation = state.o
        if observation is None:
            # No observation this episode: a real turn that just settled put
            # its prefix in the provider cache.
            if state.c is not None and now - state.c < codex_staleness_s:
                return "warm"
            return "cold"
        if observation[1] == 1 and now - observation[0] < codex_staleness_s:
            return "warm"
        return "cold"
    if state.s == "w" and state.u is not None and now - state.u < _CLAUDE_TTL_S:
        return "warm"
    return "cold"


#: Warm-episode gate codes → the state read while the sweeper keeps ``s == "w"``
#: (``stop_reason`` comes from :data:`_STOP_REASON_STATUS`).
_WARM_GATE_STATUS: dict[str, str] = {
    "switch": "off",
    "runner": "paused",
    "host": "paused",
    "card": "paused",
}

#: Label reason codes → status object ``stop_reason``. ``runner`` is the
#: runner-liveness gate; ``runner_version`` stays schema-only, never produced.
_STOP_REASON_STATUS: dict[str, str] = {
    "cap": "cap",
    "miss": "misses",
    "fail": "failures",
    "card": "card",
    "host": "host",
    "switch": "switch_off",
    "runner": "host",
}


def keep_warm_family_for_harness(harness: str | None) -> Literal["claude", "codex"] | None:
    """
    Map a canonical harness onto the keep-warm family it belongs to.

    :param harness: Canonical harness id, e.g. ``"claude-native"``; ``None``
        for an unresolved session.
    :returns: ``"claude"`` / ``"codex"`` for a harness with a keep-warm
        channel, else ``None``.
    """
    return _SUPPORTED_HARNESSES.get(harness or "")


def keep_warm_family_from_labels(
    labels: Mapping[str, str] | None,
) -> Literal["claude", "codex"] | None:
    """
    Return the family the sweeper stamped on a session's keep-warm label.

    :param labels: Conversation labels, or ``None``.
    :returns: The label's ``y`` family, or ``None`` when the label is
        missing, malformed, or predates the stamp.
    """
    if not labels:
        return None
    state = _WarmState.parse(labels.get(KEEP_WARM_LABEL))
    return state.y if state is not None else None


def warm_state_for_labels(
    labels: Mapping[str, str] | None,
    *,
    archived: bool,
    busy: bool,
    now: int,
    family: Literal["claude", "codex"] | None = None,
    cold_after_s: int | None = None,
) -> Literal["warm", "cold"] | None:
    """
    Derive the rail pill state from raw labels plus a resolved harness family.

    ``None`` only for an archived session, a mirrored native sub-agent row,
    or a session whose harness has no known keep-warm family. A known family
    with ``cold_after_s == 0`` reads ``warm`` even with no label. The label's
    ``y`` stamp wins when present; otherwise the caller's ``family`` —
    resolved from the session's harness — supplies the per-family rule. A
    session outside the sweeper's scan window carries no label at all and
    reads ``warm`` while busy, ``cold`` otherwise.

    :param labels: Conversation labels, or ``None``.
    :param archived: Whether the session row itself is archived.
    :param busy: Whether the session's status is ``running`` / ``waiting``.
    :param now: Current epoch seconds.
    :param family: Model family resolved from the session's harness, used
        when the label carries no ``y`` stamp.
    :param cold_after_s: Agent's idle seconds to cold, ``0`` to never read
        cold, or ``None`` for the per-family rule.
    :returns: ``"warm"``, ``"cold"``, or ``None``.
    """
    if archived:
        return None
    if labels and labels.get(_WRAPPER_LABEL_KEY) in _MIRRORED_WRAPPER_LABELS:
        return None
    stamped = keep_warm_family_from_labels(labels)
    if stamped is not None:
        family = stamped
    if family is not None and cold_after_s == 0:
        return "warm"
    raw = labels.get(KEEP_WARM_LABEL) if labels else None
    if raw is None:
        if family is None:
            return None
        return "warm" if busy else "cold"
    if family is None:
        return None
    return warm_state_from_label(
        raw,
        archived=False,
        harness=None,
        busy=busy,
        now=now,
        family=family,
        cold_after_s=cold_after_s,
    )


def cold_after_for_agent(settings: KeepWarmSettings, agent_id: str | None) -> int | None:
    """
    Return the agent row's cold-after seconds, ignoring the main/child switches.

    :param settings: The owner's resolved keep-warm settings.
    :param agent_id: Agent whose row applies, or ``None``.
    :returns: The stored ``cold_after_s`` (``0`` = never cold), or ``None``
        when the agent id or its row is absent.
    """
    if agent_id is None:
        return None
    row = settings.agents.get(agent_id)
    if row is None:
        return None
    return row.cold_after_s


def keep_warm_status_from_labels(
    labels: Mapping[str, str] | None,
    *,
    archived: bool,
    now: int,  # noqa: ARG001 — state comes from the label, not the clock
) -> dict[str, Any] | None:
    """
    Build the ``keep_warm`` status object from a session's labels.

    ``state`` mirrors the episode label: ``s == "w"`` reads ``on`` unless a
    gate blocks the ping (``switch`` → ``off``; ``runner`` / ``host`` /
    ``card`` → ``paused``), ``s == "p"`` reads ``paused``, and ``s == "c"``
    reads ``stopped`` when the label records a reason (``why`` / ``k``), else
    ``off``. ``stop_reason`` is the first recognized reason mapped onto the
    status vocabulary; an unrecognized code reads ``None``, never an error.
    ``last_reason`` always carries the label's raw ``k`` code (the last ping's
    skip / fail reason). Costs convert integer micro-USD to float USD.

    :param labels: Conversation labels, or ``None``.
    :param archived: Whether the session row itself is archived.
    :param now: Current epoch seconds.
    :returns: The status object, or ``None`` when the session is archived or
        carries no keep-warm label.
    """
    if archived or not labels:
        return None
    state = _WarmState.parse(labels.get(KEEP_WARM_LABEL))
    if state is None:
        return None
    if state.s == "w":
        # A blocked warm episode reports the gate, not ``on``; a receipt
        # code (``busy``, ``btw_unavailable``) keeps reading ``on``.
        status = _WARM_GATE_STATUS.get(state.k or "", "on")
    elif state.s == "p":
        status = "paused"
    elif state.why is not None or state.k is not None:
        status = "stopped"
    else:
        status = "off"
    stop_reason: str | None = None
    if status != "on":
        stop_reason = _STOP_REASON_STATUS.get(state.why or "") or _STOP_REASON_STATUS.get(
            state.k or ""
        )
    stats = _WarmStats.parse(labels.get(KEEP_WARM_STATS_LABEL))
    last_return = (
        {"at": stats.lr_at, "result": stats.lr_r or "unknown"} if stats.lr_at is not None else None
    )
    return {
        "state": status,
        "stop_reason": stop_reason,
        "episode": {
            "pings": stats.ep_p,
            "cost_usd": stats.ep_c / 1_000_000,
            "estimated": stats.ep_e,
            "started_at": stats.ep_s,
        },
        "total": {
            "pings": stats.tot_p,
            "cost_usd": stats.tot_c / 1_000_000,
            "estimated": stats.tot_e,
        },
        "last_return": last_return,
        "last_reason": state.k,
    }


def keep_warm_episode_active(labels: Mapping[str, str] | None) -> bool:
    """Return whether the labels carry an active (``s == "w"``) keep-warm episode.

    Settings-free: this reads only the episode label, so callers protecting a
    warm session (e.g. the idle CLI pool) never need the owner's settings.

    :param labels: Conversation labels, or ``None``.
    :returns: ``True`` while the episode state is warm.
    """
    if not labels:
        return False
    state = _WarmState.parse(labels.get(KEEP_WARM_LABEL))
    return state is not None and state.s == "w"


def _receipt_measurement(
    family: str | None, data: dict[str, Any]
) -> Literal["hit", "miss"] | None:
    """
    Classify one ok receipt's normalized cache fields; ``None`` is unknown.

    Claude miss = ``cache_write > cache_read``; Codex miss =
    ``cache_read / input_total < 0.5``. A missing field never reads as a
    miss — the measurement is simply unknown. For the Claude family a
    measured ``cache_result`` (the statusLine cost delta) wins over the
    token fields.
    """
    read = _as_int(data.get("cache_read"))
    if family == "claude":
        cache_result = data.get("cache_result")
        if cache_result in ("hit", "miss"):
            return cache_result
        write = _as_int(data.get("cache_write"))
        if read is None or write is None:
            return None
        return "miss" if write > read else "hit"
    if family == "codex":
        total = _as_int(data.get("input_total"))
        if read is None or total is None or total <= 0:
            return None
        return "miss" if read / total < 0.5 else "hit"
    return None


def _micro_usd(value: object) -> int:
    """Convert a receipt's ``cost_usd`` to integer micro-USD (0 when absent)."""
    number = _as_number(value)
    if number is None or number <= 0:
        return 0
    return round(number * 1_000_000)


def _has_sync_pending(session_id: str) -> bool:
    """
    Server pre-filter: whether the session has a pending synchronous card.

    Any own pending elicitation whose ``params`` carry no ``async_kind``
    blocks a ping; mirrored items (``target_session_id``) are not the
    session's own card. The authoritative check is the runner's gate at ping
    time — an unreadable index here fails closed, never toward pinging.
    """
    try:
        events = snapshot_for(session_id)
    except Exception:
        _logger.exception("Keep-warm pending-elicitation pre-filter failed")
        return True
    for event in events:
        params = event.get("params")
        if not isinstance(params, dict):
            return True
        if params.get("target_session_id"):
            continue
        if params.get("async_kind"):
            continue
        return True
    return False


#: The started sweeper, for off-request publishers that carry no app handle
#: (the live-state worker building a child summary). Set by
#: :meth:`ChildKeepWarmSweeper.start`, cleared by :meth:`shutdown`.
_sweeper: ChildKeepWarmSweeper | None = None


def cold_after_for_session(conv: Conversation) -> int | None:
    """
    Read the session agent's cold-after seconds for an off-request publisher.

    Blocking: the caller is the live-state worker thread, which already does
    the store reads for its publish. Reads as the per-family rule (``None``)
    when no sweeper is started, it has no app, or the lookup fails.

    :param conv: The session row whose agent setting applies.
    :returns: The agent row's ``cold_after_s`` (``0`` = never cold), or
        ``None``.
    """
    sweeper = _sweeper
    if sweeper is None or sweeper._app is None:
        return None
    preferences_store = getattr(
        getattr(sweeper._app, "state", None), "user_preferences_store", None
    )
    try:
        return sweeper.cold_after_for(conv, preferences_store)
    except Exception:  # noqa: BLE001 — a summary fan-out must never fail here
        _logger.warning("Keep-warm cold-after read failed for %s", conv.id, exc_info=True)
        return None


class ChildKeepWarmSweeper:
    """Forward one keep-warm control per cache window for eligible sessions."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
        forward_control: Callable[[str, dict[str, Any]], Awaitable[bool]],
        host_ok: Callable[[Conversation], bool],
        notify_line: Callable[[str, str], Awaitable[None]] | None = None,
        interval: float = _TICK_INTERVAL_S,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        """
        :param conversation_store: Store for session rows and label writes.
        :param permission_store: Permission store for owner resolution
            (``None`` in single-user local mode).
        :param liveness_lookup: Bulk session-liveness lookup — the ping idle
            gate reads ``runner_online`` from it, never ``_true_state``.
        :param forward_control: Forward one control body to the session's
            runner; ``True`` when the runner accepted it (2xx).
        :param host_ok: Whether the session's host is live and advertises the
            ``keep_warm_v1`` capability (hostless local sessions count as
            supported).
        :param notify_line: ``PeerSweeper.notify_line`` — the mother's parked
            notice queue, or ``None`` when peer messaging is off (notices are
            then dropped; the label state still settles).
        :param interval: Seconds between ticks.
        :param clock: Epoch-seconds clock, overridable for tests.
        """
        self._conversation_store = conversation_store
        self._permission_store = permission_store
        self._liveness_lookup = liveness_lookup
        self._forward_control = forward_control
        self._host_ok = host_ok
        self._notify_line = notify_line
        self._interval = interval
        self._clock = clock
        # session_id -> warm_until for every label that says state = warm.
        self._tracked: dict[str, int] = {}
        # session_id -> forward failure recorded by the ping task, applied on
        # the next tick so a slow forward never stalls the loop.
        self._outcomes: dict[str, str] = {}
        self._ping_tasks: set[asyncio.Task[None]] = set()
        # One lock serializes each session's label read-modify-write between
        # a tick and a receipt. The server runs one sweeper replica, so an
        # in-process lock is the whole exclusion story.
        self._session_lock = asyncio.Lock()
        # Owners whose legacy migration already ran in this process.
        self._migrated_owners: set[str] = set()
        # Ticks since the last host-offline archive pass.
        self._ticks_since_archive_pass = 0
        # Candidate scan window: the largest configured cap seen (at least
        # the default) plus slack; grows as settings are read.
        self._scan_window_s = KEEP_WARM_DEFAULT_MAX_S + _SLACK_S
        self._app: Any | None = None
        self._task: asyncio.Task[None] | None = None
        # The clock at the previous tick's start: the run loop sleeps after a
        # sweep, so real spacing exceeds ``_interval`` and an edge between ticks
        # needs this comparison. ``None`` until the first tick lands.
        self._previous_tick_now: int | None = None

    async def start(self, app: Any) -> None:
        """Seed the tracked warm set, then start the loop."""
        if self._task is not None and not self._task.done():
            return
        self._app = app
        global _sweeper
        _sweeper = self
        await self._seed_tracked()
        self._task = asyncio.create_task(self._run(), name="child-keep-warm")

    async def shutdown(self) -> None:
        """Stop the loop and any in-flight ping forwards."""
        global _sweeper
        if _sweeper is self:
            _sweeper = None
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
            sessions = await asyncio.to_thread(self._scan_labeled_sessions)
        except Exception:
            _logger.exception("Child keep-warm failed to seed its tracked set")
            return
        for conv in sessions:
            state = _WarmState.parse(conv.labels.get(KEEP_WARM_LABEL))
            if state is not None and state.s == "w":
                self._tracked[conv.id] = state.w or 0

    def _scan_labeled_sessions(self) -> list[Conversation]:
        """Page every non-archived session row for the startup seed."""
        sessions: list[Conversation] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._conversation_store.list_conversations(
                limit=_PAGE_LIMIT,
                after=after,
                kind=None,
                include_archived=False,
            )
            sessions.extend(page.data)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return sessions

    async def _tick(self) -> None:
        """One sweep over ping candidates plus tracked episodes; never raises."""
        assert self._app is not None, "ChildKeepWarmSweeper.start() must run before _tick()"
        now = self._clock()
        candidate_ids: set[str] = set()
        try:
            candidate_ids = await asyncio.to_thread(self._list_ping_candidates, now)
        except Exception:
            _logger.exception("Child keep-warm failed to list ping candidates")
        settings_cache: dict[str, KeepWarmSettings] = {}
        harness_cache: dict[str, str | None] = {}
        for session_id in sorted(candidate_ids | set(self._tracked)):
            try:
                await self._process_session(session_id, now, settings_cache, harness_cache)
            except Exception:
                _logger.exception("Child keep-warm failed to process session %s", session_id)
        self._ticks_since_archive_pass += 1
        if self._ticks_since_archive_pass >= _ARCHIVE_PASS_TICK_INTERVAL:
            self._ticks_since_archive_pass = 0
            try:
                await self._archive_offline_host_children(now)
            except Exception:
                _logger.exception("Child keep-warm host-offline archive pass failed")
        self._previous_tick_now = now

    def _list_ping_candidates(self, now: int) -> set[str]:
        """Return sessions updated inside the candidate window (paged)."""
        ids: set[str] = set()
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._conversation_store.list_conversations(
                limit=_PAGE_LIMIT,
                after=after,
                kind=None,
                include_archived=False,
                updated_after=now - self._scan_window_s,
                sort_by="updated_at",
            )
            ids.update(conv.id for conv in page.data)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return ids

    async def _archive_offline_host_children(self, now: int) -> None:
        """Archive live children whose host stayed offline past the owner's setting.

        Runs every :data:`_ARCHIVE_PASS_TICK_INTERVAL` ticks over ALL
        non-archived children — no recency window, since a child untouched for
        days under a dead host is exactly the row worth retiring. Bounded to
        :data:`_ARCHIVE_PASS_MAX_ARCHIVES` archives per pass; never raises.
        """
        host_store = getattr(getattr(self._app, "state", None), "host_store", None)
        if host_store is None:
            return
        try:
            children = await asyncio.to_thread(self._list_live_children)
        except Exception:
            _logger.exception("Child keep-warm host-offline archive scan failed")
            return
        settings_cache: dict[str, KeepWarmSettings] = {}
        host_cache: dict[str, Any] = {}
        archived = 0
        for conv in children:
            if archived >= _ARCHIVE_PASS_MAX_ARCHIVES:
                break
            try:
                if await self._maybe_archive_offline_child(
                    conv, now, host_store, settings_cache, host_cache
                ):
                    archived += 1
            except Exception:
                _logger.exception("Child keep-warm host-offline archive failed for %s", conv.id)

    def _list_live_children(self) -> list[Conversation]:
        """Page every non-archived sub-agent row (blocking; run in a thread)."""
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

    async def _maybe_archive_offline_child(
        self,
        conv: Conversation,
        now: int,
        host_store: Any,
        settings_cache: dict[str, KeepWarmSettings],
        host_cache: dict[str, Any],
    ) -> bool:
        """Archive one child when its effective host is offline past the setting."""
        store = self._conversation_store
        host_id = await asyncio.to_thread(effective_host_id, store, conv)
        if host_id is None:
            return False
        if host_id not in host_cache:
            host_cache[host_id] = await asyncio.to_thread(host_store.get_host, host_id)
        host = host_cache[host_id]
        # A missing row proves nothing; a live host needs no cleanup. The age
        # check below catches a hard-crashed host whose status stayed online.
        if host is None or host_is_live(host, now):
            return False
        owner = await asyncio.to_thread(collab_owner_for, conv, store, self._permission_store)
        settings = await self._settings_for(owner, now, settings_cache)
        threshold_s = settings.host_offline_archive_s
        if threshold_s <= 0 or now - host.updated_at < threshold_s:
            return False
        # A user unarchive pins the host's last-seen stamp; the same offline
        # spell must not re-archive the row.
        if conv.labels.get(ARCHIVE_EXEMPT_SINCE_LABEL) == str(host.updated_at):
            return False

        async def _clear_provenance() -> None:
            """Best-effort removal so a live row never keeps stale provenance."""
            for key in (ARCHIVE_REASON_LABEL, ARCHIVED_BY_LABEL):
                try:
                    await asyncio.to_thread(store.delete_label, conv.id, key)
                except Exception:  # noqa: BLE001 — must not mask the archive failure
                    _logger.warning(
                        "Child keep-warm could not clear label %s on session %s",
                        key,
                        conv.id,
                        exc_info=True,
                    )

        # Provenance lands before the archive flag: an unarchive arriving in
        # between must see it to install the current outage's exemption.
        await asyncio.to_thread(
            store.set_labels,
            conv.id,
            {
                ARCHIVE_REASON_LABEL: _ARCHIVE_REASON_HOST_OFFLINE,
                ARCHIVED_BY_LABEL: _ARCHIVED_BY_KEEP_WARM,
            },
        )
        try:
            updated = await asyncio.to_thread(
                store.update_conversation,
                conv.id,
                archived=True,
                close_cli_on_archive=False,
            )
        except Exception:
            await _clear_provenance()
            raise
        if updated is None:
            await _clear_provenance()
            return False
        if conv.parent_conversation_id is not None:
            _publish_child_status_to_parent(conv.id, None)
        _prune_session_read_state(conv.id)
        return True

    async def _settings_for(
        self, owner: str, now: int, cache: dict[str, KeepWarmSettings]
    ) -> KeepWarmSettings:
        """
        Read one owner's keep-warm settings, cached per tick.

        The one-time legacy migration runs before the first read of an owner
        in this process, with the claude-native / codex-native agents that
        exist now; afterwards only the ``keep_warm`` namespace is read.
        """
        cached = cache.get(owner)
        if cached is not None:
            return cached
        preferences_store = getattr(
            getattr(self._app, "state", None), "user_preferences_store", None
        )
        if preferences_store is not None and owner not in self._migrated_owners:
            try:
                agents = await asyncio.to_thread(self._native_agents_for_migration)
            except Exception:
                # Enumeration failed: skip the migration (and leave the owner
                # unmarked) so a later tick retries with a real agent list —
                # migrating against an empty snapshot would write an empty
                # keep_warm namespace and lose legacy warming for good.
                _logger.exception("Child keep-warm agent enumeration failed; migration deferred")
            else:
                await asyncio.to_thread(
                    migrate_legacy_keep_warm, preferences_store, owner, agents, now
                )
                self._migrated_owners.add(owner)
        settings = await asyncio.to_thread(read_keep_warm_settings, preferences_store, owner)
        max_low, max_high = KEEP_WARM_MAX_BOUNDS_S
        for row in settings.agents.values():
            clamped = min(max(row.max_s, max_low), max_high)
            self._scan_window_s = max(self._scan_window_s, clamped + _SLACK_S)
            # A session untouched for N can still need a tick when its N edge
            # passes, so the candidate window must cover the longest N too.
            if row.cold_after_s is not None and row.cold_after_s > 0:
                self._scan_window_s = max(self._scan_window_s, row.cold_after_s + _SLACK_S)
        cache[owner] = settings
        return settings

    def cold_after_for(self, conv: Conversation, preferences_store: Any) -> int | None:
        """
        Read the session agent's cold-after seconds; ``None`` on any failure.

        Blocking: callers run it in a thread. The owner's main/child switches
        never apply here. Any read failure logs and reads as the platform
        rule, so a response is never lost to this lookup.

        :param conv: The session row whose agent setting applies.
        :param preferences_store: Preferences store, or ``None``.
        :returns: The agent row's ``cold_after_s`` (``0`` = never cold), or
            ``None``.
        """
        return self.cold_after_for_many([conv], preferences_store).get(conv.id)

    def cold_after_for_many(
        self, convs: list[Conversation], preferences_store: Any
    ) -> dict[str, int | None]:
        """
        Batch :meth:`cold_after_for`: one settings read per distinct owner.

        Blocking: callers run it in a thread. Never raises: one owner's read
        failure logs and leaves that owner's rows at ``None`` (the per-family
        rule), and an owner-resolution failure leaves only that conversation
        at ``None``.

        :param convs: Session rows whose agent settings apply.
        :param preferences_store: Preferences store, or ``None``.
        :returns: Map from conversation id to the agent row's ``cold_after_s``
            (``0`` = never cold), or ``None``.
        """
        by_session: dict[str, int | None] = {}
        owner_by_session: dict[str, str] = {}
        settings_by_owner: dict[str, KeepWarmSettings | None] = {}
        for conv in convs:
            try:
                owner = owner_by_session.get(conv.id)
                if owner is None:
                    owner = collab_owner_for(
                        conv, self._conversation_store, self._permission_store
                    )
                    owner_by_session[conv.id] = owner
            except Exception:  # noqa: BLE001 — this lookup must never fail a snapshot
                _logger.warning("Keep-warm cold-after read failed for %s", conv.id, exc_info=True)
                by_session[conv.id] = None
                continue
            if owner not in settings_by_owner:
                try:
                    settings_by_owner[owner] = read_keep_warm_settings(preferences_store, owner)
                except Exception:  # noqa: BLE001 — one owner must not sink the batch
                    _logger.warning(
                        "Keep-warm cold-after settings read failed for %s", owner, exc_info=True
                    )
                    settings_by_owner[owner] = None
            settings = settings_by_owner[owner]
            by_session[conv.id] = (
                None if settings is None else cold_after_for_agent(settings, conv.agent_id)
            )
        return by_session

    def _native_agents_for_migration(self) -> list[tuple[str, str]]:
        """
        List the existing claude-native / codex-native agents as
        ``(agent_id, family)`` pairs for the legacy migration.

        Raises on enumeration failure: the caller defers the migration so an
        empty list is never mistaken for "no native agents exist". Blocking:
        callers run it in a thread.
        """
        agents: list[tuple[str, str]] = []
        app_state = getattr(self._app, "state", None)
        agent_store = getattr(app_state, "agent_store", None)
        if agent_store is None:
            return agents
        from omnigent.harness_aliases import canonicalize_harness
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = agent_store.list(limit=_PAGE_LIMIT, after=after)
            for agent in page.data:
                harness = self._agent_harness(agent, agent_cache, canonicalize_harness)
                family = _MIGRATION_HARNESSES.get(harness or "")
                if family is not None:
                    agents.append((agent.id, family))
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return agents

    @staticmethod
    def _agent_harness(agent: Any, agent_cache: Any, canonicalize: Any) -> str | None:
        """Resolve one agent's canonical harness from its bundle spec."""
        if agent.bundle_location is None:
            return None
        try:
            loaded = agent_cache.load(
                agent.id, agent.bundle_location, expand_env=agent.session_id is None
            )
            harness = loaded.spec.executor.config.get("harness") or loaded.spec.executor.type
        except Exception:  # noqa: BLE001 — an unloadable bundle migrates nothing
            return None
        return canonicalize(harness) or harness

    async def _harness_for(self, conv: Conversation, cache: dict[str, str | None]) -> str | None:
        """Resolve the session's harness (honours ``harness_override``)."""
        if conv.id not in cache:
            cache[conv.id] = await asyncio.to_thread(_resolve_harness, conv)
        return cache[conv.id]

    async def _process_session(
        self,
        session_id: str,
        now: int,
        settings_cache: dict[str, KeepWarmSettings],
        harness_cache: dict[str, str | None],
    ) -> None:
        """Run one session's pass under its lock, then apply deferred effects."""
        async with self._session_lock:
            result = await self._process_session_locked(
                session_id, now, settings_cache, harness_cache
            )
        if result is None:
            return
        if result.notices:
            await self._send_notices(result.conv, result.notices)
        if result.touch:
            await self._forward_touch(session_id)
        if result.ping is not None:
            attempt_id, family, harness = result.ping
            self._spawn_ping(session_id, attempt_id, family, harness)

    async def _process_session_locked(
        self,
        session_id: str,
        now: int,
        settings_cache: dict[str, KeepWarmSettings],
        harness_cache: dict[str, str | None],
    ) -> _TickResult | None:
        """Read one session fresh and apply the state-machine rules in order."""
        store = self._conversation_store
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        if conv is None:
            self._drop_tracking(session_id)
            return None

        raw_label = conv.labels.get(KEEP_WARM_LABEL)
        state = _WarmState.parse(raw_label)
        running_since = _running_since(conv)
        status = _session_status_from_cache(conv.id, conv.live_status)
        busy = status == "running"

        # Rule 1: the session itself archived — drop it; the pill reads None
        # and the label goes inert. An archived mother keeps the child
        # tracked so rule 7 can settle it cold silently.
        if conv.archived:
            self._drop_tracking(session_id)
            return None

        mirrored = conv.labels.get(_WRAPPER_LABEL_KEY) in _MIRRORED_WRAPPER_LABELS
        harness = await self._harness_for(conv, harness_cache)
        family = _SUPPORTED_HARNESSES.get(harness or "")
        # Stamp the family on every write so later readers derive the warm
        # rule from the label alone.
        if state is not None and family is not None:
            state.y = family
        inspectable = family is not None and not mirrored
        if not inspectable and state is None:
            self._drop_tracking(session_id)
            return None

        is_child = conv.parent_conversation_id is not None
        owner = await asyncio.to_thread(collab_owner_for, conv, store, self._permission_store)
        settings = await self._settings_for(owner, now, settings_cache)
        cold_after = cold_after_for_agent(settings, conv.agent_id)
        resolved = keep_warm_for_agent(settings, conv.agent_id, family) if family else None
        # Absent agent = off; the session's class picks the row's switch.
        switch_on = resolved is not None and (resolved.child if is_child else resolved.main)
        if resolved is not None:
            interval = resolved.interval_s
            max_s = resolved.max_s
        else:
            interval = (
                KEEP_WARM_CODEX_DEFAULT_INTERVAL_S
                if family == "codex"
                else KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_S
            )
            max_s = KEEP_WARM_DEFAULT_MAX_S
        staleness_s = interval + _SLACK_S if resolved is not None else _CODEX_DEFAULT_STALENESS_S

        # The pill the previous tick left behind; the first tick after a start
        # falls back to one sweep interval. Real spacing exceeds ``_interval``,
        # so comparing at ``now - interval`` would miss an edge in the gap.
        previous_tick_now = (
            self._previous_tick_now
            if self._previous_tick_now is not None
            else now - int(self._interval)
        )
        warm_at_previous_tick = warm_state_from_label(
            raw_label,
            archived=False,
            harness=harness,
            busy=busy,
            now=previous_tick_now,
            family=family,
            codex_staleness_s=staleness_s,
            cold_after_s=cold_after,
        )
        # A label that said warm may already DERIVE cold once its window
        # passed (the rail still shows the last publish), so leaving a warm
        # label is itself a pill transition worth publishing.
        state_warm_before = state is not None and state.s == "w"

        ancestor_archived = is_child and await asyncio.to_thread(self._ancestor_archived, conv)
        runner_online = self._runner_online(session_id)
        host_ok = self._host_okay(conv)
        sync_pending = _has_sync_pending(session_id)
        base_eligible = (
            inspectable
            and switch_on
            and running_since is not None
            and not is_session_closed(conv.labels, conv.title)
            and not ancestor_archived
            and runner_online is True
            and host_ok
            and status == "idle"
            and not sync_pending
        )
        # The stop reason names the first gate, in the order the checks run,
        # that blocks a ping right now; ``None`` means none blocks.
        gate: str | None = None
        if not switch_on:
            gate = "switch"
        elif runner_online is not True:
            gate = "runner"
        elif not host_ok:
            gate = "host"
        elif sync_pending:
            gate = "card"

        notices: list[tuple[str, dict[str, int]]] = []
        stats = _WarmStats.parse(conv.labels.get(KEEP_WARM_STATS_LABEL))
        stats_dirty = False
        touch = False
        # A failed attempt is retried on a LATER tick, never in the same pass
        # that recorded its failure.
        retry_deferred = False

        # A ping task's forward failure applies on the tick after it was
        # recorded: one failure, attempt cleared.
        outcome = self._outcomes.pop(session_id, None)
        if state is not None and state.a is not None and outcome is not None:
            state.f += 1
            state.a = None
            state.p = None
            retry_deferred = True

        # A pending attempt the runner never receipts times out as a failure.
        if (
            state is not None
            and state.a is not None
            and state.p is not None
            and now - state.p > _ATTEMPT_TIMEOUT_S
        ):
            state.f += 1
            state.a = None
            state.p = None
            retry_deferred = True

        # Rule 2: a sticky miss pause lifts once the session left and
        # re-entered the active zone (its archive revision moved). The
        # watermark moves to the current turn so rule 3 only re-arms on a
        # later real turn, never on the cold cache the pause was about, and
        # the miss count starts fresh — the pause was about the old zone.
        if state is not None and state.s == "p" and state.why == "miss":
            if state.v != conv.archive_revision:
                state.s, state.why = "c", "rev"
                state.m = 0
                if running_since is not None:
                    state.t = running_since

        # Rule 3: a new real turn settled starts a fresh episode. Only the
        # not-yet-revised miss pause is sticky — a cold ``cap`` label re-arms
        # here. The turn's cache reading (when measurable) classifies the
        # return after an absence and feeds the Codex warm_state observation.
        if inspectable and running_since is not None and status == "idle":
            new_episode = state is None or running_since != state.t
            if new_episode:
                sticky_miss = (
                    state is not None
                    and state.s == "p"
                    and state.why == "miss"
                    and state.v == conv.archive_revision
                )
                settle = conv.updated_at if conv.updated_at >= running_since else now
                if not sticky_miss:
                    prev_u = state.u if state is not None else None
                    prev_b = state.b if state is not None else None
                    prev_m = state.m if state is not None else 0
                    # A real-turn miss is evidence against keep-warm only when
                    # warming held the cache for this turn: a warm prior episode
                    # whose window covers the turn's start, with an ok ping in it.
                    miss_counts = (
                        state is not None
                        and state.s == "w"
                        and state.w is not None
                        and running_since <= state.w
                        and stats.ep_p >= 1
                    )
                    reading = self._turn_cache_reading(conv, prev_b, running_since, family)
                    misses = prev_m
                    if reading is not None:
                        if reading[0] == "miss":
                            misses = prev_m + 1 if miss_counts else prev_m
                        else:
                            misses = 0
                    state = _WarmState(
                        s="w",
                        y=family,
                        t=running_since,
                        c=settle,
                        u=settle,
                        m=misses,
                        b=self._usage_baseline(conv, family),
                        w=settle + interval + _SLACK_S,
                        r=reading is None,
                        e=reading is None and miss_counts,
                    )
                    if family == "codex" and reading is not None:
                        state.o = [settle, 0 if reading[0] == "miss" else 1]
                    if (
                        reading is not None
                        and reading[0] == "miss"
                        and miss_counts
                        and misses >= _MISS_PAUSE_THRESHOLD
                    ):
                        state.s, state.why = "p", "miss"
                        state.v = conv.archive_revision
                        notices.append(("miss", {"read": reading[1], "creation": reading[2]}))
                    stats = _WarmStats(
                        ep_s=settle,
                        tot_p=stats.tot_p,
                        tot_c=stats.tot_c,
                        tot_e=stats.tot_e,
                        lr_at=stats.lr_at,
                        lr_r=stats.lr_r,
                    )
                    stats_dirty = True
                    ttl = _CLAUDE_TTL_S if family == "claude" else staleness_s
                    if prev_u is not None and running_since > prev_u + ttl:
                        stats.lr_at = settle
                        stats.lr_r = reading[0] if reading is not None else "unknown"
                    # The reaper-yield touch protects an episode that will be
                    # pinged; with the switch off there is nothing to protect.
                    touch = switch_on and runner_online is True and host_ok
                else:
                    # The pause stays sticky, but the settled turn is consumed
                    # and its cache touch advances the clock readers use.
                    assert state is not None
                    state.t = running_since
                    state.u = settle
            elif state is not None and state.r and state.s == "w":
                # The episode opened with an unmeasurable reading; retry the
                # same turn's classification against the stored baseline
                # (running_since still matches state.t here) until the grace
                # elapses.
                if state.c is not None and now - state.c > _LATE_USAGE_GRACE_S:
                    state.r = False
                    state.e = False
                else:
                    late = self._turn_cache_reading(conv, state.b, running_since, family)
                    if late is not None:
                        state.r = False
                        # The miss blame eligibility was fixed when the episode
                        # opened; a late hit resets the count either way.
                        late_counts = state.e
                        state.e = False
                        # The consumed reading is the new baseline: the next
                        # episode measures only its own turn.
                        state.b = self._usage_baseline(conv, family)
                        if late[0] == "miss":
                            if late_counts:
                                state.m += 1
                        else:
                            state.m = 0
                        if family == "codex":
                            state.o = [now, 0 if late[0] == "miss" else 1]
                        if stats.lr_at == state.c and stats.lr_r == "unknown":
                            stats.lr_r = late[0]
                            stats_dirty = True
                        if late[0] == "miss" and late_counts and state.m >= _MISS_PAUSE_THRESHOLD:
                            state.s, state.why = "p", "miss"
                            state.v = conv.archive_revision
                            notices.append(("miss", {"read": late[1], "creation": late[2]}))

        # Rule 6: repeated failures pause warming until the next real turn.
        if (
            state is not None
            and state.f >= _FAILURE_PAUSE_THRESHOLD
            and not (state.s == "p" and state.why == "fail")
        ):
            state.s, state.why = "p", "fail"
            notices.append(("fail", {}))

        # Rule 7: cap and expiry settle tracked sessions whether or not they
        # are eligible to be pinged. An expired window under an archived
        # mother or a switched-off agent goes cold silently.
        tracked = session_id in self._tracked or (state is not None and state.s == "w")
        if state is not None and state.s == "w" and tracked:
            if state.c is not None and now - state.c >= max_s:
                state.s = "c"
                state.why = "cap"
                notices.append(("cap", {"hours": max(1, round(max_s / 3600))}))
            elif state.w is not None and now > state.w and not busy:
                state.s = "c"
                state.why = "exp"

        # Stop-reason visibility: an idle inspectable episode records the
        # first gate blocking its ping, and clears it once the gates pass or
        # the episode leaves warm. Written only on change so a quiet session
        # churns no label.
        desired_k = gate if state is not None and state.s == "w" else None
        if inspectable and not busy and state is not None and state.k != desired_k:
            state.k = desired_k

        # Rule 8: due — write the attempt into the label first, then forward.
        ping_due = False
        if (
            base_eligible
            and state is not None
            and state.s == "w"
            and state.a is None
            and not retry_deferred
        ):
            if (
                state.u is not None
                and now >= state.u + interval
                and (state.c is None or now - state.c < max_s)
            ):
                state.p = now
                state.a = secrets.token_hex(4)
                ping_due = True

        updates: dict[str, str] = {}
        new_label = state.to_label() if state is not None else None
        if new_label is not None and new_label != raw_label:
            updates[KEEP_WARM_LABEL] = new_label
        if stats_dirty:
            new_stats = stats.to_label()
            if new_stats != conv.labels.get(KEEP_WARM_STATS_LABEL):
                updates[KEEP_WARM_STATS_LABEL] = new_stats
        if updates:
            await asyncio.to_thread(store.set_labels, session_id, updates)
        # One publish decision per tick: the pill at the previous tick's clock
        # under the old label against the pill now under the label as written (or
        # unchanged); comparing both at ``now`` would miss an edge in the gap.
        label_now = new_label if KEEP_WARM_LABEL in updates else raw_label
        warm_after = warm_state_from_label(
            label_now,
            archived=False,
            harness=harness,
            busy=busy,
            now=now,
            family=family,
            codex_staleness_s=staleness_s,
            cold_after_s=cold_after,
        )
        state_warm_after = state is not None and state.s == "w"
        if warm_after != warm_at_previous_tick or (state_warm_before and not state_warm_after):
            _publish_child_status_to_parent(session_id, None)
        if state is not None and state.s == "w":
            self._tracked[session_id] = state.w or 0
        else:
            self._tracked.pop(session_id, None)

        ping: tuple[str, str | None, str | None] | None = None
        if ping_due and state is not None and state.a is not None:
            ping = (state.a, family, harness)
        return _TickResult(conv=conv, notices=notices, touch=touch, ping=ping)

    async def settle_receipt(self, session_id: str, data: dict[str, Any]) -> bool:
        """
        Settle one pending ping attempt from the runner's receipt.

        Unknown or already-settled attempt ids are ignored (``False``).
        ``ok`` advances the last cache touch, counts the ping and its cost,
        clears any recorded stop reason, and applies the measured miss rule
        on the normalized usage fields; ``skipped`` clears the attempt with
        no other change (the cache may expire); ``failed`` counts a failure
        (three pause warming). A ``skipped`` / ``failed`` receipt's
        ``reason`` is recorded as the stop reason.

        :param session_id: Session the receipt belongs to.
        :param data: The ``external_keep_warm_receipt`` payload.
        :returns: ``True`` when the receipt matched the pending attempt.
        """
        async with self._session_lock:
            result = await self._settle_receipt_locked(session_id, data)
        if result.conv is not None and result.notices:
            await self._send_notices(result.conv, result.notices)
        return result.matched

    async def _settle_receipt_locked(
        self, session_id: str, data: dict[str, Any]
    ) -> _ReceiptResult:
        """Settle one pending attempt under the session lock."""
        attempt_id = data.get("attempt_id")
        outcome = data.get("outcome")
        store = self._conversation_store
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        if conv is None or conv.archived:
            return _ReceiptResult(False, None, [])
        raw_label = conv.labels.get(KEEP_WARM_LABEL)
        state = _WarmState.parse(raw_label)
        if (
            state is None
            or state.a is None
            or not isinstance(attempt_id, str)
            or attempt_id != state.a
            or outcome not in _RECEIPT_OUTCOMES
        ):
            return _ReceiptResult(False, None, [])

        now = self._clock()
        status = _session_status_from_cache(conv.id, conv.live_status)
        busy = status == "running"
        harness = await asyncio.to_thread(_resolve_harness, conv)
        family = _SUPPORTED_HARNESSES.get(harness or "")
        if family is not None:
            state.y = family
        is_child = conv.parent_conversation_id is not None
        owner = await asyncio.to_thread(collab_owner_for, conv, store, self._permission_store)
        settings = await self._settings_for(owner, now, {})
        cold_after = cold_after_for_agent(settings, conv.agent_id)
        resolved = keep_warm_for_agent(settings, conv.agent_id, family) if family else None
        if resolved is not None:
            interval = resolved.interval_s
            staleness_s = resolved.interval_s + _SLACK_S
        else:
            interval = (
                KEEP_WARM_CODEX_DEFAULT_INTERVAL_S
                if family == "codex"
                else KEEP_WARM_CLAUDE_DEFAULT_INTERVAL_S
            )
            staleness_s = _CODEX_DEFAULT_STALENESS_S

        warm_before = warm_state_from_label(
            raw_label,
            archived=False,
            harness=harness,
            busy=busy,
            now=now,
            family=family,
            codex_staleness_s=staleness_s,
            cold_after_s=cold_after,
        )
        state_warm_before = state.s == "w"

        notices: list[tuple[str, dict[str, int]]] = []
        stats = _WarmStats.parse(conv.labels.get(KEEP_WARM_STATS_LABEL))
        stats_dirty = False
        if outcome == "ok":
            state.u = now
            state.f = 0
            state.w = now + interval + _SLACK_S
            state.k = None
            if stats.ep_s is None:
                stats.ep_s = state.c
            stats.ep_p += 1
            stats.tot_p += 1
            cost = _micro_usd(data.get("cost_usd"))
            stats.ep_c += cost
            stats.tot_c += cost
            if data.get("estimated") is True:
                stats.ep_e = True
                stats.tot_e = True
            stats_dirty = True
            measurement = _receipt_measurement(family, data)
            if measurement == "miss":
                state.m += 1
                if family == "codex":
                    state.o = [now, 0]
                if state.m >= _MISS_PAUSE_THRESHOLD:
                    state.s, state.why = "p", "miss"
                    state.v = conv.archive_revision
                    notices.append(("miss", _receipt_miss_detail(family, data)))
            elif measurement == "hit":
                state.m = 0
                if family == "codex":
                    state.o = [now, 1]
        else:
            if outcome == "failed":
                state.f += 1
            reason = data.get("reason")
            if isinstance(reason, str) and reason:
                state.k = reason if reason in _STOP_REASON_CODES else "other"
        state.a = None
        state.p = None
        if state.f >= _FAILURE_PAUSE_THRESHOLD and not (state.s == "p" and state.why == "fail"):
            state.s, state.why = "p", "fail"
            notices.append(("fail", {}))

        updates: dict[str, str] = {}
        new_label = state.to_label()
        if new_label != raw_label:
            updates[KEEP_WARM_LABEL] = new_label
        if stats_dirty:
            new_stats = stats.to_label()
            if new_stats != conv.labels.get(KEEP_WARM_STATS_LABEL):
                updates[KEEP_WARM_STATS_LABEL] = new_stats
        if updates:
            await asyncio.to_thread(store.set_labels, session_id, updates)
        warm_after = warm_state_from_label(
            new_label,
            archived=False,
            harness=harness,
            busy=busy,
            now=now,
            family=family,
            codex_staleness_s=staleness_s,
            cold_after_s=cold_after,
        )
        if warm_after != warm_before or (state_warm_before and state.s != "w"):
            _publish_child_status_to_parent(session_id, None)
        if state.s == "w":
            self._tracked[session_id] = state.w or 0
        else:
            self._tracked.pop(session_id, None)
        return _ReceiptResult(True, conv, notices if is_child else [])

    def _turn_cache_reading(
        self,
        conv: Conversation,
        baseline: int | list[int] | None,
        turn_running_since: int,
        family: str | None,
    ) -> tuple[str, int, int] | None:
        """Classify one settled real turn's cache use; ``None`` is unknown."""
        if family == "claude":
            parsed = _parse_last_cache(conv.labels.get(LAST_CACHE_LABEL))
            if parsed is None:
                return None
            read, creation, observed_at = parsed
            floor = baseline if isinstance(baseline, int) and not isinstance(baseline, bool) else 0
            if observed_at <= turn_running_since or observed_at <= floor:
                return None
            return ("miss" if creation > read else "hit"), read, creation
        if family == "codex":
            floor = (
                baseline
                if (
                    isinstance(baseline, list)
                    and len(baseline) == 2
                    and all(_as_int(value) is not None for value in baseline)
                )
                else [0, 0]
            )
            usage = conv.session_usage or {}
            current_input = _as_int(usage.get("input_tokens")) or 0
            current_cached = _as_int(usage.get("cache_read_input_tokens")) or 0
            delta_input = max(0, current_input - int(floor[0]))
            delta_cached = max(0, current_cached - int(floor[1]))
            total = delta_input + delta_cached
            if total <= 0:
                return None
            if delta_cached / total < 0.5:
                return "miss", delta_cached, delta_input
            return "hit", delta_cached, delta_input
        return None

    def _usage_baseline(self, conv: Conversation, family: str | None) -> int | list[int]:
        """Capture the marker the next real turn's cache reading compares to."""
        if family == "codex":
            usage = conv.session_usage or {}
            return [
                _as_int(usage.get("input_tokens")) or 0,
                _as_int(usage.get("cache_read_input_tokens")) or 0,
            ]
        parsed = _parse_last_cache(conv.labels.get(LAST_CACHE_LABEL))
        return parsed[2] if parsed is not None else 0

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

    def _runner_online(self, session_id: str) -> bool | None:
        """Strict runner reachability; ``None`` when no lookup is wired."""
        if self._liveness_lookup is None:
            return None
        try:
            liveness = self._liveness_lookup([session_id]).get(session_id)
        except Exception:
            _logger.exception("Child keep-warm liveness lookup failed")
            return None
        return liveness.runner_online if liveness is not None else None

    def _host_okay(self, conv: Conversation) -> bool:
        """The host gate fails closed: an unreadable host blocks the ping."""
        try:
            return bool(self._host_ok(conv))
        except Exception:
            _logger.exception("Child keep-warm host check failed for %s", conv.id)
            return False

    def _drop_tracking(self, session_id: str) -> None:
        self._tracked.pop(session_id, None)
        self._outcomes.pop(session_id, None)

    def _spawn_ping(
        self, session_id: str, attempt_id: str, family: str | None, harness: str | None
    ) -> None:
        """Forward the ping in its own task so a slow runner never stalls a tick."""
        body = {
            "type": "keep_warm_ping",
            "attempt_id": attempt_id,
            "family": family,
            "harness": harness,
        }
        task = asyncio.create_task(
            self._forward_ping(session_id, body), name=f"child-keep-warm-ping-{session_id}"
        )
        self._ping_tasks.add(task)
        task.add_done_callback(self._ping_tasks.discard)

    async def _forward_ping(self, session_id: str, body: dict[str, Any]) -> None:
        """Forward one keep-warm ping; record a transport failure for the next tick."""
        try:
            accepted = await self._forward_control(session_id, body)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Child keep-warm ping failed for %s", session_id)
            accepted = False
        if not accepted:
            self._outcomes[session_id] = "error"

    async def _forward_touch(self, session_id: str) -> None:
        """Best-effort episode-start touch: re-arms the runner's idle clocks."""
        try:
            await self._forward_control(session_id, {"type": "keep_warm_touch"})
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Child keep-warm touch failed for %s", session_id)

    async def _send_notices(
        self, conv: Conversation, notices: list[tuple[str, dict[str, int]]]
    ) -> None:
        """Send one ``[System: ...]`` line per stop event to the mother."""
        parent_id = conv.parent_conversation_id
        if parent_id is None or self._notify_line is None:
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
        elif kind == "fail":
            body = "3 keep-warm pings failed; it restarts after the child's next real turn."
            prefix = "keep-warm paused"
        else:
            read = detail.get("read", 0)
            creation = detail.get("creation", 0)
            body = (
                "two keep-warm probes did not read the prompt cache "
                f"(last: read {read}, written {creation} tokens) — its harness "
                "likely keeps a shorter cache. Warming stays off for this child "
                "until it is moved to past and back."
            )
            prefix = "keep-warm paused"
        return f'[System: {prefix} for child "{title}" ({where}): {body}]'


def _receipt_miss_detail(family: str | None, data: dict[str, Any]) -> dict[str, int]:
    """Notice detail for a measured receipt miss: read vs written tokens."""
    read = _as_int(data.get("cache_read")) or 0
    if family == "codex":
        # Codex reports no cache write; the uncached share is the rewrite.
        total = _as_int(data.get("input_total")) or 0
        return {"read": read, "creation": max(0, total - read)}
    return {"read": read, "creation": _as_int(data.get("cache_write")) or 0}


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


def _running_since(conv: Conversation) -> int | None:
    """Return the persisted ``omnigent.running_since`` stamp, if any."""
    raw = conv.labels.get(RUNNING_SINCE_LABEL_KEY)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


__all__ = [
    "ARCHIVED_BY_LABEL",
    "ARCHIVE_EXEMPT_SINCE_LABEL",
    "ARCHIVE_REASON_LABEL",
    "KEEP_WARM_LABEL",
    "KEEP_WARM_STATS_LABEL",
    "LAST_CACHE_LABEL",
    "ChildKeepWarmSweeper",
    "cold_after_for_agent",
    "cold_after_for_session",
    "keep_warm_episode_active",
    "keep_warm_family_for_harness",
    "keep_warm_family_from_labels",
    "keep_warm_status_from_labels",
    "warm_state_for_labels",
    "warm_state_from_label",
]
