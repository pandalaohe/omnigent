"""Unit tests for the OpenCode permission policy evaluator wiring.

The runner wires this evaluator into the OpenCode permission forwarder so
every ``permission.v2.asked`` request is decided by the SAME server-side
policy/approval gate codex-native uses (``POST /policies/evaluate``), not
silently auto-approved. These tests pin the request shape, the verdict
mapping, and — critically — that every failure mode fails CLOSED.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any

import httpx
import pytest

from omnigent.native import native_policy_hook
from omnigent.runner.app import _build_opencode_policy_evaluator


class _FakeServerClient:
    """httpx-shaped stub recording the policy-evaluate POST.

    :param script: Optional per-call outcomes. Each entry is an exception to
        raise or a ``(status, body)`` response tuple; the last entry repeats
        once the script runs out.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
        script: list[Any] | None = None,
    ) -> None:
        self._status = status
        self._body = body
        self._raise_exc = raise_exc
        self._script = list(script) if script is not None else None
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        self.calls.append((url, json, timeout))
        if self._script is not None:
            step = self._script.pop(0) if len(self._script) > 1 else self._script[0]
            if isinstance(step, Exception):
                raise step
            status, body = step
            content = b"" if body is None else _json.dumps(body).encode()
            return httpx.Response(status, content=content, request=httpx.Request("POST", url))
        if self._raise_exc is not None:
            raise self._raise_exc
        content = b"" if self._body is None else _json.dumps(self._body).encode()
        return httpx.Response(self._status, content=content, request=httpx.Request("POST", url))


async def test_evaluator_posts_tool_call_event_and_maps_allow() -> None:
    """ALLOW maps to the ``allow`` verdict; the POST carries a tool-call event."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    verdict = await evaluate(
        {"action": "bash", "command": "ls", "path": None, "url": None, "metadata": {}}
    )
    assert verdict == {"decision": "allow"}
    url, body, _timeout = client.calls[0]
    assert url == "/v1/sessions/conv_1/policies/evaluate"
    event = body["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"]["name"] == "bash"
    # Only the concrete, present resources reach the policy engine.
    assert event["data"]["arguments"] == {"command": "ls"}
    assert event["context"]["harness"] == "opencode-native"


async def test_evaluator_maps_deny_and_ask() -> None:
    """DENY → ``deny``; ASK → ``ask`` (the forwarder fails an unresolved ask closed)."""
    for action, decision in (("POLICY_ACTION_DENY", "deny"), ("POLICY_ACTION_ASK", "ask")):
        client = _FakeServerClient(body={"result": action})
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        verdict = await evaluate({"action": "edit"})
        assert verdict == {"decision": decision}


async def test_evaluator_maps_unknown_verdict_to_ask() -> None:
    """An unrecognized verdict fails closed (``ask`` → reject downstream)."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_SOMETHING_NEW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "ask"}


async def test_evaluator_fails_closed_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Zero the retry budget: this stub fails instantly, and the real budget
    # would make the test wait its full backoff schedule out.
    monkeypatch.setattr(native_policy_hook, "TOOL_CALL_POLICY_RETRY_BUDGET_S", 0.0)
    client = _FakeServerClient(raise_exc=httpx.ConnectError("boom"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_evaluator_fails_closed_on_non_200_or_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_policy_hook, "TOOL_CALL_POLICY_RETRY_BUDGET_S", 0.0)
    for status, body in ((500, {"result": "POLICY_ACTION_ALLOW"}), (200, None)):
        client = _FakeServerClient(status=status, body=body)
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_evaluator_rides_out_transient_failures_with_one_elicitation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx and a connect failure are retried; the late ALLOW is the verdict.

    Every attempt must carry the SAME elicitation id, so a retry re-attaches
    to a parked ASK instead of publishing a second approval card.
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    client = _FakeServerClient(
        script=[
            (500, {"result": "POLICY_ACTION_ALLOW"}),
            httpx.ConnectError("dns outage"),
            (200, {"result": "POLICY_ACTION_ALLOW"}),
        ]
    )
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "allow"}
    assert len(client.calls) == 3, "both transient failures must be retried"
    ids = {body["_omnigent_elicitation_id"] for _, body, _ in client.calls}
    assert len(ids) == 1, "every attempt must re-use one elicitation id"
    assert re.fullmatch(r"elicit_evaluate_[0-9a-f]{32}", ids.pop())


async def test_evaluator_denies_4xx_without_retry() -> None:
    client = _FakeServerClient(script=[(400, {"error": "bad request"})])
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}
    assert len(client.calls) == 1, "a 4xx is final and must not be retried"


async def test_evaluator_persistent_outage_stops_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent outage denies once the start-of-attempt budget rule fires."""
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", _record_sleep)
    monkeypatch.setattr(native_policy_hook, "TOOL_CALL_POLICY_RETRY_BUDGET_S", 3.5)
    client = _FakeServerClient(raise_exc=httpx.ConnectError("server down"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}
    assert len(client.calls) == 3, "attempts must stop once the next start passes the budget"
    assert sleeps == [1.0, 2.0], "backoff must double from 1s and stop at the budget"
