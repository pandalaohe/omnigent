"""Tests for the sound-alert ringer registry (``omnigent/server/sound_alerts.py``).

The registry chooses which one of a user's open session-updates
connections plays a claimed alert: the device the user most recently
used, else the account's primary device, else any recently connected
one. Each test drives the real registry with recording send coroutines
so the delivered frame and the chosen connection are both observable.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server import sound_alerts

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    """Isolate the module-global registry per test."""
    sound_alerts.reset_for_tests()
    yield
    sound_alerts.reset_for_tests()


class _Recorder:
    """Async send stand-in recording delivered frames, optionally failing."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.fail = False

    async def __call__(self, frame: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.frames.append(frame)


def _register(owner: str, conn_id: str, device_id: str, send: _Recorder, *, can_ring: bool = True):
    sound_alerts.register(
        owner,
        conn_id,
        device_id=device_id,
        device_label=device_id,
        can_ring=can_ring,
        send=send,
    )


def _record(owner: str, conn_id: str) -> Any:
    return sound_alerts._connections[owner][conn_id]


async def test_most_recently_active_connection_wins_over_the_primary_device() -> None:
    """A device used in the last 5 minutes is preferred over the primary."""
    sent_a = _Recorder()
    sent_b = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a)
    _register(ALICE, "c_b", "dev_b", sent_b)
    sound_alerts.touch(ALICE, "c_a")

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:1",
        session_id="conv_a",
        level="done",
        primary_device_id="dev_b",
    )

    assert delivered is True
    # dev_a was just used, so it wins even though dev_b is the primary.
    assert len(sent_a.frames) == 1
    assert sent_b.frames == []


async def test_primary_device_chosen_when_nobody_is_active() -> None:
    """With no activity, the account's primary device rings."""
    sent_a = _Recorder()
    sent_b = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a)
    _register(ALICE, "c_b", "dev_b", sent_b)

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:error:1",
        session_id="conv_a",
        level="error",
        primary_device_id="dev_b",
    )

    assert delivered is True
    assert sent_a.frames == []
    assert len(sent_b.frames) == 1
    assert sent_b.frames[0]["level"] == "error"


async def test_most_recent_connection_chosen_when_primary_is_offline() -> None:
    """An absent primary falls back to the most recently connected device."""
    sent_a = _Recorder()
    sent_b = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a)
    _register(ALICE, "c_b", "dev_b", sent_b)
    _record(ALICE, "c_a").connected_at = 10.0
    _record(ALICE, "c_b").connected_at = 20.0

    chosen = sound_alerts.choose_ringer(ALICE, "dev_missing", now=100.0)

    assert chosen is not None
    assert chosen.conn_id == "c_b"


async def test_ties_break_on_the_greatest_conn_id() -> None:
    """Equally recent connections resolve deterministically."""
    _register(ALICE, "c_a", "dev_a", _Recorder())
    _register(ALICE, "c_b", "dev_b", _Recorder())
    _record(ALICE, "c_a").connected_at = 10.0
    _record(ALICE, "c_b").connected_at = 10.0

    chosen = sound_alerts.choose_ringer(ALICE, None, now=100.0)

    assert chosen is not None
    assert chosen.conn_id == "c_b"


async def test_can_ring_false_is_never_chosen() -> None:
    """A device that cannot play is skipped even when it was used last."""
    sent_a = _Recorder()
    sent_b = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a)
    _register(ALICE, "c_b", "dev_b", sent_b, can_ring=False)
    sound_alerts.touch(ALICE, "c_b")

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:2",
        session_id="conv_a",
        level="done",
        primary_device_id="dev_b",
    )

    assert delivered is True
    assert len(sent_a.frames) == 1
    assert sent_b.frames == []


async def test_no_candidates_leaves_the_alert_claimed_undelivered() -> None:
    """An unclaimed id is recorded; a retry cannot deliver it late."""
    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:3",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )
    assert delivered is False

    sent = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent)
    retried = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:3",
        session_id="conv_a",
        level="done",
        primary_device_id="dev_a",
    )

    assert retried is False
    assert sent.frames == []


async def test_same_alert_id_is_delivered_only_once() -> None:
    """Overlapping detectors claim once; the repeat is dropped."""
    sent = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent)

    first = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:needs_response:1",
        session_id="conv_a",
        level="needs_response",
        primary_device_id="dev_a",
    )
    second = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:needs_response:1",
        session_id="conv_a",
        level="needs_response",
        primary_device_id="dev_a",
    )

    assert first is True
    assert second is False
    assert len(sent.frames) == 1
    assert sent.frames[0] == {
        "type": "sound_alert",
        "alert_id": "conv_a:needs_response:1",
        "session_id": "conv_a",
        "level": "needs_response",
    }


async def test_owners_are_isolated() -> None:
    """One user's claim never reaches another user's connection."""
    sent_alice = _Recorder()
    sent_bob = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_alice)
    _register(BOB, "c_b", "dev_b", sent_bob)

    await sound_alerts.claim(
        ALICE,
        alert_id="shared:done:1",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )
    assert len(sent_alice.frames) == 1
    assert sent_bob.frames == []

    # The same alert id is still claimable by bob's own owner scope.
    other = await sound_alerts.claim(
        BOB,
        alert_id="shared:done:1",
        session_id="conv_b",
        level="done",
        primary_device_id=None,
    )
    assert other is True
    assert len(sent_bob.frames) == 1


async def test_repeated_hello_keeps_connection_activity_history() -> None:
    """A re-announced connection keeps its recency and stays the active ringer."""
    sent_a = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a, can_ring=False)
    connected_at = _record(ALICE, "c_a").connected_at
    sound_alerts.touch(ALICE, "c_a")
    activity_at = _record(ALICE, "c_a").last_activity

    # The same connection toggles its ring switch on in a second hello.
    _register(ALICE, "c_a", "dev_a", sent_a, can_ring=True)
    _register(ALICE, "c_b", "dev_b", _Recorder())
    # c_b connected later; only c_a's retained activity should make it win.
    _record(ALICE, "c_b").connected_at = connected_at + 100.0

    record = _record(ALICE, "c_a")
    assert record.connected_at == connected_at
    assert record.last_activity == activity_at
    assert record.can_ring is True

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:1",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )

    assert delivered is True
    assert len(sent_a.frames) == 1


async def test_two_connections_of_one_device_ring_once() -> None:
    """Two tabs of the same device never both play one alert."""
    sent_a = _Recorder()
    sent_b = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent_a)
    _register(ALICE, "c_b", "dev_a", sent_b)
    sound_alerts.touch(ALICE, "c_a")

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:4",
        session_id="conv_a",
        level="done",
        primary_device_id="dev_a",
    )

    assert delivered is True
    assert len(sent_a.frames) + len(sent_b.frames) == 1


async def test_failed_send_unregisters_and_tries_the_next_connection() -> None:
    """A dead connection is skipped so the alert still rings elsewhere."""
    sent_good = _Recorder()
    sent_bad = _Recorder()
    sent_bad.fail = True
    _register(ALICE, "c_good", "dev_good", sent_good)
    _register(ALICE, "c_bad", "dev_bad", sent_bad)
    # The broken connection is the most recently connected, so it is chosen
    # first and its failure must roll over to the healthy one.
    _record(ALICE, "c_good").connected_at = 10.0
    _record(ALICE, "c_bad").connected_at = 20.0

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:error:2",
        session_id="conv_a",
        level="error",
        primary_device_id=None,
    )

    assert delivered is True
    assert len(sent_good.frames) == 1
    assert "c_bad" not in sound_alerts._connections.get(ALICE, {})


async def test_recent_user_stop_suppresses_done_and_error_only_for_its_session() -> None:
    """A stop silences terminal cues, while a pending prompt still rings."""
    sent_alice = _Recorder()
    sent_bob = _Recorder()
    _register(ALICE, "c_alice", "dev_alice", sent_alice)
    _register(BOB, "c_bob", "dev_bob", sent_bob)
    sound_alerts.note_user_stop(ALICE, "conv_a")

    for level in ("done", "error"):
        delivered = await sound_alerts.claim(
            ALICE,
            alert_id=f"conv_a:{level}:stopped",
            session_id="conv_a",
            level=level,
            primary_device_id=None,
        )
        assert delivered is False
    assert sent_alice.frames == []

    needs_response = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:needs_response:stopped",
        session_id="conv_a",
        level="needs_response",
        primary_device_id=None,
    )
    another_session = await sound_alerts.claim(
        ALICE,
        alert_id="conv_b:done:stopped",
        session_id="conv_b",
        level="done",
        primary_device_id=None,
    )
    another_owner = await sound_alerts.claim(
        BOB,
        alert_id="conv_a:done:stopped",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )

    assert needs_response is True
    assert another_session is True
    assert another_owner is True
    assert [frame["level"] for frame in sent_alice.frames] == ["needs_response", "done"]
    assert [frame["level"] for frame in sent_bob.frames] == ["done"]


async def test_forgetting_user_stop_allows_a_new_done_alert() -> None:
    """A failed stop request can restore completion alerts."""
    sent = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent)
    sound_alerts.note_user_stop(ALICE, "conv_a")

    before_forget = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:before_forget",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )
    sound_alerts.forget_user_stop(ALICE, "conv_a")
    repeated = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:before_forget",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )
    after_forget = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:after_forget",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )

    assert before_forget is False
    assert repeated is False
    assert after_forget is True
    assert [frame["alert_id"] for frame in sent.frames] == ["conv_a:done:after_forget"]


async def test_old_user_stop_note_no_longer_suppresses_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note older than one minute expires when claims touch owner state."""
    sent = _Recorder()
    _register(ALICE, "c_a", "dev_a", sent)
    now = 100.0
    monkeypatch.setattr(sound_alerts.time, "monotonic", lambda: now)
    sound_alerts.note_user_stop(ALICE, "conv_a")
    now = 160.001

    delivered = await sound_alerts.claim(
        ALICE,
        alert_id="conv_a:done:expired",
        session_id="conv_a",
        level="done",
        primary_device_id=None,
    )

    assert delivered is True
    assert len(sent.frames) == 1
