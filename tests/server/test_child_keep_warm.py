"""Unit tests for :class:`~omnigent.server.child_keep_warm.ChildKeepWarmSweeper`.

Fakes for the post, liveness lookup, notifications, clock and preferences
store; a real SQLAlchemy conversation store (per-test sqlite, from the
``db_uri`` fixture) so labels round-trip and the candidate query runs for
real. Every test drives ``_tick`` directly; label timestamps are seeded
relative to a base wall-clock now and the clock advances only by minutes.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server import child_keep_warm
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.child_keep_warm import (
    KEEP_WARM_LABEL,
    LAST_CACHE_LABEL,
    PING,
    ChildKeepWarmSweeper,
    warm_state_from_label,
)
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

pytestmark = pytest.mark.asyncio

_CLAUDE_INTERVAL_S = 3300
_CODEX_INTERVAL_S = 1500
_MAX_S = 28800
_SLACK_S = 300
_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_MIRRORED_CLAUDE_WRAPPER = "claude-code-native-ui-subagent"
_MIRRORED_CODEX_WRAPPER = "codex-native-ui-subagent"
_HOST_ID = "0123456789abcdef0123456789abcdef"


# ── Fakes ────────────────────────────────────────────────


class _Clock:
    """Injectable epoch-seconds clock."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class _PostScript:
    """Records ping posts; scriptable result, exception, or delay."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = {"queued": True}
        self.raises: BaseException | None = None
        self.delay_s: float = 0.0
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        request: Any,
        session_id: str,
        body: Any,
        *,
        acting_user_id: Any = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"session_id": session_id, "body": body, "acting_user_id": acting_user_id}
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        return self.result


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


class _Notices:
    """Records ``notify_line`` calls."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def __call__(self, parent_id: str, line: str) -> None:
        self.lines.append((parent_id, line))


class _Prefs:
    """Preferences store whose single owner row is scripted."""

    def __init__(self, collab: dict[str, Any] | None = None) -> None:
        self.collab = collab

    def get(self, _owner: str) -> dict[str, Any] | None:
        if self.collab is None:
            return None
        return {"settings": {"session_collab": self.collab}}


@dataclass
class _Harness:
    store: SqlAlchemyConversationStore
    now: int
    clock: _Clock
    post: _PostScript
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


@pytest.fixture
def harness(db_uri: str, published: list[tuple[str, str | None]]) -> _Harness:
    """A sweeper wired to fakes and a real per-test sqlite store."""
    store = SqlAlchemyConversationStore(db_uri)
    now = int(time.time())
    clock = _Clock(now)
    post = _PostScript()
    notices = _Notices()
    prefs = _Prefs({"enabled": True, "childKeepWarmEnabled": True})
    liveness = _Liveness()
    sweeper = ChildKeepWarmSweeper(
        conversation_store=store,
        permission_store=None,
        liveness_lookup=liveness,
        post_event_impl=post,
        notify_line=notices,
        clock=clock,
    )
    sweeper._app = SimpleNamespace(state=SimpleNamespace(user_preferences_store=prefs))
    return _Harness(
        store=store,
        now=now,
        clock=clock,
        post=post,
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
    q: bool = False,
    f: int = 0,
    m: int = 0,
    b: int | list[int] | None = None,
    v: int | None = None,
) -> str:
    """A state = warm label with sensible defaults."""
    touch = t if u is None else u
    return _label(
        s="w",
        t=t,
        c=t if c is None else c,
        u=touch,
        p=p,
        q=q,
        f=f,
        m=m,
        b=b,
        v=v,
        w=touch + _CLAUDE_INTERVAL_S + _SLACK_S if w is None else w,
    )


def _read_label(harness: _Harness, child_id: str) -> child_keep_warm._WarmState | None:
    conv = harness.store.get_conversation(child_id)
    assert conv is not None
    return child_keep_warm._WarmState.parse(conv.labels.get(KEEP_WARM_LABEL))


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


def _set_running_since(harness: _Harness, child_id: str, running_since: int) -> None:
    harness.store.set_labels(child_id, {RUNNING_SINCE_LABEL_KEY: str(running_since)})


def _append_assistant(harness: _Harness, child_id: str, text: str) -> None:
    harness.store.append(
        child_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"resp-{time.time_ns()}",
                data=MessageData(
                    role="assistant",
                    agent="claude-native-ui",
                    content=[{"type": "output_text", "text": text}],
                ),
            )
        ],
    )


def _set_last_cache(
    harness: _Harness, child_id: str, *, read: int, creation: int, observed_at: int
) -> None:
    harness.store.set_labels(child_id, {LAST_CACHE_LABEL: f"{read},{creation},{observed_at}"})


async def _tick(harness: _Harness) -> None:
    """Run one tick and let every spawned ping post finish."""
    await harness.sweeper._tick()
    if harness.sweeper._ping_tasks:
        await asyncio.gather(*list(harness.sweeper._ping_tasks))


def _settle_ping(harness: _Harness, child_id: str, state: child_keep_warm._WarmState) -> None:
    """Simulate the ping's own turn starting at its attempt time."""
    assert state.p is not None
    _set_running_since(harness, child_id, state.p)
    harness.store.set_session_live_status(child_id, "idle")


# ── Test scenarios ───────────────────────────────────────


async def test_due_claude_child_is_pinged_with_ping_text_and_owner(harness: _Harness) -> None:
    """Scenario 1: an idle, online Claude child past 55 min gets one ping."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None
    assert state.s == "w" and state.q is True and state.p == harness.now
    assert len(harness.post.calls) == 1
    call = harness.post.calls[0]
    assert call["session_id"] == child.id
    assert call["acting_user_id"] == RESERVED_USER_LOCAL
    content = call["body"].data["content"]
    assert content[0]["text"] == PING


async def test_child_not_yet_due_is_not_pinged(harness: _Harness) -> None:
    """Scenario 2: 30 minutes into the Claude window nothing happens."""
    parent = _parent(harness)
    u = harness.now - 30 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.p is None and state.q is False


async def test_codex_child_uses_the_shorter_interval(harness: _Harness) -> None:
    """Scenario 3: a Codex child is due at 25 minutes, not 55."""
    parent = _parent(harness)
    u = harness.now - 25 * 60
    child = _child(
        harness,
        parent.id,
        harness_override="codex-native",
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=u + _CODEX_INTERVAL_S + _SLACK_S)},
        running_since=u,
    )

    await _tick(harness)

    assert len(harness.post.calls) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.q is True
    assert state.b == [0, 0]


async def test_pending_attempt_is_not_reposted(harness: _Harness) -> None:
    """Scenario 4: a next tick before the turn starts posts nothing new."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    first = _read_label(harness, child.id)
    assert first is not None and first.p is not None

    harness.clock.now = first.p + 60
    await _tick(harness)

    assert len(harness.post.calls) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.q is True and state.p == first.p


async def test_attempt_timeout_counts_a_failure_and_retries_next_tick(
    harness: _Harness,
) -> None:
    """Scenario 5: no turn of ours after 180 s → f+1, retried on a later tick."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    await _tick(harness)
    first = _read_label(harness, child.id)
    assert first is not None and first.p is not None

    harness.clock.now = first.p + 181
    await _tick(harness)

    assert len(harness.post.calls) == 1, "the failure must not re-post in the same tick"
    failed = _read_label(harness, child.id)
    assert failed is not None and failed.f == 1 and failed.q is False

    await _tick(harness)

    assert len(harness.post.calls) == 2
    retried = _read_label(harness, child.id)
    assert retried is not None and retried.q is True and retried.p == harness.clock.now


async def test_window_passed_while_runner_offline_goes_cold_with_notice(
    harness: _Harness,
) -> None:
    """Scenario 6: tracked warm child, runner offline, window passed."""
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

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert len(harness.notices.lines) == 1
    assert harness.notices.lines[0][0] == parent.id
    assert "offline past the keep-warm window" in harness.notices.lines[0][1]
    assert (child.id, None) in harness.published


async def test_unsupported_mirrored_and_never_ran_children_never_get_a_label(
    harness: _Harness,
) -> None:
    """Scenario 7: opencode, both mirrored wrappers, and a never-ran child.

    The switch is on (harness preferences) and the parent is running, and the
    mirrored rows carry a ``running_since``: the wrapper exclusion is the only
    thing that can stop them.
    """
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
    never_ran = _child(harness, parent.id, title="researcher:never-ran")

    await _tick(harness)

    assert harness.post.calls == []
    for child in (opencode, mirrored_claude, mirrored_codex, never_ran):
        assert KEEP_WARM_LABEL not in child.labels
        conv = harness.store.get_conversation(child.id)
        assert conv is not None and KEEP_WARM_LABEL not in conv.labels


async def test_archived_child_is_dropped_from_tracking(
    harness: _Harness,
) -> None:
    """Scenario 8a: an archived child is never pinged and stops being tracked."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.store.update_conversation(child.id, archived=True)
    harness.sweeper._tracked[child.id] = harness.now + 100

    await _tick(harness)

    assert harness.post.calls == []
    assert child.id not in harness.sweeper._tracked
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "w" and state.q is False


async def test_archived_mother_child_goes_cold_silently(harness: _Harness) -> None:
    """Scenario 8b: a mother-archived child settles cold with no notice."""
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

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert harness.notices.lines == []


async def test_closed_child_is_not_pinged(harness: _Harness) -> None:
    """Scenario 8c: a closed child is ineligible for a ping."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={
            KEEP_WARM_LABEL: _warm_label(t=u),
            CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE,
        },
        running_since=u,
    )

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.q is False


async def test_switch_off_goes_cold_at_the_window_without_notice(harness: _Harness) -> None:
    """Scenario 9: master switch or keep-warm switch off → cold, silent."""
    harness.prefs.collab = {"enabled": True, "childKeepWarmEnabled": False}
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=harness.now - 60)},
        running_since=u,
    )

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"
    assert harness.notices.lines == []
    assert harness.published  # the pill still flips warm → cold


async def test_owner_with_no_stored_settings_is_off_with_no_ping_or_label_churn(
    harness: _Harness,
) -> None:
    """Default-off: an owner with no stored row pings nothing.

    The only move the switch-off rules allow is the window passing over a
    tracked warm label — cold ``exp``, no notice; a label still inside its
    window must not be rewritten at all.
    """
    harness.prefs.collab = None
    parent = _parent(harness)
    due_label = _warm_label(t=harness.now - 55 * 60)
    due = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: due_label},
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

    assert harness.post.calls == []
    assert harness.notices.lines == []
    state = _read_label(harness, due.id)
    assert state is not None and state.p is None and state.q is False
    stored = harness.store.get_conversation(due.id)
    assert stored is not None and stored.labels[KEEP_WARM_LABEL] == due_label
    expired_state = _read_label(harness, expired.id)
    assert expired_state is not None and expired_state.s == "c" and expired_state.why == "exp"
    assert harness.published == [(expired.id, None)]


async def test_cap_goes_cold_with_one_notice_naming_agent_host_cwd(
    harness: _Harness,
) -> None:
    """Scenario 10: 8 h after the episode start → cold + one named notice."""
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
    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "cap"
    assert len(harness.notices.lines) == 1
    line = harness.notices.lines[0][1]
    assert f"researcher · {_HOST_ID} · /opt/work/omnigent/fork/wt" in line
    assert "reached the 8 h limit" in line


async def test_new_real_turn_after_cap_starts_a_new_episode(harness: _Harness) -> None:
    """Scenario 11: a changed ``running_since`` re-arms a cold child."""
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


# ── Parent presence (design §2.14) ───────────────────────


async def test_running_parent_keeps_a_due_child_pingable(harness: _Harness) -> None:
    """§2.14: a parent mid-turn is presence even with a stale row clock."""
    parent = _parent(harness, live_status="running")
    harness.clock.now = harness.now + 2 * 3600
    now = harness.clock.now
    u = now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.sweeper._tracked[child.id] = u + _CLAUDE_INTERVAL_S + _SLACK_S

    await _tick(harness)

    assert len(harness.post.calls) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "w" and state.q is True and state.p == now


async def test_idle_parent_past_its_interval_goes_cold_mom_silently(
    harness: _Harness,
) -> None:
    """§2.14 rule 7b: a tracked warm child of an absent parent stops silently."""
    parent = _parent(harness, live_status="idle")
    harness.clock.now = harness.now + 2 * 3600
    now = harness.clock.now
    u = now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.sweeper._tracked[child.id] = u + _CLAUDE_INTERVAL_S + _SLACK_S

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "mom"
    assert harness.notices.lines == []
    assert (child.id, None) in harness.published
    assert child.id not in harness.sweeper._tracked


async def test_cap_with_an_absent_parent_goes_cold_mom_without_notice(
    harness: _Harness,
) -> None:
    """§2.14: rule 7's cap turns silent ``mom`` while the mother is away."""
    parent = _parent(harness, live_status="idle")
    harness.clock.now = harness.now + 2 * 3600
    now = harness.clock.now
    u = now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, c=now - _MAX_S - 1, w=now + 3600)},
        running_since=u,
    )
    harness.sweeper._tracked[child.id] = now + 3600

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "mom"
    assert harness.notices.lines == []
    assert (child.id, None) in harness.published
    assert child.id not in harness.sweeper._tracked


async def test_expiry_with_an_absent_parent_goes_cold_mom_without_notice(
    harness: _Harness,
) -> None:
    """§2.14: rule 7's expiry turns silent ``mom`` while the mother is away."""
    parent = _parent(harness, live_status="idle")
    harness.clock.now = harness.now + 2 * 3600
    now = harness.clock.now
    u = now - 2 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=now - 60)},
        running_since=u,
    )
    harness.sweeper._tracked[child.id] = now - 60

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "mom"
    assert harness.notices.lines == []
    assert (child.id, None) in harness.published


async def test_new_real_turn_after_mom_starts_a_new_episode(harness: _Harness) -> None:
    """§2.14: a ``why = mom`` cold label re-arms at the child's next real turn."""
    parent = _parent(harness, live_status="idle")
    r = harness.now - 2 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _label(s="c", why="mom", t=r, c=r, u=r, w=r + 3600)},
    )
    _set_running_since(harness, child.id, r + 1000)

    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None and state.s == "w" and state.t == r + 1000
    assert child.id in harness.sweeper._tracked


async def test_unknown_parent_status_pings_nothing(harness: _Harness) -> None:
    """§2.14: no cached or stored parent status → absent, fail toward cold."""
    parent = _parent(harness, live_status=None)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "mom"


async def test_parent_running_its_own_keep_warm_ping_is_not_presence(
    harness: _Harness,
) -> None:
    """§2.14: the parent's own ping turn is not presence; its old ``c`` is absent."""
    grandparent = _parent(harness)
    p = harness.now - 300
    parent = _child(
        harness,
        grandparent.id,
        live_status="running",
        running_since=p,
        labels={
            KEEP_WARM_LABEL: _warm_label(
                t=p, u=p, c=harness.now - 3 * 3600, p=p, q=True, w=p + 3600
            )
        },
    )
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "mom"


async def test_three_non_compliant_replies_pause_warming(harness: _Harness) -> None:
    """Scenario 12: each ping whose reply lacks ``[quiet]`` counts a failure."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    for _ in range(3):
        await _tick(harness)
        pinging = _read_label(harness, child.id)
        assert pinging is not None and pinging.q is True
        _settle_ping(harness, child.id, pinging)
        _append_assistant(harness, child.id, "I will not say the line")
        harness.clock.now = pinging.p + 60
        await _tick(harness)
        harness.clock.now = pinging.p + 120
        await _tick(harness)
        settled = _read_label(harness, child.id)
        assert settled is not None
        if settled.s == "w" and settled.u is not None:
            harness.clock.now = settled.u + _CLAUDE_INTERVAL_S

    assert len(harness.post.calls) == 3
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "p" and state.why == "fail"
    assert len(harness.notices.lines) == 1
    assert "3 keep-warm turns failed" in harness.notices.lines[0][1]
    assert (child.id, None) in harness.published


async def test_third_failure_with_an_absent_parent_pauses_without_notice(
    harness: _Harness,
) -> None:
    """§2.14: the failure pause is silent once the mother has gone away."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    for attempt in range(3):
        await _tick(harness)
        pinging = _read_label(harness, child.id)
        assert pinging is not None and pinging.q is True
        _settle_ping(harness, child.id, pinging)
        _append_assistant(harness, child.id, "I will not say the line")
        harness.clock.now = pinging.p + 60
        await _tick(harness)
        if attempt == 2:
            # The parent goes away while the third keep-warm turn is in flight.
            harness.store.set_session_live_status(parent.id, "idle")
        harness.clock.now = pinging.p + 120
        await _tick(harness)
        settled = _read_label(harness, child.id)
        assert settled is not None
        if settled.s == "w" and settled.u is not None:
            harness.clock.now = settled.u + _CLAUDE_INTERVAL_S

    assert len(harness.post.calls) == 3
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "p" and state.why == "fail"
    assert harness.notices.lines == []


async def test_policy_denied_ping_pauses_without_retry(harness: _Harness) -> None:
    """Scenario 13a: ``denied: true`` pauses with one notice and no retry."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)
    harness.post.result = {"queued": False, "denied": True, "reason": "policy"}

    await _tick(harness)
    harness.clock.now += 60
    await _tick(harness)

    assert len(harness.post.calls) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "p" and state.why == "pol"
    assert len(harness.notices.lines) == 1
    assert "policy did not allow" in harness.notices.lines[0][1]

    harness.clock.now += 60
    await _tick(harness)
    assert len(harness.post.calls) == 1


async def test_slow_ping_times_out_as_a_policy_pause(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 13b: a post stalled past 120 s pauses like a denial."""
    monkeypatch.setattr(child_keep_warm, "_PING_TIMEOUT_S", 0.01)
    harness.post.delay_s = 0.05
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    harness.clock.now += 60
    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None and state.s == "p" and state.why == "pol"
    assert len(harness.notices.lines) == 1


async def test_claude_two_cache_misses_pause_and_one_stays_warm(harness: _Harness) -> None:
    """Scenario 14: one miss stays warm; the second pauses at the revision."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    # First ping: a cache miss keeps warming and records m=1.
    await _tick(harness)
    first = _read_label(harness, child.id)
    assert first is not None and first.p is not None
    _settle_ping(harness, child.id, first)
    _append_assistant(harness, child.id, "[quiet]")
    _set_last_cache(harness, child.id, read=10, creation=90, observed_at=first.p + 30)
    harness.clock.now = first.p + 60
    await _tick(harness)
    harness.clock.now = first.p + 120
    await _tick(harness)
    after_first = _read_label(harness, child.id)
    assert after_first is not None
    assert after_first.s == "w" and after_first.m == 1 and after_first.f == 0
    assert harness.notices.lines == []
    assert after_first.u is not None

    # Second ping: another miss reaches the pause threshold.
    harness.clock.now = after_first.u + _CLAUDE_INTERVAL_S
    await _tick(harness)
    second = _read_label(harness, child.id)
    assert second is not None and second.p is not None
    _settle_ping(harness, child.id, second)
    _append_assistant(harness, child.id, "[quiet]")
    _set_last_cache(harness, child.id, read=10, creation=95, observed_at=second.p + 30)
    harness.clock.now = second.p + 60
    await _tick(harness)
    harness.clock.now = second.p + 120
    await _tick(harness)

    paused = _read_label(harness, child.id)
    assert paused is not None
    assert paused.s == "p" and paused.why == "miss" and paused.m == 2
    assert len(harness.notices.lines) == 1
    assert "read 10, written 95 tokens" in harness.notices.lines[0][1]
    assert (child.id, None) in harness.published


async def test_claude_stale_cache_reading_is_unknown(harness: _Harness) -> None:
    """Scenario 15: an ``observed_at`` before the ping turn is not a miss."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(harness, parent.id, labels={KEEP_WARM_LABEL: _warm_label(t=u)}, running_since=u)

    await _tick(harness)
    ping = _read_label(harness, child.id)
    assert ping is not None and ping.p is not None
    _settle_ping(harness, child.id, ping)
    _append_assistant(harness, child.id, "[quiet]")
    # Stale: observed before the ping's running_since.
    _set_last_cache(harness, child.id, read=10, creation=90, observed_at=ping.p - 100)
    harness.clock.now = ping.p + 60
    await _tick(harness)
    harness.clock.now = ping.p + 120
    await _tick(harness)

    state = _read_label(harness, child.id)
    assert state is not None and state.s == "w" and state.m == 0 and state.f == 0
    assert harness.notices.lines == []


async def test_sticky_miss_pause_lifts_on_revision_and_rearms_on_real_turn(
    harness: _Harness,
) -> None:
    """Scenario 16: revision bump lifts the pause; only a later turn re-arms.

    The lift moves the watermark to the child's current ``running_since``,
    so the turn that was already there when the pause lifted is not read as
    new work: re-arming would ping the cold cache the pause was about.
    """
    parent = _parent(harness)
    r = harness.now - 3 * 3600
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _label(s="p", why="miss", t=r, c=r, u=r, w=r + 3600, v=0)},
    )
    _set_running_since(harness, child.id, r + 1000)
    harness.store.update_conversation(child.id, archived=True)
    harness.store.update_conversation(child.id, archived=False)

    await _tick(harness)

    lifted = _read_label(harness, child.id)
    assert lifted is not None and lifted.s == "c" and lifted.why == "rev"
    assert lifted.t == r + 1000
    assert harness.post.calls == []

    # The watermark turn is not new work: the label stays cold.
    await _tick(harness)
    still = _read_label(harness, child.id)
    assert still is not None and still.s == "c" and still.why == "rev"
    assert harness.post.calls == []

    _set_running_since(harness, child.id, r + 2000)
    await _tick(harness)

    warm = _read_label(harness, child.id)
    assert warm is not None and warm.s == "w" and warm.t == r + 2000


async def test_unarchive_does_not_rearm_a_stale_cache_clock(harness: _Harness) -> None:
    """Scenario 17: unarchive keeps ``R``; the passed window stays cold."""
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

    assert harness.post.calls == []
    state = _read_label(harness, child.id)
    assert state is not None and state.s == "c" and state.why == "exp"


async def test_codex_two_cache_misses_pause_warming(harness: _Harness) -> None:
    """Scenario 18: Δcached / (Δcached + Δinput) < 0.5 twice pauses."""
    parent = _parent(harness)
    u = harness.now - 25 * 60
    child = _child(
        harness,
        parent.id,
        harness_override="codex-native",
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=u + _CODEX_INTERVAL_S + _SLACK_S)},
        running_since=u,
    )
    harness.store.set_session_usage(
        child.id, {"input_tokens": 100, "cache_read_input_tokens": 900}
    )

    for expected_m in (1, 2):
        await _tick(harness)
        ping = _read_label(harness, child.id)
        assert ping is not None and ping.p is not None
        if expected_m == 1:
            assert ping.b == [100, 900]
        else:
            assert ping.b == [200, 950]
        _settle_ping(harness, child.id, ping)
        _append_assistant(harness, child.id, "[quiet]")
        base_input = 100 * expected_m
        base_cached = 900 + 50 * (expected_m - 1)
        harness.store.set_session_usage(
            child.id,
            {
                "input_tokens": base_input + 100,
                "cache_read_input_tokens": base_cached + 50,
            },
        )
        harness.clock.now = ping.p + 60
        await _tick(harness)
        harness.clock.now = ping.p + 120
        await _tick(harness)
        settled = _read_label(harness, child.id)
        assert settled is not None
        assert settled.m == expected_m
        if settled.s == "w" and settled.u is not None:
            harness.clock.now = settled.u + _CODEX_INTERVAL_S

    assert len(harness.post.calls) == 2
    paused = _read_label(harness, child.id)
    assert paused is not None and paused.s == "p" and paused.why == "miss"


async def test_settings_interval_is_clamped_to_the_bound(harness: _Harness) -> None:
    """Scenario 24: a stored 99999 s Claude interval clamps to 3540 s."""
    harness.prefs.collab = {
        "enabled": True,
        "childKeepWarmEnabled": True,
        "childKeepWarmClaudeIntervalSeconds": 99999,
    }
    parent = _parent(harness)
    u = harness.now - 3540
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u, w=u + 3600)},
        running_since=u,
    )

    await _tick(harness)

    assert len(harness.post.calls) == 1
    state = _read_label(harness, child.id)
    assert state is not None and state.q is True


async def test_start_seeds_tracked_warm_labels_and_shutdown_stops(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start`` seeds the tracked map from stored warm labels (no candidates)."""
    parent = _parent(harness)
    u = harness.now - 55 * 60
    child = _child(
        harness,
        parent.id,
        labels={KEEP_WARM_LABEL: _warm_label(t=u)},
        running_since=u,
    )
    monkeypatch.setattr(harness.sweeper, "_list_ping_candidates", lambda _now: set())

    await harness.sweeper.start(harness.sweeper._app)
    try:
        assert child.id in harness.sweeper._tracked
    finally:
        await harness.sweeper.shutdown()
    assert harness.sweeper._task is None


async def test_label_round_trips_through_the_store_at_its_largest(
    harness: _Harness,
) -> None:
    """Scenario 20: the largest state stays under 256 chars and survives."""
    parent = _parent(harness)
    child = _child(harness, parent.id)
    state = child_keep_warm._WarmState(
        s="p",
        why="miss",
        t=2**31 - 1,
        c=2**31 - 1,
        u=2**31 - 1,
        p=2**31 - 1,
        q=True,
        f=99,
        m=2,
        b=[2**31 - 1, 2**31 - 1],
        v=99,
        w=2**31 - 1,
    )
    value = state.to_label()
    assert len(value) <= 256

    harness.store.set_labels(child.id, {KEEP_WARM_LABEL: value})

    stored = harness.store.get_conversation(child.id)
    assert stored is not None
    assert stored.labels[KEEP_WARM_LABEL] == value
    assert child_keep_warm._WarmState.parse(stored.labels[KEEP_WARM_LABEL]) == state


async def test_warm_state_from_label_derives_the_pill() -> None:
    """Scenario 19: warm + busy / in-window; paused / past / none; archived."""
    now = 1_000_000
    warm = _label(s="w", t=1, u=now - 100, w=now + 100)
    inside = _label(s="w", t=1, u=now - 100, w=now - 1)
    assert (
        warm_state_from_label(warm, archived=False, harness="claude-native", busy=False, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(inside, archived=False, harness="claude-native", busy=True, now=now)
        == "warm"
    )
    assert (
        warm_state_from_label(inside, archived=False, harness="claude-native", busy=False, now=now)
        == "cold"
    )
    paused = _label(s="p", why="fail", t=1)
    assert (
        warm_state_from_label(paused, archived=False, harness="claude-native", busy=False, now=now)
        == "cold"
    )
    # A busy child is warm whatever the label says or when there is none.
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


async def test_mom_why_code_round_trips() -> None:
    """``why = mom`` is a known code and survives serialization."""
    state = child_keep_warm._WarmState(s="c", why="mom", t=1, c=1, u=1, w=1)
    assert child_keep_warm._WarmState.parse(state.to_label()) == state


async def test_ping_text_is_exact() -> None:
    """The ping text is a contract the runner's quiet rule relies on."""
    assert (
        PING == "[System: keep-warm check from Omnigent. No action is needed; reply with only "
        "this line: [quiet]]"
    )
