"""Unit test for pin-key stripping in child-session summaries.

``_child_session_summary_from_conversation`` builds a ``ChildSessionSummary``
for a sub-agent row. Every other serialization path collapses per-user pin keys
via ``_labels_for_viewer``; this one must at least strip them so a shared
child's summary can never expose another viewer's ``omnigent.pinned.<user>``
key. Child sessions aren't pinnable, so there's nothing to surface — just the
strip. The same must hold for the server-secret artifact-link key.
"""

from __future__ import annotations

import json
import time

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.common import (
    _SUBAGENT_TERMINAL_STATUS_LABEL_KEY,
    _session_status_cache,
)
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
)
from omnigent.server.schemas import ChildSessionSummary
from omnigent.stores.conversation_store import ARTIFACT_LINK_KEY_LABEL, pinned_label_key


def _child(labels: dict[str, str], *, live_status: str | None = None) -> Conversation:
    """A minimal sub-agent conversation carrying the given labels."""
    return Conversation(
        id="conv_child",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_parent",
        title="tool:child-task",
        agent_id="ag_test",
        labels=labels,
        live_status=live_status,
    )


def test_child_summary_strips_per_user_pin_keys_and_artifact_secret() -> None:
    """A per-user pin key or the artifact-link secret on a child row must not
    leak into its summary."""
    conv = _child(
        {
            pinned_label_key("alice@example.com"): "1721760000000",
            pinned_label_key("bob@example.com"): "1721760001000",
            ARTIFACT_LINK_KEY_LABEL: "signing-secret",
            "omni_project": "Moonshot",
        }
    )
    summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    # No pin key of any kind survives — not the canonical one, not a per-user one.
    assert not any(k.startswith("omnigent.pinned") for k in summary.labels)
    assert ARTIFACT_LINK_KEY_LABEL not in summary.labels
    # Unrelated labels are preserved.
    assert summary.labels.get("omni_project") == "Moonshot"


def test_child_summary_cache_miss_uses_durable_running_status() -> None:
    summary = _child_session_summary_from_conversation(
        _child({}, live_status="running"),
        "conv_parent",
        None,
        cached_status=None,
    )

    assert summary.busy is True
    assert summary.current_task_status == "in_progress"


def test_child_summary_explicit_idle_is_not_overridden_by_durable_running() -> None:
    summary = _child_session_summary_from_conversation(
        _child({}, live_status="running"),
        "conv_parent",
        None,
        cached_status="idle",
    )

    assert summary.busy is False
    assert summary.current_task_status == "completed"


def test_child_summary_derives_warm_state_from_the_keep_warm_label() -> None:
    """The rail pill state comes from the label with no settings read."""
    now = int(time.time())
    keep_warm = "omnigent.keep_warm"

    def _summary(
        labels: dict[str, str], *, harness: str | None, live_status: str | None = None
    ) -> ChildSessionSummary:
        return _child_session_summary_from_conversation(
            _child(labels, live_status=live_status), "conv_parent", None, harness=harness
        )

    warm = json.dumps({"s": "w", "t": now - 100, "u": now - 100, "w": now + 3600})
    past = json.dumps({"s": "w", "t": now - 7200, "u": now - 7200, "w": now - 60})
    paused = json.dumps({"s": "p", "why": "fail", "t": now - 7200})

    assert _summary({keep_warm: warm}, harness="claude-native").warm_state == "warm"
    assert _summary({keep_warm: past}, harness="codex-native").warm_state == "cold"
    assert _summary({keep_warm: paused}, harness="claude-native").warm_state == "cold"
    # Busy overrides a passed window: the next turn touches the prompt anyway.
    busy = _child({keep_warm: past}, live_status="running")
    summary = _child_session_summary_from_conversation(
        busy, "conv_parent", None, harness="claude-native"
    )
    assert summary.warm_state == "warm"
    # A running turn reads warm even with no label or a paused one; mirror
    # rows and unsupported harnesses still read None.
    assert _summary({}, harness="claude-native", live_status="running").warm_state == "warm"
    assert (
        _summary({keep_warm: paused}, harness="claude-native", live_status="running").warm_state
        == "warm"
    )
    assert (
        _summary(
            {"omnigent.wrapper": "claude-code-native-ui-subagent"},
            harness="claude-native",
            live_status="running",
        ).warm_state
        is None
    )
    assert _summary({}, harness="opencode-native", live_status="running").warm_state is None
    # No label, unsupported harness, and archived children read None.
    assert _summary({}, harness="claude-native").warm_state is None
    assert _summary({keep_warm: warm}, harness="opencode-native").warm_state is None
    assert _summary({keep_warm: warm}, harness=None).warm_state is None
    archived = Conversation(
        id="conv_child",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_parent",
        title="tool:child-task",
        agent_id="ag_test",
        labels={keep_warm: warm},
        archived=True,
    )
    archived_summary = _child_session_summary_from_conversation(
        archived, "conv_parent", None, harness="claude-native"
    )
    assert archived_summary.warm_state is None


def test_child_summary_durable_terminal_precedes_live_status_on_cache_miss() -> None:
    summary = _child_session_summary_from_conversation(
        _child(
            {_SUBAGENT_TERMINAL_STATUS_LABEL_KEY: "completed"},
            live_status="running",
        ),
        "conv_parent",
        None,
        cached_status=None,
    )

    assert summary.busy is False
    assert summary.current_task_status == "completed"


def test_child_summary_live_run_beats_durable_terminal_for_a_reusable_child() -> None:
    """A reusable child's newer turn outranks its last dispatch's label."""
    summary = _child_session_summary_from_conversation(
        _child({_SUBAGENT_TERMINAL_STATUS_LABEL_KEY: "completed"}),
        "conv_parent",
        None,
        cached_status="running",
    )

    assert summary.busy is True
    assert summary.current_task_status == "in_progress"


def test_child_summary_live_run_beats_durable_terminal_from_the_status_cache() -> None:
    conv = _child({_SUBAGENT_TERMINAL_STATUS_LABEL_KEY: "completed"})
    _session_status_cache[conv.id] = "running"
    try:
        summary = _child_session_summary_from_conversation(conv, "conv_parent", None)
    finally:
        _session_status_cache.pop(conv.id, None)

    assert summary.busy is True
    assert summary.current_task_status == "in_progress"


def test_child_summary_durable_terminal_holds_for_a_harness_subagent_mirror() -> None:
    """A mirror row is one-shot, so its terminal label stays authoritative."""
    summary = _child_session_summary_from_conversation(
        _child(
            {
                "omnigent.wrapper": "claude-code-native-ui-subagent",
                _SUBAGENT_TERMINAL_STATUS_LABEL_KEY: "completed",
            }
        ),
        "conv_parent",
        None,
        cached_status="running",
    )

    assert summary.busy is False
    assert summary.current_task_status == "completed"
