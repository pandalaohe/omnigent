"""Unit tests for :class:`~omnigent.server.child_keep_warm.ChildKeepWarmSweeper`.

Fakes for the control forward, host gate, liveness lookup, notifications,
clock and preferences store; a real SQLAlchemy conversation store (per-test
sqlite, from the ``db_uri`` fixture) so labels round-trip and the candidate
query runs for real. Every test drives ``_tick`` directly; label timestamps
are seeded relative to a base wall-clock now and the clock advances only by
minutes.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.runtime import pending_elicitations
from omnigent.server import child_keep_warm
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.child_keep_warm import (
    ARCHIVE_EXEMPT_SINCE_LABEL,
    ARCHIVE_REASON_LABEL,
    ARCHIVED_BY_LABEL,
    KEEP_WARM_LABEL,
    KEEP_WARM_STATS_LABEL,
    LAST_CACHE_LABEL,
    ChildKeepWarmSweeper,
    keep_warm_status_from_labels,
    warm_state_for_labels,
    warm_state_from_label,
)
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import Host
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

pytestmark = pytest.mark.asyncio

_CLAUDE_INTERVAL_S = 3300
_CODEX_INTERVAL_S = 1500
_MAX_S = 14400
_SLACK_S = 300
_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_MIRRORED_CLAUDE_WRAPPER = "claude-code-native-ui-subagent"
_MIRRORED_CODEX_WRAPPER = "codex-native-ui-subagent"
_HOST_ID = "0123456789abcdef0123456789abcdef"
_CLAUDE_AGENT = "c0ffee" * 5 + "c0"
_CODEX_AGENT = "badcad" * 5 + "ba"
_CLAUDE_SDK_AGENT = "decafe" * 5 + "de"


# ── Fakes ────────────────────────────────────────────────


class _Clock:
    """Injectable epoch-seconds clock."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class _Forward:
    """Records forwarded controls; scriptable acceptance or exception."""

    def __init__(self) -> None:
        self.accepted: bool = True
        self.raises: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session_id: str, body: dict[str, Any]) -> bool:
        self.calls.append({"session_id": session_id, "body": body})
        if self.raises is not None:
            raise self.raises
        return self.accepted

    def pings(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["body"]["type"] == "keep_warm_ping"]

    def touches(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["body"]["type"] == "keep_warm_touch"]


class _Liveness:
    """Every looked-up session reports the scripted runner reachability."""

    def __init__(self, runner_online: bool = True) -> None:
        self.runner_online = runner_online
        self.calls: list[list[str]] = []

    def __call__(self, session_ids: list[str]) -> dict[str, SessionLiveness]:
        self.calls.append(list(session_ids))
        return {
            session_id: SessionLiveness(
                runner_online=self.runner_online,
                host_online=None,
            )
            for session_id in session_ids
        }


class _HostOk:
    """Scripted host-liveness-plus-capability gate."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def __call__(self, _conv: Conversation) -> bool:
        return self.ok


class _Notices:
    """Records ``notify_line`` calls."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def __call__(self, parent_id: str, line: str) -> None:
        self.lines.append((parent_id, line))


class _Prefs:
    """Preferences store whose single owner envelope is scripted.

    ``gate`` blocks ``get`` (called in a worker thread) until a test releases
    it, so two coroutines can be raced through the sweeper's session lock;
    ``entered`` signals the first blocked read.
    """

    def __init__(self, keep_warm: dict[str, Any] | None = None) -> None:
        self.keep_warm = keep_warm
        self.collab: dict[str, Any] | None = None
        self.patches: list[tuple[str, str, dict[str, Any]]] = []
        self.gate: threading.Event | None = None
        self.entered: threading.Event | None = None

    def get(self, _owner: str) -> dict[str, Any] | None:
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            self.gate.wait(30)
        settings: dict[str, Any] = {}
        if self.keep_warm is not None:
            settings["keep_warm"] = self.keep_warm
        if self.collab is not None:
            settings["session_collab"] = self.collab
        return {"settings": settings} if settings else None

    def patch_namespace(self, owner: str, namespace: str, value: dict[str, Any]) -> None:
        self.patches.append((owner, namespace, value))
        if namespace == "keep_warm" and isinstance(value, dict):
            self.keep_warm = value


class _AgentListStore:
    """Agent store whose paged listing fails a scripted number of times."""

    def __init__(self, agents: list[Any], failures: int = 0) -> None:
        self.agents = list(agents)
        self.failures = failures
        self.calls = 0

    def list(self, *, limit: int, after: str | None = None) -> Any:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("agent store unavailable")
        return SimpleNamespace(data=list(self.agents), has_more=False, last_id=None)


class _FakeAgentCache:
    """Agent cache whose loads all resolve to one scripted harness."""

    def __init__(self, harness: str = "claude-native") -> None:
        self.harness = harness

    def load(self, agent_id: str, bundle_location: str, *, expand_env: bool) -> Any:
        executor = SimpleNamespace(config={"harness": self.harness}, type="native")
        return SimpleNamespace(spec=SimpleNamespace(executor=executor))


class _HarnessByAgentCache:
    """Agent cache resolving each agent id to its own scripted harness."""

    def __init__(self, harness_by_agent: dict[str, str]) -> None:
        self._harness_by_agent = harness_by_agent

    def load(self, agent_id: str, bundle_location: str, *, expand_env: bool) -> Any:
        del bundle_location, expand_env
        harness = self._harness_by_agent.get(agent_id, "claude-native")
        executor = SimpleNamespace(config={"harness": harness}, type="native")
        return SimpleNamespace(spec=SimpleNamespace(executor=executor))


@dataclass
class _Harness:
    store: SqlAlchemyConversationStore
    now: int
    clock: _Clock
    forward: _Forward
    host_ok: _HostOk
    notices: _Notices
    published: list[tuple[str, str | None]]
    prefs: _Prefs
    liveness: _Liveness
    sweeper: ChildKeepWarmSweeper


@pytest.fixture(autouse=True)
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Capture warm-state publishes instead of scheduling store reads."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        child_keep_warm,
        "_publish_child_status_to_parent",
        lambda session_id, status: calls.append((session_id, status)),
    )
    return calls


@pytest.fixture(autouse=True)
def _clean_pending_elicitations() -> Any:
    """Isolate the in-memory pending-elicitation index between tests."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture
def harness(db_uri: str, published: list[tuple[str, str | None]]) -> _Harness:
    """A sweeper wired to fakes and a real per-test sqlite store."""
    store = SqlAlchemyConversationStore(db_uri)
    now = int(time.time())
    clock = _Clock(now)
    forward = _Forward()
    host_ok = _HostOk()
    notices = _Notices()
    prefs = _Prefs(
        {
            "agents": {
                _CLAUDE_AGENT: {"main": True, "child": True},
                _CODEX_AGENT: {"main": True, "child": True},
            }
        }
    )
    liveness = _Liveness()
    sweeper = ChildKeepWarmSweeper(
        conversation_store=store,
        permission_store=None,
        liveness_lookup=liveness,
        forward_control=forward,
        host_ok=host_ok,
        notify_line=notices,
        clock=clock,
    )
    sweeper._app = SimpleNamespace(state=SimpleNamespace(user_preferences_store=prefs))
    return _Harness(
        store=store,
        now=now,
        clock=clock,
        forward=forward,
        host_ok=host_ok,
        notices=notices,
        published=published,
        prefs=prefs,
        liveness=liveness,
        sweeper=sweeper,
    )


# ── Helpers ──────────────────────────────────────────────


def _label(**fields: Any) -> str:
    """Serialize a warm-state label from keyword fields."""
    return child_keep_warm._WarmState(**fields).to_label()


def _warm_label(
    *,
    t: int,
    u: int | None = None,
    c: int | None = None,
    w: int | None = None,
    p: int | None = None,
    a: str | None = None,
    f: int = 0,
    m: int = 0,
    b: int | list[int] | None = None,
    v: int | None = None,
    o: list[int] | None = None,
    r: bool = False,
    k: str | None = None,
    interval: int = _CLAUDE_INTERVAL_S,
) -> str:
    """A state = warm label with sensible defaults."""
    touch = t if u is None else u
    return _label(
        s="w",
        t=t,
        c=t if c is None else c,
        u=touch,
        p=p,
        a=a,
        f=f,
        m=m,
        b=b,
        v=v,
        o=o,
        r=r,
        k=k,
        w=touch + interval + _SLACK_S if w is None else w,
    )


def _read_label(harness: _Harness, session_id: str) -> child_keep_warm._WarmState | None:
    conv = harness.store.get_conversation(session_id)
    assert conv is not None
    return child_keep_warm._WarmState.parse(conv.labels.get(KEEP_WARM_LABEL))


def _read_stats(harness: _Harness, session_id: str) -> child_keep_warm._WarmStats:
    conv = harness.store.get_conversation(session_id)
    assert conv is not None
    return child_keep_warm._WarmStats.parse(conv.labels.get(KEEP_WARM_STATS_LABEL))


def _parent(
    harness: _Harness,
    *,
    host_id: str | None = None,
    workspace: str | None = None,
    live_status: str | None = "running",
) -> Conversation:
    conv = harness.store.create_conversation(title="parent", host_id=host_id, workspace=workspace)
    if live_status is not None:
        harness.store.set_session_live_status(conv.id, live_status)
    loaded = harness.store.get_conversation(conv.id)
    assert loaded is not None
    return loaded


def _child(
    harness: _Harness,
    parent_id: str,
    *,
    harness_override: str = "claude-native",
    agent_id: str = _CLAUDE_AGENT,
    labels: dict[str, str] | None = None,
    live_status: str | None = "idle",
    title: str = "researcher:task",
    sub_agent_name: str | None = None,
    running_since: int | None = None,
) -> Conversation:
    conv = harness.store.create_conversation(
        kind="sub_agent",
        title=title,
        parent_conversation_id=parent_id,
        agent_id=agent_id,
        harness_override=harness_override,
        sub_agent_name=sub_agent_name,
    )
    if live_status is not None:
        harness.store.set_session_live_status(conv.id, live_status)
    if running_since is not None:
        harness.store.set_labels(conv.id, {RUNNING_SINCE_LABEL_KEY: str(running_since)})
    if labels:
        harness.store.set_labels(conv.id, labels)
    loaded = harness.store.get_conversation(conv.id)
    assert loaded is not None
    return loaded


def _main(
    harness: _Harness,
    *,
    harness_override: str = "claude-native",
    agent_id: str | None = _CLAUDE_AGENT,
    labels: dict[str, str] | None = None,
    live_status: str | None = "idle",
    title: str = "main session",
    running_since: int | None = None,
) -> Conversation:
    conv = harness.store.create_conversation(
        title=title,
        agent_id=agent_id,
        harness_override=harness_override,
    )
    if live_status is not None:
        harness.store.set_session_live_status(conv.id, live_status)
    if running_since is not None:
        harness.store.set_labels(conv.id, {RUNNING_SINCE_LABEL_KEY: str(running_since)})
    if labels:
        harness.store.set_labels(conv.id, labels)
    loaded = harness.store.get_conversation(conv.id)
    assert loaded is not None
    return loaded


def _set_running_since(harness: _Harness, session_id: str, running_since: int) -> None:
    harness.store.set_labels(session_id, {RUNNING_SINCE_LABEL_KEY: str(running_since)})


def _set_last_cache(
    harness: _Harness, session_id: str, *, read: int, creation: int, observed_at: int
) -> None:
    harness.store.set_labels(session_id, {LAST_CACHE_LABEL: f"{read},{creation},{observed_at}"})


async def _tick(harness: _Harness) -> None:
    """Run one tick and let every spawned ping forward finish."""
    await harness.sweeper._tick()
    if harness.sweeper._ping_tasks:
        await asyncio.gather(*list(harness.sweeper._ping_tasks))


async def _settle(harness: _Harness, session_id: str, **fields: Any) -> str:
    """Settle the session's pending attempt with the given receipt fields."""
    state = _read_label(harness, session_id)
    assert state is not None and state.a is not None
    data: dict[str, Any] = {"attempt_id": state.a, "outcome": "ok"}
    data.update(fields)
    assert await harness.sweeper.settle_receipt(session_id, data) is True
    return state.a


# ── Ping channel and eligibility ─────────────────────────


async def test_due_main_session_is_pinged_and_the_label_carries_the_attempt(
    harness: _Harness,
) -> None:
    """Scenario 1: an idle main Claude session past 55 min gets one control ping."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    pings = harness.forward.pings()
    assert len(pings) == 1
    assert pings[0]["session_id"] == main.id
    body = pings[0]["body"]
    assert body["type"] == "keep_warm_ping"
    assert body["family"] == "claude"
    assert body["harness"] == "claude-native"
    state = _read_label(harness, main.id)
    assert state is not None
    assert state.s == "w" and state.p == harness.now
    assert state.a is not None and body["attempt_id"] == state.a
    # The ping never travels as a conversation message.
    items = harness.store.list_latest_message_items_for_conversations([main.id], 10)
    assert not any(items.values())


async def test_claude_sdk_main_session_is_pinged(harness: _Harness) -> None:
    """A top-level claude-sdk session gets a claude-family ping on the SDK harness."""
    u = harness.now - 55 * 60
    main = _main(
        harness,
        harness_override="claude-sdk",
        labels={KEEP_WARM_LABEL: _warm_label(t=u)},
        running_since=u,
    )

    await _tick(harness)

    pings = harness.forward.pings()
    assert len(pings) == 1
    assert pings[0]["session_id"] == main.id
    body = pings[0]["body"]
    assert body["type"] == "keep_warm_ping"
    assert body["harness"] == "claude-sdk"
    assert body["family"] == "claude"


async def test_child_is_pinged_without_the_mother_present(harness: _Harness) -> None:
    """Scenario 2: no parent-presence gate — a long-idle mother changes nothing."""
    parent = _parent(harness, live_status="idle")
    harness.clock.now = harness.now + 3 * 3600
    now = harness.clock.now
    u = now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.sweeper._tracked[child.id] = u + _CLAUDE_INTERVAL_S + _SLACK_S

    await _tick(harness)

    assert len(harness.forward.pings()) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "w" and state.a is not None


async def test_sync_prompt_pending_blocks_the_ping(harness: _Harness) -> None:
    """Scenario 3: an own pending elicitation without ``async_kind`` → no ping."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    pending_elicitations.record_publish(
        main.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_sync",
            "params": {"message": "Allow running 'make build'?"},
        },
    )

    await _tick(harness)

    assert harness.forward.pings() == []
    state = _read_label(harness, main.id)
    assert state is not None and state.a is None


async def test_async_card_and_mirrored_item_do_not_block_the_ping(harness: _Harness) -> None:
    """Scenario 4: an async card is not a synchronous prompt; mirrored items ignored."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    pending_elicitations.record_publish(
        main.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_async",
            "params": {"message": "Question", "async_kind": "question"},
        },
    )
    pending_elicitations.record_publish(
        main.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_mirrored",
            "params": {"message": "Mirrored", "target_session_id": "conv_other"},
        },
    )

    await _tick(harness)

    assert len(harness.forward.pings()) == 1
    # The cards stay pending — the ping does not touch them.
    assert pending_elicitations.count_for(main.id) == 2


async def test_host_without_the_capability_is_not_pinged(harness: _Harness) -> None:
    """Scenario 21: a host not advertising ``keep_warm_v1`` is ineligible."""
    harness.host_ok.ok = False
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, main.id)
    assert state is not None and state.a is None


async def test_peer_messaging_off_still_pings(harness: _Harness) -> None:
    """Scenario 19: no peer sweeper (``notify_line=None``) — pings still flow."""
    sweeper = ChildKeepWarmSweeper(
        conversation_store=harness.store,
        permission_store=None,
        liveness_lookup=harness.liveness,
        forward_control=harness.forward,
        host_ok=harness.host_ok,
        notify_line=None,
        clock=harness.clock,
    )
    sweeper._app = harness.sweeper._app
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await sweeper._tick()
    if sweeper._ping_tasks:
        await asyncio.gather(*list(sweeper._ping_tasks))

    assert len(harness.forward.pings()) == 1
    state = _read_label(harness, main.id)
    assert state is not None and state.a is not None


async def test_child_not_yet_due_is_not_pinged(harness: _Harness) -> None:
    """30 minutes into the Claude window nothing happens."""
    parent = _parent(harness)
    u = harness.now - 30 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.p is None and state.a is None


async def test_codex_session_uses_the_shorter_interval(harness: _Harness) -> None:
    """A Codex session is due at 25 minutes, not 55."""
    u = harness.now - 25 * 60
    _main(
        harness,
        harness_override="codex-native",
        agent_id=_CODEX_AGENT,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, interval=_CODEX_INTERVAL_S)},
        running_since=u,
    )

    await _tick(harness)

    pings = harness.forward.pings()
    assert len(pings) == 1
    assert pings[0]["body"]["family"] == "codex"
    assert pings[0]["body"]["harness"] == "codex-native"


async def test_codex_sdk_session_is_pinged_with_its_own_harness(harness: _Harness) -> None:
    """The SDK Codex harness shares the family and interval, on its own harness id."""
    u = harness.now - 25 * 60
    session = _main(
        harness,
        harness_override="codex",
        agent_id=_CODEX_AGENT,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, interval=_CODEX_INTERVAL_S)},
        running_since=u,
    )

    await _tick(harness)

    pings = harness.forward.pings()
    assert len(pings) == 1
    assert pings[0]["session_id"] == session.id
    assert pings[0]["body"]["family"] == "codex"
    assert pings[0]["body"]["harness"] == "codex"


async def test_main_switch_off_keeps_the_main_session_cold(harness: _Harness) -> None:
    """The session's class picks the row's ``main`` / ``child`` switch."""
    harness.prefs.keep_warm = {"agents": {_CLAUDE_AGENT: {"main": False, "child": True}}}
    u = harness.now - 55 * 60
    _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    parent = _parent(harness)
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert [call["session_id"] for call in harness.forward.pings()] == [child.id]


async def test_agent_absent_from_the_settings_is_off(harness: _Harness) -> None:
    """Absent agent = off: no row for the bound agent, no ping."""
    harness.prefs.keep_warm = {"agents": {_CODEX_AGENT: {"main": True, "child": True}}}
    u = harness.now - 55 * 60
    _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.forward.calls == []


# ── Attempt lifecycle ────────────────────────────────────


async def test_pending_attempt_is_not_forwarded_twice(harness: _Harness) -> None:
    """A next tick before the receipt arrives forwards nothing new."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    first = _read_label(harness, main.id)
    assert first is not None and first.a is not None and first.p is not None

    harness.clock.now = first.p + 60
    await _tick(harness)

    assert len(harness.forward.pings()) == 1
    state = _read_label(harness, main.id)
    assert state is not None and state.a == first.a and state.p == first.p


async def test_attempt_timeout_counts_a_failure_and_retries_next_tick(
    harness: _Harness,
) -> None:
    """A pending attempt older than 180 s fails; a later tick pings again."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    first = _read_label(harness, main.id)
    assert first is not None and first.p is not None

    harness.clock.now = first.p + 181
    await _tick(harness)

    assert len(harness.forward.pings()) == 1, "the failure must not re-forward in the same tick"
    failed = _read_label(harness, main.id)
    assert failed is not None and failed.f == 1 and failed.a is None

    await _tick(harness)

    assert len(harness.forward.pings()) == 2
    retried = _read_label(harness, main.id)
    assert retried is not None and retried.a is not None and retried.p == harness.clock.now
    assert retried.a != first.a


async def test_forward_failure_counts_a_failure_and_retries_next_tick(
    harness: _Harness,
) -> None:
    """A forward the runner did not accept counts one failure, attempt cleared."""
    harness.forward.accepted = False
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)

    harness.clock.now += 60
    await _tick(harness)

    assert len(harness.forward.pings()) == 1
    failed = _read_label(harness, main.id)
    assert failed is not None and failed.f == 1 and failed.a is None

    harness.clock.now += 60
    await _tick(harness)
    assert len(harness.forward.pings()) == 2


async def test_forward_exception_counts_as_a_failure(harness: _Harness) -> None:
    """A raising forward is one failure, applied on the next tick."""
    harness.forward.raises = RuntimeError("runner gone")
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)

    harness.clock.now += 60
    await _tick(harness)

    failed = _read_label(harness, main.id)
    assert failed is not None and failed.f == 1 and failed.a is None


async def test_three_forward_failures_pause_warming(harness: _Harness) -> None:
    """f ≥ 3 → pause ``fail`` with one notice to the mother."""
    harness.forward.accepted = False
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    for _ in range(3):
        await _tick(harness)
        harness.clock.now += 60
        await _tick(harness)

    assert len(harness.forward.pings()) == 3
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "p" and state.why == "fail"
    assert len(harness.notices.lines) == 1
    assert "3 keep-warm pings failed" in harness.notices.lines[0][1]
    assert (child.id, None) in harness.published


# ── Session lock (tick vs receipt serialization) ─────────


async def test_concurrent_ticks_serialize_on_the_session_lock(harness: _Harness) -> None:
    """A second pass waits for the first, then reads the label it wrote."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={
            KEEP_WARM_LABEL: _warm_label(t=u, c=harness.now - _MAX_S - 1, w=harness.now + 3600)
        },
        running_since=u,
    )
    entered = threading.Event()
    release = threading.Event()
    harness.prefs.entered = entered
    harness.prefs.gate = release
    harness.sweeper._migrated_owners.add(RESERVED_USER_LOCAL)

    first = asyncio.create_task(harness.sweeper._process_session(child.id, harness.now, {}, {}))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(harness.sweeper._process_session(child.id, harness.now, {}, {}))
    await asyncio.sleep(0.1)
    assert not second.done(), "the second pass must wait on the session lock"
    release.set()
    await asyncio.gather(first, second)

    # The cap settled exactly once: the second pass read the cold label and
    # neither re-noticed nor pinged.
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"
    assert len(harness.notices.lines) == 1
    assert harness.forward.pings() == []


async def test_duplicate_concurrent_receipts_settle_exactly_once(harness: _Harness) -> None:
    """The lock makes two racing receipts for one attempt settle one time."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.a is not None
    entered = threading.Event()
    release = threading.Event()
    harness.prefs.entered = entered
    harness.prefs.gate = release
    harness.sweeper._migrated_owners.add(RESERVED_USER_LOCAL)

    receipt = {"attempt_id": state.a, "outcome": "ok", "cost_usd": 0.01}
    first = asyncio.create_task(harness.sweeper.settle_receipt(main.id, dict(receipt)))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(harness.sweeper.settle_receipt(main.id, dict(receipt)))
    await asyncio.sleep(0.1)
    assert not second.done(), "the second receipt must wait on the session lock"
    release.set()
    results = await asyncio.gather(first, second)

    assert results.count(True) == 1
    settled = _read_label(harness, main.id)
    assert settled is not None and settled.a is None and settled.u == harness.now
    assert _read_stats(harness, main.id).tot_p == 1


async def test_receipt_paused_mid_settle_cannot_undo_a_cap(harness: _Harness) -> None:
    """A receipt paused mid-settle blocks the tick; the cap still lands after.

    The receipt holds the session lock across its whole read→validate→write,
    so the tick cannot slip a ``cap`` write between the receipt's read and
    write — which the receipt's stale ``s = "w"`` copy would otherwise undo.
    """
    u = harness.now - 55 * 60
    main = _main(
        harness,
        labels={
            KEEP_WARM_LABEL: _warm_label(
                t=u, c=harness.now - _MAX_S - 1, w=harness.now + 3600, a="deadbeef"
            )
        },
        running_since=u,
    )
    entered = threading.Event()
    release = threading.Event()
    harness.prefs.entered = entered
    harness.prefs.gate = release
    harness.sweeper._migrated_owners.add(RESERVED_USER_LOCAL)

    receipt = asyncio.create_task(
        harness.sweeper.settle_receipt(main.id, {"attempt_id": "deadbeef", "outcome": "ok"})
    )
    assert await asyncio.to_thread(entered.wait, 5)
    tick = asyncio.create_task(_tick(harness))
    await asyncio.sleep(0.1)
    assert not tick.done(), "the tick must wait on the session lock"
    release.set()
    await asyncio.gather(receipt, tick)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "c" and state.why == "cap"


# ── Receipt settle ───────────────────────────────────────


async def test_ok_receipt_advances_the_touch_and_counts_stats(harness: _Harness) -> None:
    """An ok receipt sets u/window, clears failures, and counts ping + cost."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)

    await _settle(harness, child.id, cost_usd=0.03, estimated=True)

    state = _read_label(harness, child.id)
    assert state is not None
    assert state.u == harness.now
    assert state.f == 0 and state.a is None
    assert state.w == harness.now + _CLAUDE_INTERVAL_S + _SLACK_S
    stats = _read_stats(harness, child.id)
    assert stats.ep_p == 1 and stats.tot_p == 1
    assert stats.ep_c == 30000 and stats.tot_c == 30000
    assert stats.ep_e is True and stats.tot_e is True
    assert stats.ep_s == state.c


async def test_duplicate_or_unknown_receipt_is_ignored(harness: _Harness) -> None:
    """Scenario 18: a second receipt with the same attempt id settles nothing."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    attempt_id = await _settle(harness, main.id, cost_usd=0.01)
    settled = _read_label(harness, main.id)
    assert settled is not None

    assert (
        await harness.sweeper.settle_receipt(
            main.id, {"attempt_id": attempt_id, "outcome": "ok", "cost_usd": 0.01}
        )
        is False
    )
    assert (
        await harness.sweeper.settle_receipt(
            main.id, {"attempt_id": "deadbeef", "outcome": "ok", "cost_usd": 0.01}
        )
        is False
    )

    state = _read_label(harness, main.id)
    assert state is not None and state.u == settled.u
    stats = _read_stats(harness, main.id)
    assert stats.tot_p == 1


async def test_receipt_for_a_session_without_pending_attempt_is_ignored(
    harness: _Harness,
) -> None:
    """No pending attempt (or no label at all) → the receipt matches nothing."""
    main = _main(harness)
    assert (
        await harness.sweeper.settle_receipt(main.id, {"attempt_id": "abc123", "outcome": "ok"})
        is False
    )


async def test_skipped_receipt_clears_the_attempt_only(harness: _Harness) -> None:
    """``skipped`` clears the attempt and records its reason; the rest stays."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.a is not None

    assert (
        await harness.sweeper.settle_receipt(
            main.id, {"attempt_id": state.a, "outcome": "skipped", "reason": "card"}
        )
        is True
    )

    settled = _read_label(harness, main.id)
    assert settled is not None
    assert settled.a is None and settled.u == u and settled.f == 0
    assert settled.k == "card"
    assert _read_stats(harness, main.id) == child_keep_warm._WarmStats()


async def test_failed_receipts_pause_at_three(harness: _Harness) -> None:
    """Three ``failed`` receipts pause warming with one notice."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    for _ in range(3):
        await _tick(harness)
        state = _read_label(harness, child.id)
        assert state is not None and state.a is not None
        assert (
            await harness.sweeper.settle_receipt(
                child.id,
                {"attempt_id": state.a, "outcome": "failed", "reason": "tool_attempt"},
            )
            is True
        )
        harness.clock.now += 60

    paused = _read_label(harness, child.id)
    assert paused is not None and paused.s == "p" and paused.why == "fail"
    assert len(harness.notices.lines) == 1


async def test_claude_two_measured_misses_pause_warming(harness: _Harness) -> None:
    """Scenario 15: two receipts with ``cache_write > cache_read`` → sticky pause."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    await _settle(harness, child.id, cache_read=10, cache_write=90, input_total=100)
    after_first = _read_label(harness, child.id)
    assert after_first is not None
    assert after_first.s == "w" and after_first.m == 1 and after_first.f == 0
    assert after_first.u == harness.now
    assert harness.notices.lines == []

    harness.clock.now = after_first.u + _CLAUDE_INTERVAL_S
    await _tick(harness)
    await _settle(harness, child.id, cache_read=10, cache_write=95, input_total=105)

    paused = _read_label(harness, child.id)
    assert paused is not None
    assert paused.s == "p" and paused.why == "miss" and paused.m == 2
    assert paused.v == child.archive_revision
    assert len(harness.notices.lines) == 1
    assert "read 10, written 95 tokens" in harness.notices.lines[0][1]
    assert (child.id, None) in harness.published
    conv = harness.store.get_conversation(child.id)
    assert conv is not None
    assert (
        warm_state_from_label(
            conv.labels.get(KEEP_WARM_LABEL),
            archived=False,
            harness="claude-native",
            busy=False,
            now=harness.clock.now,
        )
        == "cold"
    )


async def test_codex_two_measured_misses_pause_and_record_the_observation(
    harness: _Harness,
) -> None:
    """Scenario 24: ``cache_read / input_total < 0.5`` twice → pause, o recorded."""
    u = harness.now - 25 * 60
    main = _main(
        harness,
        harness_override="codex-native",
        agent_id=_CODEX_AGENT,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, interval=_CODEX_INTERVAL_S)},
        running_since=u,
    )

    await _tick(harness)
    await _settle(harness, main.id, input_total=1000, cache_read=100)
    after_first = _read_label(harness, main.id)
    assert after_first is not None
    assert after_first.s == "w" and after_first.m == 1
    assert after_first.o == [harness.now, 0]

    harness.clock.now = after_first.u + _CODEX_INTERVAL_S
    await _tick(harness)
    await _settle(harness, main.id, input_total=1000, cache_read=400)

    paused = _read_label(harness, main.id)
    assert paused is not None
    assert paused.s == "p" and paused.why == "miss" and paused.m == 2
    assert paused.o == [harness.clock.now, 0]


async def test_claude_two_measured_cost_misses_pause_warming(harness: _Harness) -> None:
    """A measured ``cache_result`` wins over the token fields: two misses → sticky pause."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    await _settle(harness, child.id, cache_result="miss", cost_usd=0.08)
    after_first = _read_label(harness, child.id)
    assert after_first is not None
    assert after_first.s == "w" and after_first.m == 1 and after_first.f == 0
    assert after_first.u == harness.now
    assert harness.notices.lines == []

    first_u = after_first.u
    assert first_u is not None
    harness.clock.now = first_u + _CLAUDE_INTERVAL_S
    await _tick(harness)
    await _settle(harness, child.id, cache_result="miss", cost_usd=0.09)

    paused = _read_label(harness, child.id)
    assert paused is not None
    assert paused.s == "p" and paused.why == "miss" and paused.m == 2
    assert paused.v == child.archive_revision
    assert len(harness.notices.lines) == 1
    assert (child.id, None) in harness.published


async def test_measured_hit_resets_the_miss_count(harness: _Harness) -> None:
    """A hit between two misses keeps warming (consecutive, not cumulative)."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    await _settle(harness, child.id, cache_read=10, cache_write=90, input_total=100)
    first = _read_label(harness, child.id)
    assert first is not None and first.m == 1

    harness.clock.now = first.u + _CLAUDE_INTERVAL_S
    await _tick(harness)
    await _settle(harness, child.id, cache_read=900, cache_write=10, input_total=910)
    hit = _read_label(harness, child.id)
    assert hit is not None and hit.s == "w" and hit.m == 0


async def test_receipt_without_usage_fields_leaves_misses_unchanged(harness: _Harness) -> None:
    """A None field is unknown — never a miss — but the touch still advances."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    await _settle(harness, child.id)

    state = _read_label(harness, child.id)
    assert state is not None
    assert state.m == 0 and state.u == harness.now
    assert _read_stats(harness, child.id).tot_p == 1


# ── Stop reason visibility ───────────────────────────────


async def test_stop_reason_tracks_the_first_failing_gate(harness: _Harness) -> None:
    """k records switch → runner → host → card and clears once nothing blocks."""
    harness.prefs.keep_warm = {"agents": {_CLAUDE_AGENT: {"main": False, "child": False}}}
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.k == "switch"
    assert harness.forward.pings() == []

    harness.prefs.keep_warm = {"agents": {_CLAUDE_AGENT: {"main": True, "child": True}}}
    harness.liveness.runner_online = False
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.k == "runner"

    harness.liveness.runner_online = True
    harness.host_ok.ok = False
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.k == "host"

    harness.host_ok.ok = True
    pending_elicitations.record_publish(
        main.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_sync",
            "params": {"message": "Allow running 'make build'?"},
        },
    )
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.k == "card"

    # Every gate passes: the due ping goes out and the reason clears.
    pending_elicitations.reset_for_tests()
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.k is None and state.a is not None
    assert len(harness.forward.pings()) == 1


async def test_unmapped_receipt_reason_stores_other(harness: _Harness) -> None:
    """A reason outside the fixed code set stores ``other``; the label stays bounded.

    A 24-char non-ASCII reason JSON-escapes past the 256-char label bound, so
    only allowlisted codes are stored verbatim.
    """
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.a is not None

    assert (
        await harness.sweeper.settle_receipt(
            main.id, {"attempt_id": state.a, "outcome": "skipped", "reason": "暖" * 24}
        )
        is True
    )

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.k == "other"


async def test_ok_receipt_clears_a_recorded_stop_reason(harness: _Harness) -> None:
    """An ok receipt drops whatever k a tick or an earlier receipt recorded."""
    u = harness.now - 55 * 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, p=harness.now - 10, a="deadbeef", k="card")},
        running_since=u,
    )

    assert (
        await harness.sweeper.settle_receipt(main.id, {"attempt_id": "deadbeef", "outcome": "ok"})
        is True
    )

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.a is None and settled.k is None


# ── Episode start, cap, cold settles ─────────────────────


async def test_first_settled_real_turn_creates_the_episode_and_touches_once(
    harness: _Harness,
) -> None:
    """Rule 3: a new real turn settled → warm episode + one keep_warm_touch."""
    main = _main(harness, running_since=harness.now - 120)

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None
    assert state.s == "w" and state.t == harness.now - 120
    assert state.c == main.updated_at and state.u == main.updated_at
    assert state.w == main.updated_at + _CLAUDE_INTERVAL_S + _SLACK_S
    assert main.id in harness.sweeper._tracked
    assert [call["body"] for call in harness.forward.touches()] == [{"type": "keep_warm_touch"}]
    stats = _read_stats(harness, main.id)
    assert stats.ep_s == main.updated_at

    await _tick(harness)

    # No new episode, no second touch, and the session is not due yet.
    assert len(harness.forward.touches()) == 1
    assert harness.forward.pings() == []


async def test_touch_is_not_forwarded_when_the_switch_is_off(harness: _Harness) -> None:
    """The reaper-yield touch protects episodes that will be pinged — not others."""
    harness.prefs.keep_warm = {"agents": {_CLAUDE_AGENT: {"main": False, "child": False}}}
    main = _main(harness, running_since=harness.now - 120)

    await _tick(harness)

    # The episode is still tracked (the pill reads warm); no control flies.
    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w"
    assert harness.forward.calls == []


async def test_new_real_turn_after_cap_starts_a_new_episode_and_touches(
    harness: _Harness,
) -> None:
    """A changed ``running_since`` re-arms a cold session."""
    parent = _parent(harness)
    old_r = harness.now - 3 * 3600
    child = _child(
        harness,
        parent.id,
        labels={
            KEEP_WARM_LABEL: _label(s="c", why="cap", t=old_r, c=old_r, u=old_r, w=old_r + 3600)
        },
    )
    _set_running_since(harness, child.id, old_r + 500)

    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None
    assert state.s == "w" and state.t == old_r + 500 and state.f == 0 and state.m == 0
    assert child.id in harness.sweeper._tracked
    assert len(harness.forward.touches()) == 1


async def test_episode_counters_reset_on_the_next_real_turn(harness: _Harness) -> None:
    """Episode counters reset at rule 3; the lifetime totals keep running."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    await _settle(harness, child.id, cost_usd=0.03)
    stats = _read_stats(harness, child.id)
    assert stats.ep_p == 1 and stats.ep_c == 30000

    _set_running_since(harness, child.id, u + 500)
    await _tick(harness)

    stats = _read_stats(harness, child.id)
    assert stats.ep_p == 0 and stats.ep_c == 0
    assert stats.tot_p == 1 and stats.tot_c == 30000


async def test_cap_goes_cold_with_one_notice_naming_agent_host_cwd(harness: _Harness) -> None:
    """Scenario 5: 4 h 1 min after the episode start → cold + one named notice."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/opt/work/omnigent/fork/wt")
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        sub_agent_name="researcher",
        labels={
            KEEP_WARM_LABEL: _warm_label(t=u, c=harness.now - _MAX_S - 1, w=harness.now + 3600)
        },
        running_since=u,
    )

    await _tick(harness)

    assert harness.forward.pings() == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"
    assert len(harness.notices.lines) == 1
    line = harness.notices.lines[0][1]
    assert f"researcher · {_HOST_ID} · /opt/work/omnigent/fork/wt" in line
    assert "reached the 4 h limit" in line


async def test_pings_do_not_reset_the_cap(harness: _Harness) -> None:
    """Scenario 6: pings at 55/110/165/220 min; the 241-min tick sends nothing."""
    parent = _parent(harness)
    t0 = harness.now
    child = _child(
        harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=t0)}, running_since=t0
    )

    for minutes in (55, 110, 165, 220):
        harness.clock.now = t0 + minutes * 60
        await _tick(harness)
        await _settle(harness, child.id)

    harness.clock.now = t0 + 241 * 60
    await _tick(harness)

    assert len(harness.forward.pings()) == 4
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"


async def test_ok_pings_with_unchanged_running_since_hold_the_episode_start(
    harness: _Harness,
) -> None:
    """
    An idle claude-native child whose ok ping receipts settle while
    ``running_since`` never changes keeps its episode start (``c``); at
    ``c + max_s`` the label is cold/``cap`` and nothing more is due. A
    ping that reads as a new real turn would restart the 4 h cap on
    every ping.
    """
    parent = _parent(harness)
    child = _child(harness, parent.id, running_since=harness.now - 120)

    await _tick(harness)  # the real turn settles: the episode opens
    opened = _read_label(harness, child.id)
    assert opened is not None and opened.s == "w"
    c0 = opened.c
    assert c0 is not None

    last_u = opened.u
    assert last_u is not None
    for _ in range(4):
        harness.clock.now = last_u + _CLAUDE_INTERVAL_S
        await _tick(harness)
        await _settle(harness, child.id, cost_usd=0.01)
        state = _read_label(harness, child.id)
        # running_since never moved: no new episode, the cap clock stands.
        assert state is not None and state.c == c0 and state.t == opened.t
        last_u = state.u
        assert last_u is not None

    harness.clock.now = c0 + _MAX_S
    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"
    assert len(harness.forward.pings()) == 4

    harness.clock.now = c0 + _MAX_S + _CLAUDE_INTERVAL_S
    await _tick(harness)

    # Cold stays cold: no new real turn, so no further ping is due.
    assert len(harness.forward.pings()) == 4
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"


async def test_window_passed_while_runner_offline_goes_cold_silently(
    harness: _Harness,
) -> None:
    """Tracked warm child, runner offline, window passed → cold, no notice."""
    harness.liveness.runner_online = False
    parent = _parent(harness)
    u = harness.now - 2 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=harness.now - 60)},
        running_since=u,
    )
    harness.sweeper._tracked[child.id] = harness.now - 60

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert harness.notices.lines == []
    assert (child.id, None) in harness.published


async def test_archived_mother_child_goes_cold_silently(harness: _Harness) -> None:
    """A mother-archived child settles cold with no notice."""
    parent = _parent(harness)
    u = harness.now - 2 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=harness.now - 60)},
        running_since=u,
    )
    harness.store.update_conversation(parent.id, archived=True)

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert harness.notices.lines == []


async def test_archived_session_is_dropped_from_tracking(harness: _Harness) -> None:
    """An archived session is never pinged and stops being tracked."""
    u = harness.now - 55 * 60
    main = _main(harness, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.store.update_conversation(main.id, archived=True)
    harness.sweeper._tracked[main.id] = harness.now + 100

    await _tick(harness)

    assert harness.forward.calls == []
    assert main.id not in harness.sweeper._tracked
    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.a is None


async def test_closed_session_is_not_pinged(harness: _Harness) -> None:
    """A closed session is ineligible for a ping."""
    u = harness.now - 55 * 60
    main = _main(
        harness,
        labels={
            KEEP_WARM_LABEL: _warm_label(t=u),
            CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE,
        },
        running_since=u,
    )

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, main.id)
    assert state is not None and state.a is None


async def test_switch_off_goes_cold_at_the_window_without_notice(harness: _Harness) -> None:
    """Agent switch off → the tracked warm label expires cold, silently."""
    harness.prefs.keep_warm = {"agents": {_CLAUDE_AGENT: {"main": True, "child": False}}}
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=harness.now - 60)},
        running_since=u,
    )

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert harness.notices.lines == []
    assert harness.published  # the pill still flips warm → cold


async def test_owner_with_no_stored_settings_is_off_with_no_ping_or_label_churn(
    harness: _Harness,
) -> None:
    """Default-off: an owner with no stored row pings nothing.

    The only moves the switch-off rules allow are the window passing over a
    tracked warm label — cold ``exp``, no notice — and the one-time stop
    reason on a due label; after that a label must not be rewritten at all.
    """
    harness.prefs.keep_warm = None
    parent = _parent(harness)
    due = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=harness.now - 55 * 60)},
        running_since=harness.now - 55 * 60,
        title="researcher:due",
    )
    expired = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=harness.now - 2 * 3600, w=harness.now - 60)},
        running_since=harness.now - 2 * 3600,
        title="researcher:expired",
    )

    await _tick(harness)

    assert harness.forward.calls == []
    assert harness.notices.lines == []
    state = _read_label(harness, due.id)
    assert state is not None and state.p is None and state.a is None
    assert state.k == "switch"
    stored = harness.store.get_conversation(due.id)
    assert stored is not None
    settled_label = stored.labels[KEEP_WARM_LABEL]
    expired_state = _read_label(harness, expired.id)
    assert expired_state is not None and expired_state.s == "c" and expired_state.why == "exp"
    assert harness.published == [(expired.id, None)]

    await _tick(harness)

    stored = harness.store.get_conversation(due.id)
    assert stored is not None and stored.labels[KEEP_WARM_LABEL] == settled_label


async def test_unsupported_mirrored_and_never_ran_sessions_never_get_a_label(
    harness: _Harness,
) -> None:
    """Unsupported harnesses, mirrored wrappers, and never-ran rows stay label-free."""
    parent = _parent(harness)
    due = harness.now - 55 * 60
    opencode = _child(
        harness,
        parent.id,
        harness_override="opencode-native",
        title="researcher:opencode",
        running_since=due,
    )
    mirrored_claude = _child(
        harness,
        parent.id,
        labels={_WRAPPER_LABEL_KEY: _MIRRORED_CLAUDE_WRAPPER},
        running_since=due,
        title="researcher:mirrored-claude",
    )
    mirrored_codex = _child(
        harness,
        parent.id,
        harness_override="codex-native",
        labels={_WRAPPER_LABEL_KEY: _MIRRORED_CODEX_WRAPPER},
        running_since=due,
        title="researcher:mirrored-codex",
    )
    opencode_main = _main(
        harness,
        harness_override="opencode-native",
        title="opencode main",
        running_since=due,
    )
    never_ran = _child(harness, parent.id, title="researcher:never-ran")

    await _tick(harness)

    assert harness.forward.calls == []
    for conv in (opencode, mirrored_claude, mirrored_codex, opencode_main, never_ran):
        stored = harness.store.get_conversation(conv.id)
        assert stored is not None and KEEP_WARM_LABEL not in stored.labels


# ── Miss pause stickiness ────────────────────────────────


async def test_sticky_miss_pause_lifts_on_revision_and_rearms_on_real_turn(
    harness: _Harness,
) -> None:
    """Revision bump lifts the pause; only a later turn re-arms.

    The lift moves the watermark to the session's current ``running_since``,
    so the turn that was already there when the pause lifted is not read as
    new work: re-arming would ping the cold cache the pause was about.
    """
    parent = _parent(harness)
    r = harness.now - 3 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _label(s="p", why="miss", t=r, c=r, u=r, w=r + 3600, v=0, m=2)},
    )
    _set_running_since(harness, child.id, r + 1000)
    harness.store.update_conversation(child.id, archived=True)
    harness.store.update_conversation(child.id, archived=False)

    await _tick(harness)

    lifted = _read_label(harness, child.id)
    assert lifted is not None and lifted.s == "c" and lifted.why == "rev"
    assert lifted.t == r + 1000 and lifted.m == 0
    assert harness.forward.calls == []

    # The watermark turn is not new work: the label stays cold.
    await _tick(harness)
    still = _read_label(harness, child.id)
    assert still is not None and still.s == "c" and still.why == "rev"
    assert harness.forward.calls == []

    _set_running_since(harness, child.id, r + 2000)
    await _tick(harness)

    warm = _read_label(harness, child.id)
    assert warm is not None and warm.s == "w" and warm.t == r + 2000


async def test_unarchive_does_not_rearm_a_stale_cache_clock(harness: _Harness) -> None:
    """Unarchive keeps ``u``; the passed window stays cold."""
    parent = _parent(harness)
    r = harness.now - 2 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=r, u=r, w=r + _CLAUDE_INTERVAL_S + _SLACK_S)},
    )
    _set_running_since(harness, child.id, r)
    harness.store.update_conversation(child.id, archived=True)
    harness.store.update_conversation(child.id, archived=False)

    await _tick(harness)

    assert harness.forward.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"


# ── Real-turn cache classification (D15) ─────────────────


async def test_first_real_turn_after_an_absence_records_the_return(
    harness: _Harness,
) -> None:
    """A return past the family TTL is classified from ``omnigent.last_cache``.

    The measured miss is recorded as the return but NOT counted toward the
    miss pause: the turn began after the previous episode's window with no
    ok ping behind it, so keep-warm was not holding the cache for it.
    """
    t0 = harness.now - 2 * 3600
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0, w=t0 + 3600)},
        running_since=r,
    )
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)

    await _tick(harness)

    stats = _read_stats(harness, main.id)
    assert stats.lr_at == main.updated_at and stats.lr_r == "miss"
    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.m == 0
    # The consumed reading is the new baseline: a second tick reclassifies nothing.
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.m == 0


async def test_turn_with_no_reading_retries_the_classification_late(
    harness: _Harness,
) -> None:
    """An unmeasurable opening reading flags r; a later tick settles it.

    This turn began after the previous episode's window with no ok ping
    behind it, so the episode opened ineligible (``e`` stays unset): the
    late miss settles the return record but is not counted toward the pause.
    """
    t0 = harness.now - 2 * 3600
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0, w=t0 + 3600)},
        running_since=r,
    )

    # No ``omnigent.last_cache`` yet: the return records ``unknown`` and the
    # episode stays open for a late reading.
    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.r is True and state.e is False
    stats = _read_stats(harness, main.id)
    assert stats.lr_at == main.updated_at and stats.lr_r == "unknown"

    # The harness's usage write lands late; the next tick classifies the turn
    # against the stored baseline and settles the return record.
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)
    harness.clock.now += 60
    await _tick(harness)

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.s == "w" and settled.r is False
    assert settled.e is False and settled.m == 0
    stats = _read_stats(harness, main.id)
    assert stats.lr_at == main.updated_at and stats.lr_r == "miss"


async def test_late_reading_retry_gives_up_after_the_grace(harness: _Harness) -> None:
    """A reading that never lands stops retrying 300 s after the episode opened."""
    t0 = harness.now - 2 * 3600
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0, w=t0 + 3600)},
        running_since=r,
    )
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.r is True

    harness.clock.now += 301
    await _tick(harness)

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.s == "w" and settled.r is False
    assert settled.m == 0
    stats = _read_stats(harness, main.id)
    assert stats.lr_r == "unknown"
    assert harness.forward.pings() == []


async def test_real_turn_inside_the_ttl_records_no_return(harness: _Harness) -> None:
    """A turn inside the family TTL is no return; the hit still counts."""
    t0 = harness.now - 30 * 60
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0)},
        running_since=r,
    )
    _set_last_cache(harness, main.id, read=900, creation=10, observed_at=r + 30)

    await _tick(harness)

    stats = _read_stats(harness, main.id)
    assert stats.lr_at is None and stats.lr_r is None
    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.m == 0


async def test_codex_episode_records_observation_and_return(harness: _Harness) -> None:
    """Codex real turn: the usage delta is the observation and the return."""
    t0 = harness.now - 2 * 3600
    r = harness.now - 60
    main = _main(
        harness,
        harness_override="codex-native",
        agent_id=_CODEX_AGENT,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0, interval=_CODEX_INTERVAL_S, w=t0 + 1800)},
        running_since=r,
    )
    harness.store.set_session_usage(main.id, {"input_tokens": 100, "cache_read_input_tokens": 900})

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w"
    assert state.o == [main.updated_at, 1]
    stats = _read_stats(harness, main.id)
    assert stats.lr_at == main.updated_at and stats.lr_r == "hit"
    conv = harness.store.get_conversation(main.id)
    assert conv is not None
    assert (
        warm_state_from_label(
            conv.labels.get(KEEP_WARM_LABEL),
            archived=False,
            harness="codex-native",
            busy=False,
            now=harness.clock.now,
        )
        == "warm"
    )


async def test_codex_real_turn_miss_counts_toward_the_pause(harness: _Harness) -> None:
    """A real-turn miss inside a ping-kept warm window counts; two pause."""
    parent = _parent(harness)
    t0 = harness.now - 1200
    r = harness.now - 120
    child = _child(
        harness,
        parent.id,
        harness_override="codex-native",
        agent_id=_CODEX_AGENT,
        labels={
            # Warm, the window still open at the turn, one ok ping behind it.
            KEEP_WARM_LABEL: _warm_label(t=t0, interval=_CODEX_INTERVAL_S),
            KEEP_WARM_STATS_LABEL: child_keep_warm._WarmStats(ep_p=1).to_label(),
        },
        running_since=r,
    )
    harness.store.set_session_usage(
        child.id, {"input_tokens": 900, "cache_read_input_tokens": 100}
    )
    await _tick(harness)
    first = _read_label(harness, child.id)
    assert first is not None and first.s == "w" and first.m == 1
    assert first.o == [child.updated_at, 0]

    # The episode reset the episode counters; the next turn's miss counts
    # only with an ok ping behind this episode too (set directly here).
    harness.store.set_labels(
        child.id, {KEEP_WARM_STATS_LABEL: child_keep_warm._WarmStats(ep_p=1).to_label()}
    )
    r2 = r + 500
    _set_running_since(harness, child.id, r2)
    harness.store.set_session_usage(
        child.id, {"input_tokens": 1800, "cache_read_input_tokens": 200}
    )
    harness.clock.now += 60
    await _tick(harness)

    paused = _read_label(harness, child.id)
    assert paused is not None and paused.s == "p" and paused.why == "miss" and paused.m == 2
    assert len(harness.notices.lines) == 1


async def test_fresh_sessions_first_real_turn_miss_does_not_count(
    harness: _Harness,
) -> None:
    """A fresh session's first real turn measures a miss that is not keep-warm's."""
    main = _main(harness, running_since=harness.now - 60)
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=harness.now - 30)

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.m == 0
    assert harness.notices.lines == []


async def test_real_turn_miss_after_the_window_does_not_count(harness: _Harness) -> None:
    """Warm prior episode with pings, but the turn began after ``w`` — no blame."""
    t0 = harness.now - 2 * 3600
    r = harness.now - 60
    main = _main(
        harness,
        labels={
            KEEP_WARM_LABEL: _warm_label(t=t0, w=t0 + 3600),  # the window closed before r
            KEEP_WARM_STATS_LABEL: child_keep_warm._WarmStats(ep_p=1).to_label(),
        },
        running_since=r,
    )
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.m == 0


async def test_real_turn_miss_without_a_prior_ok_ping_does_not_count(
    harness: _Harness,
) -> None:
    """Inside the window but keep-warm never pinged — the opening miss is not counted."""
    t0 = harness.now - 1200
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0)},  # the window covers r; ep_p == 0
        running_since=r,
    )
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.m == 0


async def test_late_reading_miss_counts_when_the_episode_was_eligible(
    harness: _Harness,
) -> None:
    """A late-landing miss counts when the episode opened inside a ping-kept window."""
    t0 = harness.now - 1200
    r = harness.now - 60
    main = _main(
        harness,
        labels={
            KEEP_WARM_LABEL: _warm_label(t=t0),
            KEEP_WARM_STATS_LABEL: child_keep_warm._WarmStats(ep_p=1).to_label(),
        },
        running_since=r,
    )

    # No ``omnigent.last_cache`` yet: the episode opens unmeasurable but eligible.
    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.r is True and state.e is True

    # The turn's reading lands late; because the episode was eligible, the
    # miss counts toward the pause.
    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)
    harness.clock.now += 60
    await _tick(harness)

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.s == "w" and settled.r is False
    assert settled.e is False and settled.m == 1


async def test_late_reading_miss_does_not_count_when_the_episode_was_ineligible(
    harness: _Harness,
) -> None:
    """A late-landing miss never counts when the episode opened with no ok ping."""
    t0 = harness.now - 1200
    r = harness.now - 60
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=t0)},  # inside the window, no pings yet
        running_since=r,
    )

    await _tick(harness)

    state = _read_label(harness, main.id)
    assert state is not None and state.s == "w" and state.r is True and state.e is False

    _set_last_cache(harness, main.id, read=10, creation=90, observed_at=r + 30)
    harness.clock.now += 60
    await _tick(harness)

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.s == "w" and settled.r is False
    assert settled.e is False and settled.m == 0


async def test_late_codex_reading_advances_the_usage_baseline(harness: _Harness) -> None:
    """A late-consumed reading becomes the baseline the next episode measures.

    Without the advance, the next turn's cumulative usage compares against
    the pre-reading baseline and its own warm delta reads as a second miss.
    """
    t0 = harness.now - 1200
    r = harness.now - 60
    main = _main(
        harness,
        harness_override="codex-native",
        agent_id=_CODEX_AGENT,
        labels={
            # Warm, the window still open at the turn, one ok ping behind it.
            KEEP_WARM_LABEL: _warm_label(t=t0, interval=_CODEX_INTERVAL_S),
            KEEP_WARM_STATS_LABEL: child_keep_warm._WarmStats(ep_p=1).to_label(),
        },
        running_since=r,
    )

    # No usage yet: the episode opens with the classification pending.
    await _tick(harness)
    state = _read_label(harness, main.id)
    assert state is not None and state.r is True and state.e is True

    # The turn's usage lands late: a [900, 100] delta reads as a miss.
    harness.store.set_session_usage(main.id, {"input_tokens": 900, "cache_read_input_tokens": 100})
    harness.clock.now += 60
    await _tick(harness)

    settled = _read_label(harness, main.id)
    assert settled is not None and settled.r is False and settled.e is False and settled.m == 1
    assert settled.b == [900, 100]

    # The next turn adds [10, 90]: against the advanced baseline, a hit.
    _set_running_since(harness, main.id, r + 500)
    harness.store.set_session_usage(main.id, {"input_tokens": 910, "cache_read_input_tokens": 190})
    harness.clock.now += 60
    await _tick(harness)

    nxt = _read_label(harness, main.id)
    assert nxt is not None and nxt.s == "w" and nxt.m == 0
    assert nxt.o is not None and nxt.o[1] == 1


# ── Legacy migration ─────────────────────────────────────


async def test_migration_defers_when_agent_enumeration_fails(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enumeration error skips the migration; the next tick retries it.

    Migrating against a failed (empty) enumeration would write an empty
    ``keep_warm`` namespace and switch legacy warming off for good, so the
    owner must stay un-migrated until a listing succeeds.
    """
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: _FakeAgentCache())
    agent = SimpleNamespace(id=_CLAUDE_AGENT, bundle_location="/fake/claude.zip", session_id=None)
    agent_store = _AgentListStore([agent], failures=1)
    harness.sweeper._app.state.agent_store = agent_store
    harness.prefs.keep_warm = None
    harness.prefs.collab = {"enabled": True, "childKeepWarmEnabled": True}
    _main(harness, running_since=harness.now - 120)

    await _tick(harness)

    assert harness.prefs.patches == []
    assert RESERVED_USER_LOCAL not in harness.sweeper._migrated_owners

    await _tick(harness)

    assert RESERVED_USER_LOCAL in harness.sweeper._migrated_owners
    assert len(harness.prefs.patches) == 1
    owner, namespace, value = harness.prefs.patches[0]
    assert owner == RESERVED_USER_LOCAL and namespace == "keep_warm"
    row = value["agents"].get(_CLAUDE_AGENT)
    assert row is not None and row["child"] is True and row["main"] is False


async def test_migration_ignores_sdk_agents(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy migration covers the native agents only, never the SDK agents."""
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_cache",
        lambda: _HarnessByAgentCache(
            {
                _CLAUDE_AGENT: "claude-native",
                _CODEX_AGENT: "codex",
                _CLAUDE_SDK_AGENT: "claude-sdk",
            }
        ),
    )
    native = SimpleNamespace(id=_CLAUDE_AGENT, bundle_location="/fake/claude.zip", session_id=None)
    sdk_codex = SimpleNamespace(
        id=_CODEX_AGENT, bundle_location="/fake/codex.zip", session_id=None
    )
    sdk_claude = SimpleNamespace(
        id=_CLAUDE_SDK_AGENT, bundle_location="/fake/claude-sdk.zip", session_id=None
    )
    harness.sweeper._app.state.agent_store = _AgentListStore([native, sdk_codex, sdk_claude])
    harness.prefs.keep_warm = None
    harness.prefs.collab = {"enabled": True, "childKeepWarmEnabled": True}
    _main(harness, running_since=harness.now - 120)

    await _tick(harness)

    assert len(harness.prefs.patches) == 1
    _owner, namespace, value = harness.prefs.patches[0]
    assert namespace == "keep_warm"
    assert _CLAUDE_AGENT in value["agents"]
    assert _CODEX_AGENT not in value["agents"]
    assert _CLAUDE_SDK_AGENT not in value["agents"]


# ── warm_state rule ──────────────────────────────────────


async def test_warm_state_from_label_derives_the_pill() -> None:
    """Claude clock rule; busy override; archived / unsupported / none."""
    now = 1_000_000
    warm = _label(s="w", t=1, u=now - 100, w=now + 100)
    stale_touch = _label(s="w", t=1, u=now - 3601, w=now + 100)
    assert (
        warm_state_from_label(warm, archived=False, harness="claude-native", busy=False, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(
            stale_touch, archived=False, harness="claude-native", busy=False, now=now
        )
        == "cold"
    )
    assert (
        warm_state_from_label(warm, archived=False, harness="claude-native", busy=True, now=now)
        == "warm"
    )
    paused = _label(s="p", why="fail", t=1, u=now - 100)
    assert (
        warm_state_from_label(paused, archived=False, harness="claude-native", busy=False, now=now)
        == "cold"
    )
    # A busy session is warm whatever the label says or when there is none.
    assert (
        warm_state_from_label(None, archived=False, harness="claude-native", busy=True, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(paused, archived=False, harness="claude-native", busy=True, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(None, archived=False, harness="claude-native", busy=False, now=now)
        is None
    )
    assert (
        warm_state_from_label(warm, archived=True, harness="claude-native", busy=False, now=now)
        is None
    )
    assert (
        warm_state_from_label(warm, archived=False, harness="opencode-native", busy=False, now=now)
        is None
    )
    assert (
        warm_state_from_label(warm, archived=True, harness="claude-native", busy=True, now=now)
        is None
    )
    assert (
        warm_state_from_label(warm, archived=False, harness="opencode-native", busy=True, now=now)
        is None
    )


async def test_codex_warm_state_follows_the_latest_observation() -> None:
    """Scenario 24: hit 20 min ago → warm; a miss or a stale hit → cold."""
    now = 1_000_000
    hit = _label(s="w", t=1, u=now - 1200, w=now, o=[now - 1200, 1])
    miss = _label(s="w", t=1, u=now - 1200, w=now, o=[now - 1200, 0])
    stale = _label(s="w", t=1, u=now - 2400, w=now, o=[now - 2400, 1])
    none_seen = _label(s="w", t=1, u=now - 100, w=now + 100)
    assert (
        warm_state_from_label(hit, archived=False, harness="codex-native", busy=False, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(miss, archived=False, harness="codex-native", busy=False, now=now)
        == "cold"
    )
    assert (
        warm_state_from_label(stale, archived=False, harness="codex-native", busy=False, now=now)
        == "cold"
    )
    assert (
        warm_state_from_label(
            none_seen, archived=False, harness="codex-native", busy=False, now=now
        )
        == "cold"
    )
    # The staleness bound is the agent's interval + 300 s when configured.
    assert (
        warm_state_from_label(
            stale,
            archived=False,
            harness="codex-native",
            busy=False,
            now=now,
            codex_staleness_s=3600,
        )
        == "warm"
    )
    assert (
        warm_state_from_label(hit, archived=False, harness="codex-native", busy=True, now=now)
        == "warm"
    )


async def test_codex_warm_state_reads_a_fresh_settle_without_an_observation() -> None:
    """An observation-less codex episode reads warm while its real turn's
    settle is inside the staleness bound; a recorded o still outranks it."""
    now = 1_000_000
    fresh_settle = _label(s="w", t=1, c=now - 60, u=now - 60)
    stale_settle = _label(s="w", t=1, c=now - 1801, u=now - 1801)
    miss_with_fresh_settle = _label(s="w", t=1, c=now - 60, u=now - 60, o=[now - 120, 0])
    hit_with_fresh_settle = _label(s="w", t=1, c=now - 60, u=now - 60, o=[now - 120, 1])

    def state(label: str, **kwargs: int) -> str | None:
        return warm_state_from_label(
            label,
            archived=False,
            harness="codex-native",
            busy=False,
            now=now,
            **kwargs,
        )

    assert state(fresh_settle) == "warm"
    assert state(stale_settle) == "cold"
    assert state(miss_with_fresh_settle) == "cold"
    assert state(hit_with_fresh_settle) == "warm"
    # The staleness bound is the agent's interval + 300 s when configured.
    assert state(stale_settle, codex_staleness_s=3600) == "warm"


async def test_warm_state_label_round_trips_the_family() -> None:
    """The family stamp survives a serialize / parse cycle; bad values drop."""
    state = child_keep_warm._WarmState(s="w", y="claude", t=1)
    serialized = state.to_label()
    parsed = child_keep_warm._WarmState.parse(serialized)
    assert parsed is not None
    assert parsed.y == "claude"
    assert '"y":"claude"' in serialized
    # A label without the stamp, or with an unknown family, reads no family.
    assert child_keep_warm._WarmState.parse(_label(s="w", t=1)).y is None
    assert child_keep_warm._WarmState.parse(_label(s="w", y="other", t=1)).y is None


async def test_warm_state_for_labels_uses_the_family_stamp() -> None:
    """No harness lookup: the label's ``y`` picks the per-family rule."""
    now = 1_000_000
    claude_warm = _label(s="w", y="claude", t=1, u=now - 100, w=now + 100)
    claude_stale = _label(s="w", y="claude", t=1, u=now - 3601, w=now + 100)
    codex_hit = _label(s="w", y="codex", t=1, u=now - 1200, w=now, o=[now - 1200, 1])
    codex_stale = _label(s="w", y="codex", t=1, u=now - 2400, w=now, o=[now - 2400, 1])
    codex_miss = _label(s="w", y="codex", t=1, u=now - 1200, w=now, o=[now - 1200, 0])

    def _state(labels: dict[str, str], *, archived: bool = False, busy: bool = False) -> Any:
        return warm_state_for_labels(labels, archived=archived, busy=busy, now=now)

    assert _state({KEEP_WARM_LABEL: claude_warm}) == "warm"
    assert _state({KEEP_WARM_LABEL: claude_stale}) == "cold"
    assert _state({KEEP_WARM_LABEL: codex_hit}) == "warm"
    assert _state({KEEP_WARM_LABEL: codex_stale}) == "cold"
    assert _state({KEEP_WARM_LABEL: codex_miss}) == "cold"
    # Busy wins over the clock / observation: the next turn touches the cache.
    assert _state({KEEP_WARM_LABEL: claude_stale}, busy=True) == "warm"
    assert _state({}, busy=True) is None
    # Archived, no labels, and a label predating the family stamp read None.
    assert _state({KEEP_WARM_LABEL: claude_warm}, archived=True) is None
    assert _state({}) is None
    assert _state({KEEP_WARM_LABEL: _label(s="w", t=1, u=now - 100)}) is None
    assert _state({"omnigent.keep_warm_stats": "{}"}) is None


async def test_warm_state_for_labels_falls_back_to_the_harness_family() -> None:
    """No label or a pre-stamp label reads per the resolved harness family."""
    now = 1_000_000
    # No label: cold when idle, warm while busy, None without a family.
    assert (
        warm_state_for_labels({}, archived=False, busy=False, now=now, family="claude") == "cold"
    )
    assert warm_state_for_labels({}, archived=False, busy=True, now=now, family="claude") == "warm"
    assert warm_state_for_labels({}, archived=False, busy=False, now=now) is None
    # A label predating the family stamp uses the resolved family's rule.
    stale_codex = _label(s="w", t=1, u=now - 1200, o=[now - 2400, 1])
    hit_codex = _label(s="w", t=1, u=now - 1200, o=[now - 1200, 1])
    assert (
        warm_state_for_labels(
            {KEEP_WARM_LABEL: stale_codex}, archived=False, busy=False, now=now, family="codex"
        )
        == "cold"
    )
    assert (
        warm_state_for_labels(
            {KEEP_WARM_LABEL: hit_codex}, archived=False, busy=False, now=now, family="codex"
        )
        == "warm"
    )
    # Archived and mirrored rows read None whatever the family says.
    assert warm_state_for_labels({}, archived=True, busy=False, now=now, family="claude") is None
    assert (
        warm_state_for_labels(
            {_WRAPPER_LABEL_KEY: _MIRRORED_CLAUDE_WRAPPER},
            archived=False,
            busy=False,
            now=now,
            family="claude",
        )
        is None
    )


async def test_keep_warm_family_helpers_read_harness_and_label() -> None:
    """The family helpers map supported harnesses and the label's ``y`` stamp."""
    assert child_keep_warm.keep_warm_family_for_harness("claude-native") == "claude"
    assert child_keep_warm.keep_warm_family_for_harness("claude-sdk") == "claude"
    assert child_keep_warm.keep_warm_family_for_harness("codex") == "codex"
    assert child_keep_warm.keep_warm_family_for_harness("opencode-native") is None
    assert child_keep_warm.keep_warm_family_for_harness(None) is None
    assert (
        child_keep_warm.keep_warm_family_from_labels({KEEP_WARM_LABEL: _label(s="w", y="codex")})
        == "codex"
    )
    assert child_keep_warm.keep_warm_family_from_labels({KEEP_WARM_LABEL: _label(s="w")}) is None
    assert child_keep_warm.keep_warm_family_from_labels({}) is None


async def test_sweeper_stamps_the_family_on_label_writes(harness: _Harness) -> None:
    """A fresh episode and a due ping both persist ``y`` for label readers."""
    main = _main(harness, running_since=harness.now - 50)
    await _tick(harness)
    opened = _read_label(harness, main.id)
    assert opened is not None and opened.y == "claude"

    child = _child(
        harness,
        main.id,
        labels={
            KEEP_WARM_LABEL: _warm_label(
                t=harness.now - 100, u=harness.now - _CLAUDE_INTERVAL_S - 1
            )
        },
        running_since=harness.now - 100,
    )
    assert _read_label(harness, child.id).y is None
    await _tick(harness)
    due = _read_label(harness, child.id)
    assert due is not None and due.y == "claude"
    assert due.a is not None


async def test_keep_warm_status_maps_state_reasons_and_costs() -> None:
    """The status object reads on / paused / stopped / off from the label."""
    now = 1_000_000
    stats = child_keep_warm._WarmStats(
        ep_p=3,
        ep_c=40_000,
        ep_e=True,
        ep_s=now - 3600,
        tot_p=9,
        tot_c=120_000,
        tot_e=True,
        lr_at=now - 50,
        lr_r="hit",
    ).to_label()

    def _status(raw: str | None, *, archived: bool = False) -> dict[str, Any] | None:
        labels = {} if raw is None else {KEEP_WARM_LABEL: raw, KEEP_WARM_STATS_LABEL: stats}
        return keep_warm_status_from_labels(labels, archived=archived, now=now)

    on = _status(_label(s="w", y="claude", t=1, u=now - 100, w=now + 100))
    assert on is not None
    assert on["state"] == "on"
    assert on["stop_reason"] is None
    # Micro-USD converts to float USD; the last return passes through.
    assert on["episode"] == {
        "pings": 3,
        "cost_usd": pytest.approx(0.04),
        "estimated": True,
        "started_at": now - 3600,
    }
    assert on["total"] == {"pings": 9, "cost_usd": pytest.approx(0.12), "estimated": True}
    assert on["last_return"] == {"at": now - 50, "result": "hit"}
    assert on["last_reason"] is None

    paused = _status(_label(s="p", why="miss", y="claude", t=1))
    assert paused is not None
    assert paused["state"] == "paused"
    assert paused["stop_reason"] == "misses"

    stopped = _status(_label(s="c", why="cap", y="claude", t=1))
    assert stopped is not None
    assert stopped["state"] == "stopped"
    assert stopped["stop_reason"] == "cap"

    off = _status(_label(s="c", y="claude", t=1))
    assert off is not None
    assert off["state"] == "off"
    assert off["stop_reason"] is None

    # An unrecognized stored code never errors; it reads a null stop reason.
    unknown = _status(_label(s="c", why="exp", y="claude", t=1))
    assert unknown is not None
    assert unknown["state"] == "stopped"
    assert unknown["stop_reason"] is None

    assert _status(None) is None
    assert _status(_label(s="w", y="claude", t=1), archived=True) is None


async def test_keep_warm_status_reports_a_blocking_gate() -> None:
    """A warm label blocked by a gate reads off / paused with its gate reason."""
    now = 1_000_000

    def _gate(k: str) -> dict[str, Any] | None:
        return keep_warm_status_from_labels(
            {KEEP_WARM_LABEL: _label(s="w", k=k, y="claude", t=1, u=now - 100)},
            archived=False,
            now=now,
        )

    switch = _gate("switch")
    assert switch is not None
    assert switch["state"] == "off"
    assert switch["stop_reason"] == "switch_off"
    assert switch["last_reason"] == "switch"

    runner = _gate("runner")
    assert runner is not None
    assert runner["state"] == "paused"
    assert runner["stop_reason"] == "host"
    assert runner["last_reason"] == "runner"

    host = _gate("host")
    assert host is not None
    assert host["state"] == "paused"
    assert host["stop_reason"] == "host"
    assert host["last_reason"] == "host"

    card = _gate("card")
    assert card is not None
    assert card["state"] == "paused"
    assert card["stop_reason"] == "card"
    assert card["last_reason"] == "card"


async def test_keep_warm_status_keeps_a_receipt_code_on() -> None:
    """A non-gate receipt code leaves a warm episode on with no stop reason."""
    now = 1_000_000
    status = keep_warm_status_from_labels(
        {KEEP_WARM_LABEL: _label(s="w", k="btw_unavailable", y="claude", t=1, u=now - 100)},
        archived=False,
        now=now,
    )
    assert status is not None
    assert status["state"] == "on"
    assert status["stop_reason"] is None
    assert status["last_reason"] == "btw_unavailable"


async def test_keep_warm_status_names_the_failure_cause() -> None:
    """A failures pause carries the last ping's raw skip / fail code."""
    status = keep_warm_status_from_labels(
        {KEEP_WARM_LABEL: _label(s="p", why="fail", k="btw_unavailable", y="claude", t=1, f=3)},
        archived=False,
        now=1_000_000,
    )
    assert status is not None
    assert status["state"] == "paused"
    assert status["stop_reason"] == "failures"
    assert status["last_reason"] == "btw_unavailable"


# ── Label mechanics ──────────────────────────────────────


async def test_start_seeds_tracked_warm_labels_and_shutdown_stops(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start`` seeds the tracked map from stored warm labels, both kinds."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u)},
        running_since=u,
    )
    main = _main(
        harness,
        labels={KEEP_WARM_LABEL: _warm_label(t=u)},
        running_since=u,
        title="seeded main",
    )
    monkeypatch.setattr(harness.sweeper, "_list_ping_candidates", lambda _now: set())

    await harness.sweeper.start(harness.sweeper._app)
    try:
        assert child.id in harness.sweeper._tracked
        assert main.id in harness.sweeper._tracked
    finally:
        await harness.sweeper.shutdown()
    assert harness.sweeper._task is None


async def test_label_round_trips_through_the_store_at_its_largest(
    harness: _Harness,
) -> None:
    """The largest state stays under 256 chars and survives."""
    main = _main(harness)
    state = child_keep_warm._WarmState(
        s="p",
        why="miss",
        t=2**31 - 1,
        c=2**31 - 1,
        u=2**31 - 1,
        p=2**31 - 1,
        a="deadbeef",
        f=99,
        m=2,
        b=[2**31 - 1, 2**31 - 1],
        v=99,
        w=2**31 - 1,
        o=[2**31 - 1, 1],
        r=True,
        e=True,
        k="x" * 24,
    )
    value = state.to_label()
    assert len(value) <= 256

    harness.store.set_labels(main.id, {KEEP_WARM_LABEL: value})

    stored = harness.store.get_conversation(main.id)
    assert stored is not None
    assert stored.labels[KEEP_WARM_LABEL] == value
    assert child_keep_warm._WarmState.parse(stored.labels[KEEP_WARM_LABEL]) == state


async def test_stats_label_round_trips(harness: _Harness) -> None:
    """The stats label round-trips and tolerates garbage."""
    stats = child_keep_warm._WarmStats(
        ep_p=3,
        ep_c=45000,
        ep_e=True,
        ep_s=100,
        tot_p=9,
        tot_c=80000,
        tot_e=True,
        lr_at=200,
        lr_r="miss",
    )
    assert child_keep_warm._WarmStats.parse(stats.to_label()) == stats
    assert child_keep_warm._WarmStats.parse(None) == child_keep_warm._WarmStats()
    assert child_keep_warm._WarmStats.parse("garbage") == child_keep_warm._WarmStats()
    assert child_keep_warm._WarmStats.parse(json.dumps({"lr": [100, "bogus"]})) == (
        child_keep_warm._WarmStats(lr_at=100)
    )


async def test_parse_rejects_malformed_labels() -> None:
    """Garbage or partial labels parse to ``None`` instead of raising."""
    assert child_keep_warm._WarmState.parse(None) is None
    assert child_keep_warm._WarmState.parse("") is None
    assert child_keep_warm._WarmState.parse("not json") is None
    assert child_keep_warm._WarmState.parse("[1,2]") is None
    assert child_keep_warm._WarmState.parse(json.dumps({"s": "x"})) is None
    state = child_keep_warm._WarmState.parse(json.dumps({"s": "w", "t": "nope", "b": "x"}))
    assert state is not None and state.t is None and state.b is None
    odd = child_keep_warm._WarmState.parse(json.dumps({"s": "c", "why": "zzz"}))
    assert odd is not None and odd.why is None
    # A pending attempt from the old quiet-turn format has no id: inert.
    legacy = child_keep_warm._WarmState.parse(json.dumps({"s": "w", "p": 100, "q": 1}))
    assert legacy is not None and legacy.a is None


async def test_legacy_why_codes_round_trip() -> None:
    """SCC19's ``mom`` / ``pol`` codes are never written but still parse."""
    for why in ("mom", "pol"):
        state = child_keep_warm._WarmState(s="c", why=why, t=1, c=1, u=1, w=1)
        assert child_keep_warm._WarmState.parse(state.to_label()) == state


# ── Host-offline archive pass ────────────────────────────


class _HostStore:
    """Host store fake with scripted rows."""

    def __init__(self, hosts: dict[str, Host]) -> None:
        self._hosts = hosts

    def get_host(self, host_id: str) -> Host | None:
        return self._hosts.get(host_id)


def _wire_host(harness: _Harness, host_id: str, *, status: str, updated_at: int) -> Host:
    """Attach a scripted host row to the sweeper's app state."""
    host = Host(
        host_id=host_id,
        name="keep-warm-test-host",
        user_id=RESERVED_USER_LOCAL,
        status=status,
        created_at=updated_at,
        updated_at=updated_at,
    )
    harness.sweeper._app.state.host_store = _HostStore({host_id: host})
    return host


async def _tick_archive_pass(harness: _Harness) -> None:
    """Drive enough ticks to run the host-offline archive pass exactly once."""
    for _ in range(child_keep_warm._ARCHIVE_PASS_TICK_INTERVAL):
        await _tick(harness)


def _archived(harness: _Harness, session_id: str) -> bool:
    conv = harness.store.get_conversation(session_id)
    assert conv is not None
    return bool(conv.archived)


async def test_host_offline_past_threshold_archives_the_child(harness: _Harness) -> None:
    """A child whose host is offline past the 4 h default is archived, stamped."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is True
    conv = harness.store.get_conversation(child.id)
    assert conv is not None
    assert conv.labels[ARCHIVE_REASON_LABEL] == "host_offline"
    assert conv.labels[ARCHIVED_BY_LABEL] == "keep_warm"
    assert (child.id, None) in harness.published
    # The mother is a top-level row: never touched by the pass.
    assert _archived(harness, parent.id) is False


async def test_host_offline_archive_stamps_provenance_before_the_archive_flag(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The labels are already stored when the archive flag is committed."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))
    original = harness.store.update_conversation
    observed: list[dict[str, str]] = []

    def _spy(session_id: str, **kwargs: Any) -> Conversation | None:
        if kwargs.get("archived") is True:
            conv = harness.store.get_conversation(session_id)
            assert conv is not None
            observed.append(dict(conv.labels))
        return original(session_id, **kwargs)

    monkeypatch.setattr(harness.store, "update_conversation", _spy)

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is True
    assert len(observed) == 1
    assert observed[0][ARCHIVE_REASON_LABEL] == "host_offline"
    assert observed[0][ARCHIVED_BY_LABEL] == "keep_warm"


async def test_host_offline_archive_clears_provenance_when_the_update_returns_none(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused archive update leaves the live row with neither label."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))
    original = harness.store.update_conversation

    def _refuse(session_id: str, **kwargs: Any) -> Conversation | None:
        if kwargs.get("archived") is True:
            return None
        return original(session_id, **kwargs)

    monkeypatch.setattr(harness.store, "update_conversation", _refuse)

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is False
    conv = harness.store.get_conversation(child.id)
    assert conv is not None
    assert ARCHIVE_REASON_LABEL not in conv.labels
    assert ARCHIVED_BY_LABEL not in conv.labels


async def test_host_offline_under_threshold_leaves_the_child(harness: _Harness) -> None:
    """One minute short of the 4 h default, nothing is archived."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 - 60))

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is False


async def test_host_offline_archive_never_touches_top_level_sessions(
    harness: _Harness,
) -> None:
    """The pass scans ``kind="sub_agent"`` only, even for a host-bound main."""
    main = harness.store.create_conversation(
        agent_id=_CLAUDE_AGENT,
        host_id=_HOST_ID,
        workspace="/tmp/kw-main",
    )
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))

    await _tick_archive_pass(harness)

    assert _archived(harness, main.id) is False


async def test_host_offline_archive_setting_zero_disables(harness: _Harness) -> None:
    """``hostOfflineArchiveSeconds: 0`` turns the pass off for that owner."""
    harness.prefs.keep_warm = {
        "agents": {_CLAUDE_AGENT: {"main": True, "child": True}},
        "hostOfflineArchiveSeconds": 0,
    }
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is False


async def test_host_offline_archive_has_no_recency_window(harness: _Harness) -> None:
    """A child untouched for days is still archived — the pass has no updated_after."""
    from sqlalchemy import update as sql_update
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlConversation

    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))
    # Backdate the row three days, well outside the ping candidate window.
    with Session(harness.store._engine) as session:
        session.execute(
            sql_update(SqlConversation)
            .where(SqlConversation.id == child.id)
            .values(updated_at=harness.now - 3 * 86400)
        )
        session.commit()

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is True


async def test_unarchive_exemption_skips_the_same_offline_spell(harness: _Harness) -> None:
    """A row pinned to the host's current last-seen stamp is not re-archived."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    host = _wire_host(
        harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60)
    )
    harness.store.set_labels(child.id, {ARCHIVE_EXEMPT_SINCE_LABEL: str(host.updated_at)})

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is False


async def test_unarchive_exemption_expires_when_the_host_last_seen_moves(
    harness: _Harness,
) -> None:
    """A stamp from a PREVIOUS offline spell no longer exempts the row."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    host = _wire_host(
        harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60)
    )
    # The exemption names an older last-seen stamp: the host reconnected (and
    # died again) since the user unarchived, so the pass applies once more.
    harness.store.set_labels(child.id, {ARCHIVE_EXEMPT_SINCE_LABEL: str(host.updated_at - 7200)})

    await _tick_archive_pass(harness)

    assert _archived(harness, child.id) is True


async def test_host_offline_archive_caps_a_pass_at_fifty(harness: _Harness) -> None:
    """A dead host's fleet archives in bounded batches, not one burst."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    children = [
        _child(harness, parent.id, live_status=None, title=f"researcher:task{i}")
        for i in range(55)
    ]
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))

    await _tick_archive_pass(harness)

    archived = [child.id for child in children if _archived(harness, child.id)]
    assert len(archived) == child_keep_warm._ARCHIVE_PASS_MAX_ARCHIVES


async def test_host_offline_archive_runs_only_every_tenth_tick(harness: _Harness) -> None:
    """The pass cadence: ticks before the interval boundary archive nothing."""
    parent = _parent(harness, host_id=_HOST_ID, workspace="/tmp/kw-archive", live_status="idle")
    child = _child(harness, parent.id, live_status="idle")
    _wire_host(harness, _HOST_ID, status="offline", updated_at=harness.now - (4 * 3600 + 60))

    for _ in range(child_keep_warm._ARCHIVE_PASS_TICK_INTERVAL - 1):
        await _tick(harness)
    assert _archived(harness, child.id) is False

    await _tick(harness)
    assert _archived(harness, child.id) is True
