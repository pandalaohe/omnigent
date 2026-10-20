"""Tool lifecycle facts through the actual SSE publication entry."""

import pytest

from omnigent.runtime import session_stream, turn_activity


@pytest.fixture(autouse=True)
def clean_activity():
    turn_activity.reset_for_tests()
    yield
    turn_activity.reset_for_tests()


def test_lifecycle_required_and_tool_results_close_calls():
    sid = "tool-activity-test"
    session_stream.publish(sid, {"type": "session.status", "status": "running"})
    assert turn_activity.snapshot_for(sid) is None
    session_stream.publish(sid, {"type": "response.in_progress", "response": {"id": "turn-a"}})
    session_stream.publish(
        sid,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "status": "action_required",
                "call_id": "call-a",
                "name": "shell",
            },
        },
    )
    state = turn_activity.snapshot_for(sid)
    assert state is not None and state.tools["call-a"][0] == "shell"
    assert state.tools["call-a"][1] > 0
    state.tools.clear()
    assert turn_activity.snapshot_for(sid).tools
    session_stream.publish(
        sid,
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call_output", "call_id": "call-a", "output": "done"},
        },
    )
    assert turn_activity.snapshot_for(sid).tools == {}
    # The scaffold emits the completed call after its result.
    session_stream.publish(
        sid,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "status": "completed",
                "call_id": "call-a",
                "name": "shell",
            },
        },
    )
    assert turn_activity.snapshot_for(sid).tools == {}


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "killed"])
def test_async_terminal_status_wins_over_async_flag(status):
    sid = "async-tool-test"
    session_stream.publish(sid, {"type": "response.in_progress", "response": {"id": "turn-a"}})
    for result_status in ("running", status):
        session_stream.publish(
            sid,
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "is_async": True,
                    "tool_status": result_status,
                    "output": "task",
                },
            },
        )
        assert bool(turn_activity.snapshot_for(sid).tools) == (result_status == "running")


def test_only_matching_terminal_releases_turn_and_close_discards():
    sid = "success-test"
    session_stream.publish(sid, {"type": "response.in_progress", "response": {"id": "turn-a"}})
    session_stream.publish(sid, {"type": "session.status", "status": "idle"})
    assert turn_activity.snapshot_for(sid).active is True
    session_stream.publish(sid, {"type": "response.completed", "response": {"id": "old-turn"}})
    assert turn_activity.snapshot_for(sid).active is True
    event = {"type": "response.completed", "response": {"id": "turn-a"}}
    session_stream.publish(sid, event)
    state = turn_activity.snapshot_for(sid)
    assert state.response_id == "turn-a"
    assert state.active is False
    session_stream.publish(sid, event)
    assert turn_activity.snapshot_for(sid).active is False
    session_stream.close(sid)
    assert turn_activity.snapshot_for(sid) is None


def test_provider_native_tool_is_tracked_until_terminal_status():
    sid = "provider-tool-test"
    session_stream.publish(sid, {"type": "response.in_progress", "response": {"id": "turn-a"}})
    for status in ("in_progress", "completed"):
        session_stream.publish(
            sid,
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "native_tool",
                    "item": {"id": "native-call", "type": "web_search_call", "status": status},
                },
            },
        )
        assert bool(turn_activity.snapshot_for(sid).tools) == (status == "in_progress")
