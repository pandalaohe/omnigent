"""Runner-side member snapshot lock for joint-agent dispatches (SCC06 F1a).

A session launched from a saved library joint agent (the ``ca_`` template
label) freezes a member snapshot (``omnigent.member.<role>`` labels) and locks
the member's harness / model / effort: an explicit ``sys_session_send`` value
that differs is rejected, a member the server marked unavailable refuses the
dispatch, and a dispatch naming none of them runs the snapshot's model / effort
instead of parent inheritance. Other sessions keep per-dispatch choice and
parent-model inheritance.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.member_snapshot import (
    LIBRARY_AGENT_TEMPLATE_LABEL_KEY,
    MEMBER_LABEL_VALUE_MAX_CHARS,
    MEMBER_LOCKED_FIELD,
    encode_member_entry,
    launched_from_library_agent,
    member_entries_from_labels,
    member_label_key,
    member_lock_applies,
    unlocked_legacy_member_labels,
)

_MEMBER_MODEL = "databricks-claude-haiku-4-5"
_PARENT_MODEL = "databricks-claude-sonnet-4-6"


def _spec_with_worker(
    harness: str,
    *,
    worker_model: str | None = None,
    worker_effort: str | None = None,
    allowed_harnesses: list[str] | None = None,
) -> SimpleNamespace:
    """
    Build a parent-spec stub declaring one ``worker`` sub-agent.

    :param harness: The sub-agent's declared harness, e.g. ``"claude-sdk"``.
    :param worker_model: Optional ``executor.model`` pin on the worker spec.
    :param worker_effort: Optional ``executor.reasoning_effort`` pin.
    :param allowed_harnesses: Optional ``executor.config.allowed_harnesses``
        allowlist for a per-dispatch harness override.
    :returns: A structural parent-spec stub for ``execute_tool``.
    """
    config: dict[str, object] = {"harness": harness}
    if allowed_harnesses is not None:
        config["allowed_harnesses"] = allowed_harnesses
    executor = SimpleNamespace(type="omnigent", config=config)
    if worker_model is not None:
        executor.model = worker_model
    if worker_effort is not None:
        executor.reasoning_effort = worker_effort
    return SimpleNamespace(sub_agents=[SimpleNamespace(name="worker", executor=executor)])


def _stub_worker_launchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a native-harness dispatch pass preflight in a CLI-less test env."""
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(harness_install, "missing_harness_cli", lambda _harness: None)


def _member_labels(
    role: str = "worker",
    *,
    template_id: str | None = "ca_test_agent",
    **entry: object,
) -> dict[str, str]:
    """One member snapshot label for *role* with sensible overrides.

    The entry defaults to ``locked: True`` (a session launched from a saved
    library agent).

    :param template_id: Value of the library-agent template label; ``None``
        omits it (a session not started from a saved library joint agent).
    """
    payload: dict[str, object] = {
        "host": None,
        "harness": "claude-sdk",
        "model": _MEMBER_MODEL,
        "effort": "high",
        "lead": False,
        MEMBER_LOCKED_FIELD: True,
    }
    payload.update(entry)
    labels = {member_label_key(role): encode_member_entry(payload)}
    if template_id is not None:
        labels[LIBRARY_AGENT_TEMPLATE_LABEL_KEY] = template_id
    return labels


async def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_spec: Any,
    conv_id: str,
    labels: dict[str, str] | None = None,
    parent_snapshot: dict[str, Any] | None = None,
    dispatch_args: dict[str, Any] | None = None,
    resolve_payload: dict[str, Any] | None = None,
    resolve_queries: list[dict[str, str]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Drive one named ``sys_session_send`` and capture the child create bodies.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param agent_spec: The parent spec under test.
    :param conv_id: Unique parent conversation id per test.
    :param labels: Session labels the server reports, or ``None`` for a 404
        (a session with no snapshot at all).
    :param parent_snapshot: JSON the mock server returns for
        ``GET /v1/sessions/{conv_id}``; ``None`` serves a 404.
    :param dispatch_args: Extra ``args``-object fields (model / effort /
        harness) for the dispatch.
    :param resolve_payload: JSON the mock server returns for
        ``GET /v1/calling-defaults/resolve``; ``None`` serves a 404 so the
        dispatch falls back to parent-model inheritance.
    :param resolve_queries: When given, each calling-defaults request's query
        params are appended.
    :returns: ``(tool_output, create_bodies)``.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner import subagent_work
    from omnigent.runner.tool_dispatch import execute_tool

    create_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(subagent_work, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve labels, the parent snapshot, the chain, child lookup, create, events."""
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv_id}/labels":
            if labels is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"labels": labels})
        if request.method == "GET" and request.url.path == "/v1/calling-defaults/resolve":
            if resolve_queries is not None:
                resolve_queries.append(dict(request.url.params))
            if resolve_payload is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=resolve_payload)
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv_id}":
            if parent_snapshot is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=parent_snapshot)
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{conv_id}/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "conv_child_member"})
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_child_member/events"
        ):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    args: dict[str, Any] = {"input": "do the task"}
    args.update(dispatch_args or {})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "title": "task", "args": args}),
                server_client=server_client,
                conversation_id=conv_id,
                agent_spec=agent_spec,
                session_inbox=session_inbox,
            )
        finally:
            subagent_work.unregister_subagent_work("conv_child_member")
            runner_app._session_inboxes_ref.pop(conv_id, None)
    return output, create_bodies


@pytest.mark.asyncio
async def test_explicit_model_mismatch_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch model differing from the snapshot is rejected, nothing created."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_model_lock",
        labels=_member_labels(),
        dispatch_args={"model": _PARENT_MODEL},
    )

    assert output.startswith("Error:")
    assert "'worker' is locked to" in output
    assert _MEMBER_MODEL in output
    assert bodies == []


@pytest.mark.asyncio
async def test_explicit_effort_mismatch_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch effort differing from the snapshot is rejected."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_effort_lock",
        labels=_member_labels(),
        dispatch_args={"reasoning_effort": "low"},
    )

    assert output.startswith("Error:")
    assert "'worker' is locked to 'high'" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_explicit_harness_mismatch_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch harness differing from the snapshot is rejected."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("codex-native"),
        conv_id="conv_member_harness_lock",
        labels=_member_labels(),
        dispatch_args={"harness": "codex-native"},
    )

    assert output.startswith("Error:")
    assert "'worker' is locked to 'claude-sdk'" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_matching_explicit_values_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit values equal to the snapshot pass through to creation."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk", allowed_harnesses=["claude-sdk"]),
        conv_id="conv_member_matching",
        labels=_member_labels(),
        parent_snapshot={
            "id": "conv_member_matching",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
        dispatch_args={
            "model": _MEMBER_MODEL,
            "reasoning_effort": "high",
            "harness": "claude-sdk",
        },
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert len(bodies) == 1
    assert bodies[0]["model_override"] == _MEMBER_MODEL
    assert bodies[0]["reasoning_effort"] == "high"
    # The matching harness is a no-op: the child resolves the snapshot harness
    # from its spec, so no override rides the create body.
    assert "harness_override" not in bodies[0]


@pytest.mark.asyncio
async def test_snapshot_matching_explicit_harness_needs_no_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit harness equal to the snapshot is a no-op, allowlist or not."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_harness_noop",
        labels=_member_labels(),
        dispatch_args={"harness": "claude-sdk"},
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert len(bodies) == 1
    assert "harness_override" not in bodies[0]


@pytest.mark.asyncio
async def test_explicit_harness_still_needs_the_allowlist_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role with no snapshot entry keeps the override allowlist gate."""
    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_harness_no_snapshot",
        labels=None,
        dispatch_args={"harness": "claude-sdk"},
    )

    assert output.startswith("Error:")
    assert "allowed_harnesses" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_snapshot_model_and_effort_apply_instead_of_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit value: the frozen model / effort win over the parent's model."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk", worker_effort="low"),
        conv_id="conv_member_snapshot_wins",
        labels=_member_labels(),
        parent_snapshot={
            "id": "conv_member_snapshot_wins",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _MEMBER_MODEL
    assert bodies[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_null_snapshot_model_skips_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member frozen to no model override never inherits the parent's."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_null_model",
        labels=_member_labels(model=None),
        parent_snapshot={
            "id": "conv_member_null_model",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_null_snapshot_model_rejects_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member frozen to no model refuses an explicit dispatch model."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_null_model_explicit",
        labels=_member_labels(model=None),
        dispatch_args={"model": _PARENT_MODEL},
    )

    assert output.startswith("Error:")
    assert "locked to 'default'" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_unavailable_member_refuses_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable snapshot member returns the reason and creates nothing."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_unavailable",
        labels=_member_labels(unavailable="host_offline"),
    )

    assert output.startswith("Error:")
    assert "worker" in output
    assert "host_offline" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_session_without_member_labels_still_inherits_parent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed chain lookup leaves today's parent-model inheritance in place."""
    _stub_worker_launchable(monkeypatch)
    queries: list[dict[str, str]] = []

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_no_snapshot",
        labels={"unrelated": "1"},
        parent_snapshot={
            "id": "conv_member_no_snapshot",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
        resolve_queries=queries,
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _PARENT_MODEL
    # The chain was consulted and failed (404), so inheritance took over.
    assert queries == [{"agent_id": "worker", "harness": "claude-sdk"}]


@pytest.mark.asyncio
async def test_member_labels_without_template_keep_choice_and_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlocked entry keeps per-dispatch choice and parent inheritance."""
    _stub_worker_launchable(monkeypatch)

    _output, explicit_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_no_template_explicit",
        labels=_member_labels(template_id=None, locked=False),
        dispatch_args={"model": _PARENT_MODEL},
    )
    assert explicit_bodies[0]["model_override"] == _PARENT_MODEL

    _output, inherited_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_no_template_inherit",
        labels=_member_labels(template_id=None, locked=False),
        parent_snapshot={
            "id": "conv_member_no_template_inherit",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
    )
    assert inherited_bodies[0]["model_override"] == _PARENT_MODEL


@pytest.mark.asyncio
async def test_non_library_template_label_does_not_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlocked entry on a non-``ca_`` template keeps per-dispatch choice."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_builtin_template",
        labels=_member_labels(template_id="ag_builtin", locked=False),
        dispatch_args={"model": _PARENT_MODEL},
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _PARENT_MODEL


@pytest.mark.asyncio
async def test_unlocked_entry_ignores_the_ca_template_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``locked: False`` entry stays unlocked even under a ``ca_`` label.

    Save-as-Agent stamps the template label onto a running session after
    create, so the lock follows the entry's create-time provenance, not the
    label.
    """
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_unlocked_ca_template",
        labels=_member_labels(locked=False),
        dispatch_args={"model": _PARENT_MODEL},
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _PARENT_MODEL


def _legacy_member_labels(*, template_id: str | None) -> dict[str, str]:
    """A pre-``locked`` entry: its lock comes from the session's template label."""
    payload: dict[str, object] = {
        "host": None,
        "harness": "claude-sdk",
        "model": _MEMBER_MODEL,
        "effort": "high",
        "lead": False,
    }
    labels = {member_label_key("worker"): encode_member_entry(payload)}
    if template_id is not None:
        labels[LIBRARY_AGENT_TEMPLATE_LABEL_KEY] = template_id
    return labels


@pytest.mark.asyncio
async def test_legacy_entry_without_the_field_follows_the_template_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-``locked`` entry is locked iff its session has a ``ca_`` label."""
    _stub_worker_launchable(monkeypatch)

    locked_output, locked_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_legacy_locked",
        labels=_legacy_member_labels(template_id="ca_legacy_agent"),
        dispatch_args={"model": _PARENT_MODEL},
    )
    assert locked_output.startswith("Error:")
    assert "'worker' is locked to" in locked_output
    assert locked_bodies == []

    _output, unlocked_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_legacy_unlocked",
        labels=_legacy_member_labels(template_id="ag_builtin"),
        dispatch_args={"model": _PARENT_MODEL},
    )
    assert unlocked_bodies[0]["model_override"] == _PARENT_MODEL


@pytest.mark.asyncio
async def test_builtin_member_takes_the_calling_defaults_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 15: the chain's model / effort fill a dispatch without a snapshot."""
    _stub_worker_launchable(monkeypatch)
    queries: list[dict[str, str]] = []

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("codex-native"),
        conv_id="conv_member_chain",
        labels={"unrelated": "1"},
        parent_snapshot={
            "id": "conv_member_chain",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
            "project_id": "proj_polly",
            "host_id": "host_hds",
        },
        resolve_payload={"model": "gpt-6-astra", "effort": "medium"},
        resolve_queries=queries,
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == "gpt-6-astra"
    assert bodies[0]["reasoning_effort"] == "medium"
    assert queries == [
        {
            "project_id": "proj_polly",
            "host_id": "host_hds",
            "agent_id": "worker",
            "harness": "codex-native",
        }
    ]


@pytest.mark.asyncio
async def test_explicit_model_and_pinned_spec_beat_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit dispatch model wins; a spec-pinned value keeps its field."""
    _stub_worker_launchable(monkeypatch)

    _output, explicit_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_chain_explicit",
        labels={"unrelated": "1"},
        parent_snapshot={"id": "conv_member_chain_explicit", "agent_id": "ag_parent"},
        dispatch_args={"model": _MEMBER_MODEL},
        resolve_payload={"model": "chain-model", "effort": "medium"},
    )
    assert explicit_bodies[0]["model_override"] == _MEMBER_MODEL
    # The unset effort still comes from the chain.
    assert explicit_bodies[0]["reasoning_effort"] == "medium"

    _output, pinned_bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk", worker_model="pinned-model"),
        conv_id="conv_member_chain_pinned",
        labels={"unrelated": "1"},
        parent_snapshot={"id": "conv_member_chain_pinned", "agent_id": "ag_parent"},
        resolve_payload={"model": "chain-model", "effort": "medium"},
    )
    assert "model_override" not in pinned_bodies[0]
    assert pinned_bodies[0]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_snapshot_member_never_consults_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen member's dispatch stays on its snapshot values, no chain lookup."""
    _stub_worker_launchable(monkeypatch)
    queries: list[dict[str, str]] = []

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_chain_frozen",
        labels=_member_labels(),
        parent_snapshot={"id": "conv_member_chain_frozen", "agent_id": "ag_parent"},
        resolve_payload={"model": "chain-model", "effort": "medium"},
        resolve_queries=queries,
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _MEMBER_MODEL
    assert bodies[0]["reasoning_effort"] == "high"
    assert queries == []


@pytest.mark.asyncio
async def test_malformed_member_label_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt member label behaves like no snapshot entry, not a crash."""
    _stub_worker_launchable(monkeypatch)

    output, bodies = await _dispatch(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_member_malformed",
        labels={member_label_key("worker"): "{not json"},
        parent_snapshot={
            "id": "conv_member_malformed",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": _PARENT_MODEL,
            "llm_model": None,
        },
    )

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert bodies[0]["model_override"] == _PARENT_MODEL


def test_launched_from_library_agent_reads_the_template_label() -> None:
    """Only a ``ca_`` id under the create-time template label marks a launch."""
    assert launched_from_library_agent(None) is False
    assert launched_from_library_agent({}) is False
    assert launched_from_library_agent({LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "ca_abc"}) is True
    assert launched_from_library_agent({LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "ag_builtin"}) is False
    assert launched_from_library_agent({LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "polly"}) is False
    non_str: dict[str, Any] = {LIBRARY_AGENT_TEMPLATE_LABEL_KEY: 7}
    assert launched_from_library_agent(non_str) is False


def test_member_lock_applies_only_to_a_true_locked_entry() -> None:
    """Only the boolean ``locked is True`` marks a locked entry."""
    assert member_lock_applies({MEMBER_LOCKED_FIELD: True}) is True
    assert member_lock_applies({MEMBER_LOCKED_FIELD: False}) is False
    assert member_lock_applies({}) is False
    assert member_lock_applies({MEMBER_LOCKED_FIELD: "true"}) is False


_LEGACY_MEMBER_ENTRY: dict[str, Any] = {
    "host": None,
    "harness": "claude-sdk",
    "model": _MEMBER_MODEL,
    "effort": "medium",
    "lead": False,
}


def test_unlocked_legacy_member_labels_freezes_a_pre_field_entry() -> None:
    """A legacy entry gains ``locked: false`` without other field changes."""
    labels = {member_label_key("worker"): encode_member_entry(_LEGACY_MEMBER_ENTRY)}

    rewritten = unlocked_legacy_member_labels(labels)

    expected = {**_LEGACY_MEMBER_ENTRY, MEMBER_LOCKED_FIELD: False}
    assert rewritten == {member_label_key("worker"): encode_member_entry(expected)}
    entries = member_entries_from_labels(
        {**labels, **rewritten, LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "ca_saved"}
    )
    assert entries == {"worker": expected}


def test_unlocked_legacy_member_labels_skips_a_library_agent_session() -> None:
    """A ``ca_`` launch already resolves members to locked; nothing to freeze."""
    labels = {
        member_label_key("worker"): encode_member_entry(_LEGACY_MEMBER_ENTRY),
        LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "ca_saved",
    }

    assert unlocked_legacy_member_labels(labels) == {}


def test_unlocked_legacy_member_labels_keeps_explicit_lock_values() -> None:
    """Entries already carrying ``locked`` are left untouched, either value."""
    labels = {
        member_label_key("lead"): encode_member_entry(
            {**_LEGACY_MEMBER_ENTRY, "lead": True, MEMBER_LOCKED_FIELD: True}
        ),
        member_label_key("worker"): encode_member_entry(
            {**_LEGACY_MEMBER_ENTRY, MEMBER_LOCKED_FIELD: False}
        ),
    }

    assert unlocked_legacy_member_labels(labels) == {}


def test_unlocked_legacy_member_labels_ignores_malformed_and_non_member_labels() -> None:
    """Non-member keys and values that are not a JSON object yield nothing."""
    labels = {
        "unrelated": "1",
        LIBRARY_AGENT_TEMPLATE_LABEL_KEY: "ag_builtin",
        member_label_key("lead"): "[]",
        member_label_key("worker"): "{not json",
    }

    assert unlocked_legacy_member_labels(labels) == {}


def test_unlocked_legacy_member_labels_drops_an_over_cap_rewrite() -> None:
    """A rewrite that would overflow the 256-char label is left out."""
    entry = {**_LEGACY_MEMBER_ENTRY, "model": "m" * 165}
    value = encode_member_entry(entry)
    assert len(value) <= MEMBER_LABEL_VALUE_MAX_CHARS
    assert (
        len(encode_member_entry({**entry, MEMBER_LOCKED_FIELD: False}))
        > MEMBER_LABEL_VALUE_MAX_CHARS
    )

    assert unlocked_legacy_member_labels({member_label_key("worker"): value}) == {}
