"""Usage tests for Claude-native forwarding."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import omnigent.harnesses.claude_native.bridge as claude_native_bridge
import omnigent.harnesses.claude_native.forwarder as forwarder


def test_usage_from_status_state_surfaces_cumulative_cost() -> None:
    """
    ``_usage_from_status_state`` surfaces ``total_cost_usd`` as
    ``cumulative_cost_usd`` so the forwarder posts it for native cost tracking.

    Failure means Claude Code's captured cost never reaches the server, so
    native ``session_usage.total_cost_usd`` stays 0.
    """
    state = {
        "context_window_size": 1_000_000,
        "current_usage": {"input_tokens": 6, "output_tokens": 50},
        "total_cost_usd": 0.42,
    }
    result = forwarder._usage_from_status_state(state)
    assert result is not None
    assert result["cumulative_cost_usd"] == 0.42
    # Token fields still flow for the context ring.
    assert result["input_tokens"] == 6
    assert result["output_tokens"] == 50


def test_usage_from_status_state_omits_cost_when_absent() -> None:
    """
    Without ``total_cost_usd`` in state, no ``cumulative_cost_usd`` is emitted.

    Older Claude Code versions (or a statusLine without a cost block) must not
    cause a bogus 0-cost post that would overwrite a real value with SET.
    """
    state = {
        "context_window_size": 1_000_000,
        "current_usage": {"input_tokens": 6, "output_tokens": 50},
    }
    result = forwarder._usage_from_status_state(state)
    assert result is not None
    assert "cumulative_cost_usd" not in result


@pytest.fixture
def otel_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh TracerProvider with an in-memory exporter for one test.

    Restores the previous provider on teardown so OTel's set-once
    semantics do not leak into later tests in the same process.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    previous = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    in_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_mem))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    try:
        yield in_mem
    finally:
        in_mem.clear()
        with contextlib.suppress(Exception):
            provider.shutdown()
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_done  # type: ignore[attr-defined]


def _ok_usage_client() -> httpx.AsyncClient:
    """
    Build a client whose ``POST /events`` always succeeds.

    :returns: Client backed by a mock transport returning ``200``.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        base_url="http://omnigent.test",
    )


@pytest.mark.asyncio
async def test_post_session_usage_records_gen_ai_token_attributes(
    otel_exporter: InMemorySpanExporter,
) -> None:
    """
    A native Claude usage post carries the turn's tokens as ``gen_ai.usage.*``.

    A native turn runs to completion in the terminal, so the harness
    executor's ``TurnComplete`` reports no usage and the agent span closes
    without token attributes. This post is where the real counts are known,
    so it must record them or the session's tokens stay invisible to
    MLflow / any OTel backend.
    """
    async with _ok_usage_client() as client:
        await forwarder._post_external_session_usage(
            client,
            session_id="conv_abc123",
            usage={"context_tokens": 1773, "input_tokens": 1523, "output_tokens": 847},
            context_window=200_000,
            token_usage={
                "input_tokens": 1523,
                "output_tokens": 847,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 50,
            },
        )

    spans = [s for s in otel_exporter.get_finished_spans() if s.name == "claude_native.usage"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.usage.input_tokens"] == 1523
    assert attrs["gen_ai.usage.output_tokens"] == 847
    assert attrs["gen_ai.usage.total_tokens"] == 1523 + 847
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 200
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 50


@pytest.mark.asyncio
async def test_post_session_usage_without_token_usage_records_no_tokens(
    otel_exporter: InMemorySpanExporter,
) -> None:
    """
    A post with no ``token_usage`` records no token attributes.

    Cost posts and context-window-only posts reach the same helper carrying
    a usage snapshot but no new counts. Falling back to that snapshot would
    re-record a figure already counted, or report a 0-token turn on every
    cost tick.
    """
    async with _ok_usage_client() as client:
        await forwarder._post_external_session_usage(
            client,
            session_id="conv_abc123",
            usage={"cumulative_cost_usd": 0.42, "model": "claude-opus-4-8"},
        )

    spans = [s for s in otel_exporter.get_finished_spans() if s.name == "claude_native.usage"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert not [key for key in attrs if key.startswith("gen_ai.usage.")]


@pytest.mark.asyncio
async def test_forwarder_records_each_api_call_usage_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_exporter: InMemorySpanExporter,
) -> None:
    """
    Each assistant API call contributes exactly one usage span.

    The usage POST re-fires whenever the statusLine gauge or the context
    window moves, which happens several times per API call. Recording the
    snapshot on each of those would make a backend that SUMS
    ``gen_ai.usage.*`` across spans multiply-count the same prompt. Only a
    new completed assistant record may add a span.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    def _assistant(uuid: str, text: str, usage: dict[str, int]) -> str:
        """
        Build one assistant JSONL line carrying ``message.usage``.

        :param uuid: Transcript entry uuid, e.g. ``"a1"``.
        :param text: Assistant text content.
        :param usage: Anthropic ``message.usage`` block for the call.
        :returns: A JSON-encoded transcript line.
        """
        return json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": text}],
                    "usage": usage,
                },
            }
        )

    # The statusLine gauge moves every poll (a streaming message's output
    # grows, cache reads land) — the churn that used to re-record tokens.
    status_box = {"value": {"input_tokens": 1000, "output_tokens": 10}}
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {"context_window_size": 200_000, "current_usage": status_box["value"]},
    )

    transcript_path.write_text(
        _assistant("a1", "hi", {"input_tokens": 1000, "output_tokens": 50}) + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    dedupe = forwarder._ForwardDedupeState()
    retry_tracker = forwarder._PostRetryTracker()

    transport = httpx.MockTransport(lambda _request: httpx.Response(202, json={}))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def poll() -> None:
            """Run one forwarder poll against the shared cursor state."""
            nonlocal state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )

        await poll()
        # Same API call, gauge still moving: re-posts usage, records nothing.
        status_box["value"] = {"input_tokens": 1000, "output_tokens": 40}
        await poll()
        status_box["value"] = {
            "input_tokens": 1000,
            "output_tokens": 50,
            "cache_read_input_tokens": 900,
        }
        await poll()

        recorded = _recorded_token_spans(otel_exporter)
        assert recorded == [(1000, 50)], "one completed API call must record exactly one span"

        # A second API call is new usage and does add a span — and the
        # statusLine gauge deliberately does NOT move for it. The side-channel
        # tail runs on quiet polls too, so the gauge comparison has already
        # caught up to this snapshot; if the span were gated on that
        # comparison, a2's tokens would be dropped and never retried.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant("a2", "more", {"input_tokens": 2200, "output_tokens": 80}) + "\n")
        await poll()

    assert _recorded_token_spans(otel_exporter) == [(1000, 50), (2200, 80)]
    assert dedupe.recorded_token_usage == {"input_tokens": 2200, "output_tokens": 80}


def _recorded_token_spans(exporter: InMemorySpanExporter) -> list[tuple[int, int]]:
    """
    Collect ``(input_tokens, output_tokens)`` from every usage span recorded.

    :param exporter: In-memory exporter holding the finished spans.
    :returns: One pair per span that carried token attributes, in order.
    """
    pairs: list[tuple[int, int]] = []
    for span in exporter.get_finished_spans():
        attrs = dict(span.attributes or {})
        if "gen_ai.usage.input_tokens" in attrs:
            pairs.append((attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]))
    return pairs


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # The cost tag and the derived context gauge are not token counters.
        (
            {"context_tokens": 1773, "input_tokens": 1523, "output_tokens": 847},
            {"input_tokens": 1523, "output_tokens": 847},
        ),
        ({"cumulative_cost_usd": 0.42, "model": "claude-opus-4-8"}, None),
        ({"context_tokens": 1773}, None),
        (None, None),
    ],
)
def test_gen_ai_usage_tokens_keeps_only_token_counters(
    usage: dict[str, float | str] | None,
    expected: dict[str, int] | None,
) -> None:
    """
    Only real input/output token counters survive into the OTel payload.

    :param usage: Usage payload posted to the Sessions API.
    :param expected: Token counts to record, or ``None`` for no recording.
    """
    assert forwarder._gen_ai_usage_tokens(usage) == expected


# ── session cost reconciliation (max(S, C)) ───────────────────────────


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"total_cost_usd": 0.5}, 0.5),
        ({"total_cost_usd": 0}, 0.0),
        ({"total_cost_usd": -1.0}, None),  # negative rejected
        ({"total_cost_usd": True}, None),  # bool rejected (not a real cost)
        ({"total_cost_usd": "x"}, None),  # non-numeric rejected
        ({}, None),  # absent
        (None, None),  # no statusLine yet
    ],
)
def test_cumulative_cost_from_status_state(
    state: dict[str, Any] | None, expected: float | None
) -> None:
    """Only a non-negative numeric ``total_cost_usd`` yields a cost."""
    assert forwarder._cumulative_cost_from_status_state(state) == expected


def test_transcript_cost_size_cached_recomputes_only_on_growth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    An unchanged transcript is not read again; a grown one is.

    Guards the per-poll optimization: an unchanged transcript must not be
    re-parsed every 0.25s tick, but a grown one must be read from its
    previous offset rather than from the start.
    """
    reads: list[int] = []

    def fake_accumulate(
        path: Path,
        ledger: object,
        *,
        byte_offset: int,
        start_line: int,
        include_sidechains: bool,
    ) -> tuple[int, int]:
        del ledger, include_sidechains
        reads.append(byte_offset)
        return path.stat().st_size, start_line

    monkeypatch.setattr(forwarder, "accumulate_transcript_cost", fake_accumulate)
    monkeypatch.setattr(forwarder, "price_transcript_cost_ledger", lambda ledger: 3.0)
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    path = tmp_path / "t.jsonl"
    path.write_text("abc", encoding="utf-8")  # 3 bytes
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(3.0)
    assert reads == [0]
    # Second call at the same size → served from cache, no read.
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(3.0)
    assert reads == [0]
    # File grows → read only the appended bytes.
    path.write_text("abcdef", encoding="utf-8")  # 6 bytes
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(3.0)
    assert reads == [0, 3]
    # Missing file → None, no read.
    assert (
        forwarder._transcript_cost_size_cached(
            tmp_path / "missing.jsonl", include_sidechains=True, cache=cache
        )
        is None
    )
    assert reads == [0, 3]


def test_session_cost_estimate_takes_max_of_status_and_transcript_sum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``C`` sums parent + sub-agent transcript cost; the result is max(S, C).

    During a sub-agent run the real-time transcript sum (C) exceeds the
    lagging statusLine total (S), so C is used — this is what lets the
    parent budget see the sub-agent's spend mid-turn. Once S settles
    higher than C, S is used.
    """
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")
    subagents_dir = forwarder._subagents_dir_for_transcript(parent)
    subagents_dir.mkdir(parents=True)
    sub_path = subagents_dir / "agent-aaa.jsonl"
    sub_path.write_text("sub", encoding="utf-8")

    per_path_cost = {parent: 0.10, sub_path: 0.55}

    def fake_size_cached(
        path: Path,
        *,
        include_sidechains: bool,
        cache: dict[Path, forwarder._TranscriptCostCacheEntry],
    ) -> float | None:
        del include_sidechains, cache
        return per_path_cost.get(path)

    monkeypatch.setattr(forwarder, "_transcript_cost_size_cached", fake_size_cached)
    entries = [forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")]

    # S stale ($0.005) < C (0.10 + 0.55 = 0.65) → C wins (mid-run).
    assert forwarder._session_cost_estimate(
        parent_transcript_path=parent,
        active_subagents=entries,
        status_cost=0.005,
        cost_cache={},
    ) == pytest.approx(0.65)

    # S settled ($2.00) > C → S wins (no double-count after settle).
    assert forwarder._session_cost_estimate(
        parent_transcript_path=parent,
        active_subagents=entries,
        status_cost=2.0,
        cost_cache={},
    ) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_forward_session_cost_splits_display_and_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``_forward_session_cost`` posts S for display and max(S, C) for policy.

    With a sub-agent present the two fields advance independently and
    monotonically:

    - ``cumulative_cost_usd`` (display) = the statusLine total S verbatim,
      so the badge matches ``/cost``. It stays frozen while S is frozen,
      then jumps when the turn settles.
    - ``policy_cost_usd`` (enforcement) = max(S, transcript estimate C),
      so the gate sees in-flight sub-agent spend while S is frozen.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")

    # statusLine total (S) and transcript estimate (C) are both stubbed so
    # the test can drive them independently across polls.
    status_box = {"value": 0.01}
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {"total_cost_usd": status_box["value"]},
    )
    estimate_box = {"value": 0.65}
    monkeypatch.setattr(
        forwarder,
        "_session_cost_estimate",
        lambda **_kwargs: estimate_box["value"],
    )
    subagent_state = forwarder.SubagentForwardState(
        subagents={
            "aaa": forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")
        }
    )
    dedupe = forwarder._ForwardDedupeState()

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:

        async def run() -> None:
            await forwarder._forward_session_cost(
                client=client,
                session_id="conv_parent",
                bridge_dir=bridge_dir,
                parent_transcript_path=parent,
                subagent_state=subagent_state,
                dedupe=dedupe,
                cost_cache={},
            )

        # First poll: display = S (0.01) verbatim, policy = max(0.01, 0.65).
        # If display showed 0.65 here, the badge would diverge from /cost —
        # the exact bug this split fixes.
        await run()
        assert posted == [
            {
                "cumulative_cost_usd": pytest.approx(0.01),
                "policy_cost_usd": pytest.approx(0.65),
            }
        ]
        assert dedupe.posted_cost == pytest.approx(0.01)
        assert dedupe.posted_policy_cost == pytest.approx(0.65)

        # Nothing changed → neither field re-posts. A 2nd post would mean a
        # dedupe baseline wasn't honored.
        await run()
        assert len(posted) == 1

        # A lower transcript read must NOT walk policy back, and S is
        # unchanged → no post at all (both fields monotonic).
        estimate_box["value"] = 0.40
        await run()
        assert len(posted) == 1

        # C advances while S stays frozen (sub-agent still running): only
        # policy_cost_usd re-posts. Proves the badge (S) stays put mid-turn
        # while the gate sees the rising in-flight cost.
        estimate_box["value"] = 0.90
        await run()
        assert posted[-1] == {"policy_cost_usd": pytest.approx(0.90)}
        assert dedupe.posted_policy_cost == pytest.approx(0.90)
        # Display baseline untouched — S never advanced.
        assert dedupe.posted_cost == pytest.approx(0.01)

        # Turn settles: S jumps to the sub-agent-inclusive total. Display
        # advances; policy advances to the same settled value. Both post.
        status_box["value"] = 0.95
        estimate_box["value"] = 0.95
        await run()
        assert posted[-1] == {
            "cumulative_cost_usd": pytest.approx(0.95),
            "policy_cost_usd": pytest.approx(0.95),
        }
        assert dedupe.posted_cost == pytest.approx(0.95)
        assert dedupe.posted_policy_cost == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_forward_session_cost_posts_status_when_no_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    With no sub-agents, display and policy are both the statusLine total.

    There is no statusLine lag to correct without a sub-agent, so the
    transcript estimator must not run and both ``cumulative_cost_usd``
    (display) and ``policy_cost_usd`` (enforcement) equal S — they only
    diverge while a sub-agent is mid-run.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")
    monkeypatch.setattr(
        forwarder, "read_claude_context_state", lambda _bridge: {"total_cost_usd": 0.25}
    )

    def _fail_estimate(**_kwargs: Any) -> float | None:
        raise AssertionError("estimator must not run without sub-agents")

    monkeypatch.setattr(forwarder, "_session_cost_estimate", _fail_estimate)
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._forward_session_cost(
            client=client,
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            parent_transcript_path=parent,
            subagent_state=forwarder.SubagentForwardState(subagents={}),
            dedupe=dedupe,
            cost_cache={},
        )
    # Both fields = S (0.25). policy_cost_usd present so the gate has a value
    # without a sub-agent too; if it were missing, the engine would fall back
    # to total_cost_usd (also S) — but the forwarder posts it explicitly.
    assert posted == [
        {
            "cumulative_cost_usd": pytest.approx(0.25),
            "policy_cost_usd": pytest.approx(0.25),
        }
    ]
    assert dedupe.posted_cost == pytest.approx(0.25)
    assert dedupe.posted_policy_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_forward_session_cost_backs_off_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")
    monkeypatch.setattr(
        forwarder, "read_claude_context_state", lambda _bridge: {"total_cost_usd": 0.25}
    )
    now = {"value": 100.0}
    monkeypatch.setattr(forwarder.time, "monotonic", lambda: now["value"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "5"}, request=request)
        return httpx.Response(200, json={}, request=request)

    dedupe = forwarder._ForwardDedupeState()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        kwargs = {
            "client": client,
            "session_id": "conv_parent",
            "bridge_dir": bridge_dir,
            "parent_transcript_path": parent,
            "subagent_state": forwarder.SubagentForwardState(subagents={}),
            "dedupe": dedupe,
            "cost_cache": {},
        }
        await forwarder._forward_session_cost(**kwargs)
        assert len(requests) == 1
        assert dedupe.cost_retry_failures == 1
        assert dedupe.cost_retry_not_before == pytest.approx(105.0)

        before_retry = requests.copy()
        await forwarder._forward_session_cost(**kwargs)
        assert requests == before_retry

        now["value"] = 105.0
        await forwarder._forward_session_cost(**kwargs)

    assert len(requests) == 2
    assert dedupe.cost_retry_failures == 0
    assert dedupe.cost_retry_not_before == 0.0
    assert dedupe.posted_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_forward_session_cost_tags_display_advance_with_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A display-cost (S) advance is tagged with the statusLine's active model.

    claude-native sends no token counts with its cost, so the server has
    nothing to attribute the cost to in the per-model TOKEN USAGE view without
    a ``model`` tag — it would drop the cost from that view. The forwarder
    rides the statusLine model (captured in context.json) on the payload
    whenever the display cost advances. A policy-only mid-turn re-post (S
    frozen, only the gate estimate C advancing) carries NO model: there is no
    new display cost to attribute, so tagging it would be meaningless churn.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")

    status_box = {"value": 0.01}
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {"total_cost_usd": status_box["value"], "model": "claude-opus-4-8"},
    )
    estimate_box = {"value": 0.65}
    monkeypatch.setattr(
        forwarder,
        "_session_cost_estimate",
        lambda **_kwargs: estimate_box["value"],
    )
    subagent_state = forwarder.SubagentForwardState(
        subagents={
            "aaa": forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")
        }
    )
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:

        async def run() -> None:
            await forwarder._forward_session_cost(
                client=client,
                session_id="conv_parent",
                bridge_dir=bridge_dir,
                parent_transcript_path=parent,
                subagent_state=subagent_state,
                dedupe=dedupe,
                cost_cache={},
            )

        # Display cost advances → the model rides along for per-model attribution.
        await run()
        assert posted == [
            {
                "cumulative_cost_usd": pytest.approx(0.01),
                "policy_cost_usd": pytest.approx(0.65),
                "model": "claude-opus-4-8",
            }
        ]

        # Mid-turn: S frozen, only C (policy) advances → policy-only re-post
        # carries NO model (no new display cost to attribute).
        estimate_box["value"] = 0.90
        await run()
        assert posted[-1] == {"policy_cost_usd": pytest.approx(0.90)}


def test_provider_usage_limits_post_only_on_change_or_refresh() -> None:
    baseline: dict[str, object] = {
        "provider": "Claude",
        "captured_at": 1_000,
        "windows": [{"label": "5h", "used_percent": 21}],
    }
    assert forwarder._provider_usage_limits_should_post(baseline, None) is True
    assert (
        forwarder._provider_usage_limits_should_post(
            {**baseline, "captured_at": 1_299},
            baseline,
        )
        is False
    )
    assert (
        forwarder._provider_usage_limits_should_post(
            {**baseline, "captured_at": 1_300},
            baseline,
        )
        is True
    )
    assert (
        forwarder._provider_usage_limits_should_post(
            {
                **baseline,
                "captured_at": 1_001,
                "windows": [{"label": "5h", "used_percent": 22}],
            },
            baseline,
        )
        is True
    )


def test_usage_from_status_state_carries_last_call_cache_split() -> None:
    """
    The statusLine's cache split rides along as ``last_cache_*`` fields.

    The keep-warm sweeper ties a cache reading to a ping turn through these
    fields; both keys must be present on every post, 0 when the statusLine
    reported no cache activity.
    """
    state = {
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 6,
            "output_tokens": 50,
            "cache_read_input_tokens": 400,
            "cache_creation_input_tokens": 30,
        },
    }
    result = forwarder._usage_from_status_state(state)
    assert result is not None
    assert result["last_cache_read_input_tokens"] == 400
    assert result["last_cache_creation_input_tokens"] == 30

    bare = forwarder._usage_from_status_state(
        {"context_window_size": 1_000_000, "current_usage": {"input_tokens": 6}}
    )
    assert bare is not None
    assert bare["last_cache_read_input_tokens"] == 0
    assert bare["last_cache_creation_input_tokens"] == 0


def test_usage_with_last_cache_split_rewrites_transcript_cache_keys() -> None:
    """
    The transcript fallback's conditional cache keys become the ``last_*`` pair.

    :func:`bridge._usage_from_transcript_entry` only emits the raw
    ``cache_*`` keys when nonzero, so the forwarder must fill both
    ``last_*`` fields and drop the raw spelling.
    """
    mapped = forwarder._usage_with_last_cache_split(
        {
            "context_tokens": 40,
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 5,
        }
    )
    assert mapped["last_cache_read_input_tokens"] == 30
    assert mapped["last_cache_creation_input_tokens"] == 5
    assert "cache_read_input_tokens" not in mapped
    assert "cache_creation_input_tokens" not in mapped

    bare = forwarder._usage_with_last_cache_split(
        {"context_tokens": 10, "input_tokens": 10, "output_tokens": 2}
    )
    assert bare["last_cache_read_input_tokens"] == 0
    assert bare["last_cache_creation_input_tokens"] == 0


def _cost_transcript_entry(
    *,
    input_tokens: int,
    output_tokens: int,
    request_id: str | None = None,
    model: str = "m",
) -> dict[str, Any]:
    """
    Build one assistant transcript record with a usage block.

    :param input_tokens: Non-cached input tokens for the usage block.
    :param output_tokens: Output tokens for the usage block.
    :param request_id: Top-level ``requestId`` to stamp, or ``None`` to
        omit it.
    :param model: ``message.model`` to stamp.
    :returns: A decoded transcript record dict.
    """
    entry: dict[str, Any] = {
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    }
    if request_id is not None:
        entry["requestId"] = request_id
    return entry


def _append_cost_transcript(path: Path, entries: list[dict[str, Any]]) -> None:
    """
    Append decoded transcript records as newline-terminated JSONL.

    :param path: Destination JSONL path.
    :param entries: Decoded record dicts to append.
    :returns: None.
    """
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def test_transcript_cost_size_cached_parses_only_appended_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The incremental total equals a full re-parse and resumes at the cursor.

    A record reusing an earlier ``requestId`` with larger usage must replace
    the earlier price, so the append-only ledger stays equivalent to the
    whole-file computation.
    """
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "t.jsonl"
    _append_cost_transcript(
        path,
        [
            _cost_transcript_entry(input_tokens=2, output_tokens=3, request_id="req_A"),
            _cost_transcript_entry(input_tokens=1, output_tokens=1, request_id="req_B"),
            _cost_transcript_entry(input_tokens=4, output_tokens=0),
        ],
    )
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    first_size = path.stat().st_size
    first = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert first is not None

    _append_cost_transcript(
        path,
        [
            _cost_transcript_entry(input_tokens=5, output_tokens=5, request_id="req_A"),
            _cost_transcript_entry(input_tokens=3, output_tokens=1, request_id="req_C"),
        ],
    )
    offsets: list[int] = []
    real_read = claude_native_bridge._read_complete_jsonl_records

    def spy_read(p: Path, *, byte_offset: int, start_line: int, **kwargs: Any) -> Any:
        offsets.append(byte_offset)
        return real_read(p, byte_offset=byte_offset, start_line=start_line, **kwargs)

    monkeypatch.setattr(claude_native_bridge, "_read_complete_jsonl_records", spy_read)
    second = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert offsets == [first_size]
    assert second == pytest.approx(
        claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    )
    assert second is not None and second > first


def test_transcript_cost_size_cached_ignores_partial_appended_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half-written trailing line is billed only once its newline lands."""
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "t.jsonl"
    _append_cost_transcript(
        path,
        [_cost_transcript_entry(input_tokens=2, output_tokens=3, request_id="req_A")],
    )
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    stable = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert stable is not None

    entry = _cost_transcript_entry(input_tokens=7, output_tokens=7, request_id="req_B")
    line = (json.dumps(entry) + "\n").encode("utf-8")
    cut = len(line) // 2
    with path.open("ab") as handle:
        handle.write(line[:cut])
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(stable)

    with path.open("ab") as handle:
        handle.write(line[cut:])
    completed = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert completed == pytest.approx(
        claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    )
    assert completed is not None and completed > stable


def test_transcript_cost_size_cached_reparses_rewritten_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An in-place rewrite discards the cursor; a new inode starts fresh too."""
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "t.jsonl"
    _append_cost_transcript(
        path,
        [_cost_transcript_entry(input_tokens=2, output_tokens=3, request_id="req_A")],
    )
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    assert (
        forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
        is not None
    )

    # Same inode, different earlier usage, longer body.
    rewritten_payload = (
        json.dumps(_cost_transcript_entry(input_tokens=100, output_tokens=100, request_id="req_A"))
        + "\n"
        + json.dumps(_cost_transcript_entry(input_tokens=5, output_tokens=5, request_id="req_B"))
        + "\n"
    )
    with path.open("r+b") as handle:
        handle.write(rewritten_payload.encode("utf-8"))
    rewritten = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert rewritten == pytest.approx(
        claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    )

    # New inode via atomic replace.
    replacement = tmp_path / "replacement.jsonl"
    _append_cost_transcript(
        replacement,
        [_cost_transcript_entry(input_tokens=9, output_tokens=9, request_id="req_C")],
    )
    os.replace(replacement, path)
    replaced = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert replaced == pytest.approx(
        claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    )


def test_transcript_cost_size_cached_reprices_after_provider_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider-config change re-prices the already-parsed prefix."""

    def provider_config(input_per_million: float) -> dict[str, Any]:
        return {
            "providers": {
                "anthropic-local": {
                    "kind": "local",
                    "default": True,
                    "anthropic": {
                        "base_url": "http://anthropic.local/v1",
                        "api_key": "test",
                        "pricing": {
                            "input_per_million": input_per_million,
                            "output_per_million": 0.0,
                        },
                    },
                }
            }
        }

    active_config = provider_config(1.0)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: active_config)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "t.jsonl"
    _append_cost_transcript(
        path,
        [
            _cost_transcript_entry(
                input_tokens=1_000_000,
                output_tokens=0,
                request_id="req_A",
                model="self-hosted",
            )
        ],
    )
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(1.0)

    active_config = provider_config(2.0)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    _append_cost_transcript(
        path,
        [
            _cost_transcript_entry(
                input_tokens=1_000_000,
                output_tokens=0,
                request_id="req_B",
                model="self-hosted",
            )
        ],
    )
    total = forwarder._transcript_cost_size_cached(path, include_sidechains=True, cache=cache)
    assert total == pytest.approx(4.0)
    assert total == pytest.approx(
        claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    )


@pytest.mark.asyncio
async def test_forward_available_items_posts_auto_compact_window_once_and_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The user's auto-compaction window posts on first sight and on change.

    The window comes from the user's settings / environment, not the
    statusLine gauge, so it rides the usage post without re-posting a steady
    value every poll.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    # No statusLine state, so the auto-compaction window is the only value
    # that can trigger the usage post — each payload proves the trigger.
    monkeypatch.setattr(forwarder, "read_claude_context_state", lambda _bridge: None)
    window = {"value": 400_000}
    monkeypatch.setattr(forwarder, "read_user_auto_compact_window", lambda: window["value"])
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for value in (400_000, 400_000, 300_000, 300_000):
            window["value"] = value
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_auto_compact",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

    usage_posts = [r for r in requests if r["type"] == "external_session_usage"]
    assert [post["data"] for post in usage_posts] == [
        {"auto_compact_token_limit": 400_000},
        {"auto_compact_token_limit": 300_000},
    ]
    assert dedupe.auto_compact_token_limit == 300_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,expected_data",
    [
        pytest.param(
            200_000,
            {"context_tokens": 10, "auto_compact_token_limit": 200_000},
            id="set",
        ),
        pytest.param(None, {"context_tokens": 10}, id="unset"),
    ],
)
async def test_post_session_usage_auto_compact_window_key_is_conditional(
    limit: int | None,
    expected_data: dict[str, int],
) -> None:
    """An unset limit must send no key, leaving the persisted value untouched."""
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request), base_url="http://omnigent.test"
    ) as client:
        await forwarder._post_external_session_usage(
            client,
            session_id="conv_abc123",
            usage={"context_tokens": 10},
            auto_compact_token_limit=limit,
        )

    assert requests[0]["data"] == expected_data


@pytest.mark.asyncio
async def test_usage_post_stamps_last_usage_observed_at_and_keeps_it_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    One cache reading carries one observation stamp, retries included.

    The keep-warm sweeper ties a Claude reading to its ping turn through
    ``last_usage_observed_at``. An unchanged usage must not re-post every
    poll, a changed one stamps once, and a failed POST's retry must keep
    the original stamp instead of minting a fresh one each poll.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    status_state: dict[str, object] = {
        "context_window_size": 150_000,
        "current_usage": {
            "input_tokens": 10,
            "output_tokens": 1,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
        },
    }
    monkeypatch.setattr(forwarder, "read_claude_context_state", lambda _bridge: status_state)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(forwarder.time, "time", lambda: clock["now"])

    fail_usage = {"on": False}
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if body["type"] == "external_session_usage" and fail_usage["on"]:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    dedupe = forwarder._ForwardDedupeState()

    async def _poll(client: httpx.AsyncClient) -> forwarder.TranscriptForwardState:
        return await forwarder._forward_available_items(
            client=client,
            session_id="conv_keep_warm",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Poll 1 at t=1000: the first usage snapshot posts with its stamp.
        state = await _poll(client)
        # Poll 2 at t=1060: only time passes — no new usage POST.
        clock["now"] = 1_060.0
        state = await _poll(client)
        # Poll 3 at t=2000: the counters moved; the stamped POST fails.
        status_state["current_usage"] = {
            "input_tokens": 20,
            "output_tokens": 1,
            "cache_read_input_tokens": 8,
            "cache_creation_input_tokens": 3,
        }
        fail_usage["on"] = True
        clock["now"] = 2_000.0
        state = await _poll(client)
        # Poll 4 at t=3000: the same counters retry with the SAME stamp.
        fail_usage["on"] = False
        clock["now"] = 3_000.0
        state = await _poll(client)

    usage_posts = [r for r in requests if r["type"] == "external_session_usage"]
    assert [p["data"]["last_usage_observed_at"] for p in usage_posts] == [1000, 2000, 2000]
    first = usage_posts[0]["data"]
    assert first["last_cache_read_input_tokens"] == 4
    assert first["last_cache_creation_input_tokens"] == 2
    # 20 + 8 + 3 on the retried snapshot.
    assert usage_posts[-1]["data"]["context_tokens"] == 31


@pytest.mark.asyncio
async def test_usage_post_from_transcript_fallback_carries_last_cache_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The transcript fallback posts the same three ``last_*`` fields.

    Before the statusLine fires, ``latest_usage`` is the only usage source;
    the server's ``omnigent.last_cache`` label must still be writable, so
    the fallback maps its conditional ``cache_*`` keys onto the ``last_*``
    pair and stamps the observation time.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 5,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    monkeypatch.setattr(forwarder, "read_claude_context_state", lambda _bridge: None)
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_fallback",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
        )

    usage_post = next(r for r in requests if r["type"] == "external_session_usage")
    assert usage_post["data"]["last_cache_read_input_tokens"] == 30
    assert usage_post["data"]["last_cache_creation_input_tokens"] == 5
    assert usage_post["data"]["last_usage_observed_at"] > 0
