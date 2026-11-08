"""End-to-end tests for the Phase 4 named-sub-agent pipeline.

Coverage of:

* ``test_spawn_named_sub_agent_e2e`` — the LLM picks up the
  ``sys_session_send(agent, title, args)`` signature and a child
  conversation persists with title="<agent>:<title>".
* ``test_send_to_named_sub_agent_continuation_e2e`` — turn 2
  uses ``sys_session_send`` to continue the existing child;
  the child's history accumulates across turns.
* ``test_ambient_hint_steers_followup_to_send_e2e`` — turn 2's
  user prompt is neutral; the LLM uses the ambient hint
  ("Open sub-agents:") to choose ``sys_session_send`` over a
  duplicate spawn (the critical D6 test — if it fails, named
  persistence is useless because the LLM forgets across turns).
* ``test_parallel_named_sub_agents_e2e`` — both researcher
  ("first") and summarizer ("second") in one turn; both
  markers reach the final reply.
* ``test_cross_parent_named_isolation_e2e`` — same name in two
  separate top-level conversations doesn't leak.

Always uses mock-LLM mode. The fixture agent bundle is uploaded
(it declares the sub-agent specs the runner needs) and responses
come from the mock LLM server's keyed queues.

Each turn is driven through a runner-bound session: the agent
bundle is registered (``upload_agent`` rewrites the native
``gpt-5.4`` model to a Databricks-served name and stamps the
``--profile`` onto the executor blocks), a session is created and
bound to the live runner, and the user message is posted to
``POST /v1/sessions/{id}/events``. The terminal turn is read from
the session snapshot — the legacy ``POST /v1/responses`` route was
removed. Multi-turn tests reuse the same session id, so continuation
is implicit (no ``previous_response_id``).

``sys_session_send`` is async: the sub-agent runs AFTER the parent's
dispatch turn ends, then auto-wakes the parent in a continuation turn
that reads the inbox and surfaces the result. So a sub-agent marker
lands in the session only after the first idle — tests
:func:`_wait_for_markers` (poll the snapshot) rather than read the
dispatch turn's terminal reply, mirroring ``test_subagent_autowake_e2e``.

Excluded from default ``pytest`` runs via
``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_named_sub_agent_persistence.py \\
        --profile oss -v
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Each test is 3+ serial gateway turns (dispatch + sub-agent + auto-wake
# continuation), so FMAPI 429 backoff stacks multiplicatively and the
# suite-wide 180s cap hard-killed the xdist worker (thread method calls
# os._exit, wedging the shard). 600s absorbs the backoff; the signal
# method is safe here, unlike the pexpect/pty tests the workflow's
# thread default protects, because these tests block only in
# main-thread httpx polls, so the worker survives and fixtures tear
# down on a genuine timeout.
# These sub-agent tests drive each child agent through per-sub-agent mock-LLM
# routing — every child runs on its own mock model and auth.base_url. A server
# < 0.3.0 does not propagate the sub-agent executor's mock base_url (fixed in
# #779, which added auth to the inner ExecutorSpec; it landed ~2h after v0.2.0
# was tagged, so v0.2.0 just missed it), so the child harness reaches the real
# gateway and its mock-only model name is rejected (HTTP 400); the child returns
# nothing, so the parent's auto-wake has nothing to surface. Verified against a
# v0.2.0 server: the child 400s on the real gateway while auto-wake itself works
# (waiting downgraded, no 500). This is a mock-LLM test-infrastructure gap, not
# a product regression. The backwards-compat matrix skips these against servers
# < 0.3.0 via min_server_version; they run unchanged on main and in the gate.
pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "_fixtures" / "agents"
_NAMED_FIXTURE = _FIXTURES_DIR / "named-sub-agent-test"

# Base model names match named-sub-agent-test.yaml. Mock parent models get a
# per-test suffix so late auto-wakes cannot consume the next test's queue.
_PARENT_MODEL = "gpt-5.4"
_RESEARCHER_MODEL = "gpt-5.4-named-researcher"
_SUMMARIZER_MODEL = "gpt-5.4-named-summarizer"


# ─── Mock-LLM helpers ──────────────────────────────────────


def _wait_for_autowake_settled(
    http_client: httpx.Client,
    session_id: str,
    timeout_s: float = 60.0,
    *,
    expected_children: int = 1,
) -> None:
    """Wait for all child sessions and parent auto-wake continuations to settle."""
    deadline = time.monotonic() + timeout_s
    stable_polls = 0
    while time.monotonic() < deadline:
        children_resp = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
        children_resp.raise_for_status()
        children = children_resp.json().get("data", [])
        children_settled = len(children) >= expected_children
        if children_settled:
            for child in children:
                if child.get("busy") or child.get("current_task_status") not in (
                    "completed",
                    "failed",
                    "cancelled",
                    "stopped",
                    "killed",
                ):
                    children_settled = False
                    break
                child_resp = http_client.get(f"/v1/sessions/{child['id']}")
                child_resp.raise_for_status()
                child_body = child_resp.json()
                if child_body.get("status") != "idle" or "PHASE4_OK" not in json.dumps(
                    child_body.get("items", [])
                ):
                    children_settled = False
                    break
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        parent_body = resp.json()
        items = parent_body.get("items", [])
        unanswered_wake = bool(items) and "[System: sub-agent " in json.dumps(items[-1])
        if (
            children_settled
            and parent_body.get("status") == "idle"
            and not parent_body.get("busy")
            and not unanswered_wake
        ):
            stable_polls += 1
            if stable_polls >= 2:
                return
        else:
            stable_polls = 0
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id} and its children did not settle within {timeout_s:.0f}s"
    )


def _sys_session_send_tool_call(
    agent: str,
    title: str,
    args: str,
    *,
    call_id: str = "call_1",
) -> dict:
    """Build a tool_calls response entry for ``sys_session_send``."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": args}),
    }


def _configure_spawn_flow(
    mock_url: str | None,
    *,
    agent: str = "researcher",
    parent_model: str,
    child_model: str = _RESEARCHER_MODEL,
    title: str = "auth",
    child_args: str = "What are common auth patterns?",
    child_marker: str = "RESEARCHER_PHASE4_OK",
    parent_reply: str | None = None,
) -> None:
    """Configure mock queues for a single spawn-and-autowake flow.

    Queues responses on per-model keys so the parent and child never
    race for the same queue slot:

    Parent key (``parent_model``):
      1. Dispatch: ``sys_session_send`` tool call.
      2. After tool result: text acknowledging dispatch.
      3. Auto-wake continuation: text quoting the marker.

    Child key (*child_model*):
      1. Child turn: text containing *child_marker*.

    The parent's harness makes TWO LLM calls per tool dispatch: one that
    returns the tool_call, then another after the runner supplies the
    tool result.
    """
    if parent_reply is None:
        parent_reply = f"The sub-agent returned: {child_marker}"
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    _sys_session_send_tool_call(agent, title, child_args),
                ],
            },
            {"text": f"Dispatched {agent}, waiting for result."},
            {"text": parent_reply},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_url,
        [{"text": f"Research complete. {child_marker}"}],
        key=child_model,
    )


@pytest.fixture
def parent_model(mock_llm_server_url: str | None) -> str:
    """Late auto-wakes must never consume another test's parent responses."""
    return f"{_PARENT_MODEL}-{uuid.uuid4().hex[:12]}" if mock_llm_server_url else _PARENT_MODEL


@pytest.fixture
def named_sub_agent_test_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
    parent_model: str,
    tmp_path: Path,
) -> str:
    """
    Upload the named-sub-agent-test fixture (parent + 2 sub-agents).

    Rewrites the parent's and nested sub-agents' ``executor.model``
    values and stamps the active profile onto their (harness-less,
    native) executor blocks when ``--profile`` is set — otherwise the
    native executors reach the gateway with no profile and 401.

    :param http_client: HTTP client pointed at the live server.
    :param databricks_workspace_host: Workspace host URL when
        ``--profile`` is set, else ``None``.
    :param databricks_profile_or_none: Active ``--profile`` value,
        stamped onto the native executors so they authenticate.
    :returns: The registered fixture's unique agent name.
    """
    config = yaml.safe_load((_NAMED_FIXTURE / "named-sub-agent-test.yaml").read_text())
    config["executor"]["model"] = parent_model
    config["name"] = f"named-sub-agent-{uuid.uuid4().hex[:12]}"
    agent_dir = tmp_path / config["name"]
    agent_dir.mkdir()
    (agent_dir / "omnigent.yaml").write_text(yaml.safe_dump(config))
    return upload_agent(
        http_client,
        agent_dir,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
        databricks_profile=databricks_profile_or_none,
    )


def _run_turn(
    http_client: httpx.Client,
    *,
    runner_id: str,
    agent_name: str,
    user_text: str,
    session_id: str | None = None,
    timeout_s: float = 240.0,
) -> tuple[dict, str]:
    """
    Drive one turn through a runner-bound session; return ``(body, session_id)``.

    When *session_id* is ``None`` a fresh runner-bound session is
    created; otherwise the existing session is reused, so a follow-up
    turn continues the same conversation implicitly (no
    ``previous_response_id`` — same-session continuation is how the
    session-events API threads turns). The legacy ``POST /v1/responses``
    route was removed; the dispatch turn's terminal state is read from
    the session snapshot via :func:`poll_session_until_terminal`. Note
    a sub-agent's RESULT is not in this body — see :func:`_wait_for_markers`.

    :param http_client: HTTP client.
    :param runner_id: Live runner id to bind a new session to.
    :param agent_name: Agent name to invoke.
    :param user_text: Plain-text input message for the agent.
    :param session_id: Existing session to continue, or ``None`` to
        create a fresh one.
    :param timeout_s: Max seconds to wait for the dispatch turn to
        go terminal.
    :returns: ``(terminal_body, session_id)``.
    """
    if session_id is None:
        session_id = create_runner_bound_session(
            http_client, agent_name=agent_name, runner_id=runner_id
        )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content=user_text
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=timeout_s
    )
    return body, session_id


def _wait_for_markers(
    http_client: httpx.Client,
    session_id: str,
    *markers: str,
    timeout_s: float = 240.0,
) -> str:
    """
    Poll the session snapshot until every *marker* substring appears.

    ``sys_session_send`` is async: the sub-agent runs after the parent's
    dispatch turn ends, then auto-wakes the parent in a continuation turn.
    The marker therefore lands in the session AFTER the dispatch turn goes
    idle (in the auto-delivered ``[System: task ...]`` message, a tool
    output, or the parent's continuation reply). Serializing the whole
    items list and substring-searching avoids coupling to item shapes —
    mirrors :mod:`test_subagent_autowake_e2e`.

    :param http_client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id to poll.
    :param markers: Substrings that must all appear in the session.
    :param timeout_s: Max seconds to wait for the sub-agent result(s)
        to surface via auto-wake.
    :returns: The final serialized items blob (for any further checks).
    :raises AssertionError: When a marker never surfaces in time.
    """
    deadline = time.monotonic() + timeout_s
    blob = ""
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        blob = json.dumps(resp.json().get("items", []))
        if all(m in blob for m in markers):
            return blob
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"markers {markers!r} did not all surface in session {session_id} "
        f"within {timeout_s:.0f}s (sub-agent result never reached the parent "
        f"via auto-wake). Last items blob: {blob[:600]!r}"
    )


def _conversation_items(http_client: httpx.Client, conversation_id: str) -> list[dict]:
    """Fetch conversation items in store order."""
    resp = http_client.get(
        f"/v1/sessions/{conversation_id}/items",
        params={"limit": 100},
    )
    resp.raise_for_status()
    data: list[dict] = resp.json()["data"]
    return data


def _function_call_names(items: list[dict]) -> list[str]:
    """Return the names of all function_call items in order."""
    return [item.get("name", "") for item in items if item.get("type") == "function_call"]


# ─── Tests ───────────────────────────────────────────────────


def test_spawn_named_sub_agent_e2e(
    http_client: httpx.Client,
    named_sub_agent_test_agent: str,
    parent_model: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    LLM dispatches ``sys_session_send(agent, title, args)``
    and the child conversation persists with the documented
    title shape ``"<agent>:<title>"``. Without this the
    follow-up ``sys_session_send`` lookup wouldn't find the
    child.
    """
    reset_mock_llm(mock_llm_server_url)
    _configure_spawn_flow(
        mock_llm_server_url,
        parent_model=parent_model,
        agent="researcher",
        title="auth",
        child_args="What are common auth patterns?",
        child_marker="RESEARCHER_PHASE4_OK",
    )

    _body, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "Spawn the researcher sub-agent named 'auth' with "
            "input 'What are common auth patterns?'. Quote the "
            "literal marker the sub-agent returns."
        ),
    )
    # The researcher's marker arrives via auto-wake after the dispatch
    # turn — poll the session until it surfaces.
    _wait_for_markers(http_client, session_id, "RESEARCHER_PHASE4_OK")

    # Verify the child conversation was spawned via sys_session_send.
    items = _conversation_items(http_client, session_id)
    spawn_calls = [
        item
        for item in items
        if item.get("type") == "function_call" and item.get("name") == "sys_session_send"
    ]
    assert len(spawn_calls) >= 1, (
        f"Expected at least 1 sys_session_send call; got {_function_call_names(items)}"
    )
    # The spawn arguments must include title="auth" — proves the LLM
    # picked up the named-sub-agent field from the schema. The tool's
    # field is ``title`` (the child persists as ``"<agent>:<title>"``,
    # i.e. ``"researcher:auth"`` — see tool_dispatch.py).
    first_spawn_args = spawn_calls[0]["arguments"]
    assert '"title"' in first_spawn_args and "auth" in first_spawn_args, (
        f"Spawn call arguments missing title='auth'; got {first_spawn_args!r}"
    )
    _wait_for_autowake_settled(http_client, session_id)


def test_ambient_hint_steers_followup_to_send_e2e(
    http_client: httpx.Client,
    named_sub_agent_test_agent: str,
    parent_model: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The ambient hint must let the LLM REMEMBER previously-spawned
    sub-agents across turns. Turn 1 spawns ``researcher:topic``.
    Turn 2's user prompt is deliberately neutral — it doesn't
    name the sub-agent or use the words "researcher" or
    "topic" — so the only way the LLM can correctly continue
    is by reading the ambient hint and choosing
    ``sys_session_send``.

    If the ambient hint isn't injected, the LLM will either
    re-spawn (create a duplicate) or fail to invoke any
    sub-agent tool. Either failure mode breaks named
    persistence.

    The mock LLM returns the correct ``sys_session_send``
    continuation unconditionally — we exercise the full runner
    pipeline (tool dispatch, child-session reuse, auto-wake)
    but NOT the LLM's ability to read the hint.
    """
    reset_mock_llm(mock_llm_server_url)
    # Parent LLM calls on the parent model key (6 total across 2 turns):
    # turn 1: dispatch, after-tool, auto-wake; turn 2: dispatch, after-tool, auto-wake.
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1: parent dispatches
            {
                "tool_calls": [
                    _sys_session_send_tool_call(
                        "researcher", "topic", "Investigate the foundations"
                    ),
                ],
            },
            # Turn 1: parent after tool result
            {"text": "Dispatched researcher, waiting."},
            # Turn 1: parent auto-wake
            {"text": "Researcher returned: RESEARCHER_PHASE4_OK"},
            # Turn 2: parent continues (mock returns the right tool call)
            {
                "tool_calls": [
                    _sys_session_send_tool_call(
                        "researcher", "topic", "Continue the prior investigation"
                    ),
                ],
            },
            # Turn 2: parent after tool result
            {"text": "Dispatched continuation, waiting."},
            # Turn 2: parent auto-wake
            {"text": "Continuation result: RESEARCHER_PHASE4_OK"},
        ],
        key=parent_model,
    )
    # Researcher child LLM calls on the researcher model key (2 total across 2 turns).
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1: child responds
            {"text": "Foundations investigated. RESEARCHER_PHASE4_OK"},
            # Turn 2: child responds
            {"text": "Continued investigation. RESEARCHER_PHASE4_OK"},
        ],
        key=_RESEARCHER_MODEL,
    )

    # Turn 1: spawn with explicit name + topic. Wait for the result so
    # the auto-wake continuation settles before baselining + turn 2.
    _r1, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "Spawn the researcher sub-agent named 'topic' with "
            "input 'Investigate the foundations'. Quote the "
            "literal marker it returns."
        ),
    )
    _wait_for_markers(http_client, session_id, "RESEARCHER_PHASE4_OK")
    _wait_for_autowake_settled(http_client, session_id)
    fc_after_t1 = _function_call_names(_conversation_items(http_client, session_id))
    assert "sys_session_send" in fc_after_t1, "Turn 1 didn't spawn — test premise broken."

    # Turn 2: NEUTRAL prompt that doesn't say "researcher",
    # "topic", or any other identifier. The LLM has to pick up
    # the existing sub-agent from the ambient hint.
    _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "I want to keep working on what we started. Continue "
            "the prior investigation. Quote whatever marker comes "
            "back."
        ),
        session_id=session_id,
    )

    # The critical assertion — turn 2 used sys_session_send
    # (NOT a fresh spawn). If the ambient hint is broken the
    # LLM has no way to know the existing sub-agent's name.
    fc_names = _function_call_names(_conversation_items(http_client, session_id))
    new_calls = fc_names[len(fc_after_t1) :]
    assert "sys_session_send" in new_calls, (
        f"Turn 2's neutral prompt didn't trigger sys_session_send "
        f"— ambient hint failed to surface the existing sub-agent. "
        f"New calls: {new_calls}. Without the hint working, named "
        f"persistence is useless because the LLM forgets across "
        f"turns."
    )
    _wait_for_markers(http_client, session_id, "Continuation result: RESEARCHER_PHASE4_OK")
    _wait_for_autowake_settled(http_client, session_id)


def test_parallel_named_sub_agents_e2e(
    http_client: httpx.Client,
    named_sub_agent_test_agent: str,
    parent_model: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    LLM dispatches researcher and summarizer in parallel
    with distinct names. Both markers reach the final reply,
    proving each child ran independently.
    """
    reset_mock_llm(mock_llm_server_url)
    # Each agent gets its own model-keyed queue so their LLM calls never
    # race against the parent's auto-wake call on a shared queue.
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Parent dispatches both sub-agents in one response
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", "r1", "topic A", call_id="call_r1"),
                    _sys_session_send_tool_call("summarizer", "s1", "topic B", call_id="call_s1"),
                ],
            },
            # Parent after tool results
            {"text": "Dispatched both, waiting."},
            # Parent auto-wake continuation (reads inbox with both results)
            {
                "text": (
                    "Both sub-agents finished. RESEARCHER_PHASE4_OK and SUMMARIZER_PHASE4_OK"
                ),
            },
            # Second auto-wake fallback when parallel children complete in separate wakes
            {
                "text": (
                    "Both sub-agents finished. RESEARCHER_PHASE4_OK and SUMMARIZER_PHASE4_OK"
                ),
            },
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Topic A research. RESEARCHER_PHASE4_OK"}],
        key=_RESEARCHER_MODEL,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Topic B summary. SUMMARIZER_PHASE4_OK"}],
        key=_SUMMARIZER_MODEL,
    )

    _body, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "Spawn TWO sub-agents in parallel — emit both "
            "sys_session_send tool calls in the same response: "
            "researcher named 'r1' with input 'topic A', and "
            "summarizer named 's1' with input 'topic B'. Once "
            "both finish, quote both literal markers in your "
            "final reply."
        ),
    )
    # Both sub-agent results arrive via auto-wake; poll until both land.
    _wait_for_markers(http_client, session_id, "RESEARCHER_PHASE4_OK", "SUMMARIZER_PHASE4_OK")
    _wait_for_autowake_settled(http_client, session_id, expected_children=2)


def test_cross_parent_named_isolation_e2e(
    http_client: httpx.Client,
    named_sub_agent_test_agent: str,
    parent_model: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Same name (``researcher:auth``) in two independent top-level
    conversations: both spawns succeed, neither sees the other's
    history. The partial unique index is per-parent.
    """
    reset_mock_llm(mock_llm_server_url)
    # Two sequential spawn flows with per-model queues so each agent's
    # LLM calls consume from their own stream.
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Conv A: parent dispatches
            {
                "tool_calls": [
                    _sys_session_send_tool_call(
                        "researcher",
                        "auth",
                        "Authentication strategies for project A",
                    ),
                ],
            },
            # Conv A: parent after tool result
            {"text": "Dispatched researcher for project A."},
            # Conv A: parent auto-wake
            {"text": "Researcher returned: RESEARCHER_PHASE4_OK"},
            # Conv B: parent dispatches
            {
                "tool_calls": [
                    _sys_session_send_tool_call(
                        "researcher",
                        "auth",
                        "Authentication strategies for project B",
                    ),
                ],
            },
            # Conv B: parent after tool result
            {"text": "Dispatched researcher for project B."},
            # Conv B: parent auto-wake
            {"text": "Researcher returned: RESEARCHER_PHASE4_OK"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Conv A: child responds
            {"text": "Project A auth. RESEARCHER_PHASE4_OK"},
            # Conv B: child responds
            {"text": "Project B auth. RESEARCHER_PHASE4_OK"},
        ],
        key=_RESEARCHER_MODEL,
    )

    # Conversation A (fresh session).
    _r_a, conv_a = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "Spawn the researcher sub-agent named 'auth' with "
            "input 'Authentication strategies for project A'. "
            "Quote its marker."
        ),
    )
    _wait_for_markers(http_client, conv_a, "RESEARCHER_PHASE4_OK")
    _wait_for_autowake_settled(http_client, conv_a)

    # Conversation B (a second fresh session — independent parent).
    # Must succeed even though conversation A already has a
    # researcher:auth: the unique index is per-parent.
    _r_b, conv_b = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=named_sub_agent_test_agent,
        user_text=(
            "Spawn the researcher sub-agent named 'auth' with "
            "input 'Authentication strategies for project B'. "
            "Quote its marker."
        ),
    )
    _wait_for_markers(http_client, conv_b, "RESEARCHER_PHASE4_OK")
    _wait_for_autowake_settled(http_client, conv_b)

    # The two parent conversations must be distinct.
    assert conv_a != conv_b, "Conversations A and B should be distinct"
