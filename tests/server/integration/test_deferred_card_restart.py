"""
Detached cards (deferred approvals, async question cards) across a server restart.

Each test serves one app through its real lifespan, leaves a card in some
state, stops that app, wipes the process memory a restart loses (elicitation
registries, the pending index, approval grants) and serves a second app on the
same database. Whatever the first server owed — the card to the user, the
verdict to the agent, the grant for the re-issued call — must still arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import _elicitation_registry
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_route
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.approval_grants import approval_grants
from omnigent.server.routes.sessions import routes_hooks as hooks_routes
from omnigent.server.schemas import ElicitationResult
from omnigent.server.user_preferences_store import ApprovalTimeout
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.helpers import create_session_for_agent, create_test_agent

pytestmark = pytest.mark.asyncio

_DEFERRED = ApprovalTimeout(timeout_s=3000.0, stop_turn=True, async_approvals=True)


class _Deliveries:
    """Record verdict deliveries; ``hold`` keeps them in flight like an offline runner."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.hold = False

    async def __call__(
        self,
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        persist_failure: bool = True,
    ) -> bool:
        if self.hold:
            await asyncio.Event().wait()
        self.calls.append((session_id, text))
        return True


@pytest.fixture(autouse=True)
def _deferred_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner keeps the server default: approvals are deferred."""
    monkeypatch.setattr(sessions_route, "read_approval_timeout", lambda store, owner: _DEFERRED)


@pytest.fixture()
def deliveries(monkeypatch: pytest.MonkeyPatch) -> _Deliveries:
    recorder = _Deliveries()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _clean_process_state() -> None:
    _wipe_process_memory()
    yield
    _wipe_process_memory()


def _wipe_process_memory() -> None:
    """Drop everything a server process holds only in memory."""
    _elicitation_registry.reset_for_tests()
    pending_elicitations.reset_for_tests()
    approval_grants.clear()


def _build_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@contextlib.asynccontextmanager
async def _serve(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Run one server process: lifespan start, requests, lifespan stop."""
    app = _build_app(db_uri, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    _wipe_process_memory()


def _bash_payload(command: str, description: str = "run it") -> dict[str, Any]:
    return {
        "session_id": "claude_sess_abc",
        "transcript_path": "/opt/work/transcript.jsonl",
        "cwd": "/opt/work/omnigent/fork/wt",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": description},
    }


async def _request_bash(client: httpx.AsyncClient, session_id: str, command: str) -> str:
    """Ask for a Bash call; return the hook's decision behavior."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request", json=_bash_payload(command)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["hookSpecificOutput"]["decision"]["behavior"]


async def _pending(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["pending_elicitations"]


async def _answer(
    client: httpx.AsyncClient,
    session_id: str,
    elicitation_id: str,
    action: str,
    content: dict[str, Any] | None = None,
) -> None:
    data: dict[str, Any] = {"elicitation_id": elicitation_id, "action": action}
    if content is not None:
        data["content"] = content
    resp = await client.post(
        f"/v1/sessions/{session_id}/events", json={"type": "approval", "data": data}
    )
    assert resp.status_code == 202, resp.text


async def _wait_for(predicate: Any, *, timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await asyncio.sleep(0.01)


async def _new_session(client: httpx.AsyncClient, name: str) -> str:
    agent = await create_test_agent(client, name)
    return await create_session_for_agent(client, agent["id"])


async def test_pending_approval_is_answerable_after_restart(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """The card returns under its id; Approve reaches the agent and the re-run is allowed."""
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-pending")
        assert await _request_bash(client, session_id, "pnpm vitest") == "deny"
        (card,) = await _pending(client, session_id)

    async with _serve(db_uri, tmp_path) as client:
        restored = await _pending(client, session_id)
        assert [entry["elicitation_id"] for entry in restored] == [card["elicitation_id"]], (
            "the pending approval card did not survive the restart"
        )
        assert restored[0]["params"]["approval_ref"] == card["params"]["approval_ref"]
        await _answer(client, session_id, card["elicitation_id"], "accept")
        aid = card["params"]["approval_ref"]
        await _wait_for(lambda: bool(deliveries.calls))
        assert deliveries.calls[-1][0] == session_id
        assert f"[System: approval #{aid} granted: re-run Bash(" in deliveries.calls[-1][1]
        assert await _request_bash(client, session_id, "pnpm vitest") == "allow"
        assert await _pending(client, session_id) == []


async def test_parent_handoff_survives_restart_and_cannot_be_taken_back(
    runtime_init, db_uri, tmp_path, deliveries, monkeypatch
):
    from unittest.mock import AsyncMock

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.child_approvals import APPROVAL_KIND
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.standing_approval import StandingApprovalService

    store = SqlAlchemyConversationStore(db_uri)
    service = StandingApprovalService(store)
    monkeypatch.setattr(orchestration, "_post_system_message", AsyncMock(return_value=True))
    async with _serve(db_uri, tmp_path) as client:
        parent_id = await _new_session(client, "parent")
        parent = store.get_conversation(parent_id)
        service.change(parent, "grant", [APPROVAL_KIND], "approval:fixture")
        store.set_runner_id(parent_id, token_bound_runner_id("fixture-binding"))
        created = await client.post(
            "/v1/sessions",
            json={
                "agent_id": parent.agent_id,
                "parent_session_id": parent_id,
            },
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["id"]
        assert await _request_bash(client, child_id, "pnpm build") == "deny"
        (card,) = await _pending(client, child_id)
        assert card["params"]["triage"]["state"] == "offered"
        url = f"/v1/sessions/{parent_id}/child-approvals/{child_id}/{card['elicitation_id']}"
        headers = {RUNNER_TUNNEL_TOKEN_HEADER: "fixture-binding"}
        response = await client.post(
            url,
            headers=headers,
            json={
                "decision": "pass_to_user",
                "reason": "Needs the user's decision",
                "note": "这次构建需要你决定是否运行。",
                "recommend": "approve",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "passed"}

    async with _serve(db_uri, tmp_path) as client:
        (restored,) = await _pending(client, child_id)
        assert restored["params"]["triage"]["state"] == "passed"
        assert restored["params"]["triage"]["note"] == "这次构建需要你决定是否运行。"
        response = await client.post(
            url,
            headers=headers,
            json={
                "decision": "approve",
                "reason": "Attempting to take it back",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "refused", "code": "passed"}
        await _answer(client, child_id, card["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))
        assert await _request_bash(client, child_id, "pnpm build") == "allow"


async def test_grant_survives_a_restart_before_the_rerun(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """An approval given before the restart still lets the re-issued call run once."""
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-grant")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))

    async with _serve(db_uri, tmp_path) as client:
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        assert await _request_bash(client, session_id, "pnpm build") == "deny"

    async with _serve(db_uri, tmp_path) as client:
        # A consumed grant stays consumed across the next restart.
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_verdict_in_flight_is_delivered_after_restart(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """A denial whose delivery had not landed reaches the agent from the next server."""
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-inflight")
        assert await _request_bash(client, session_id, "rm -rf build") == "deny"
        (card,) = await _pending(client, session_id)
        deliveries.hold = True
        await _answer(
            client, session_id, card["elicitation_id"], "decline", content={"feedback": "no"}
        )
        await asyncio.sleep(0.1)
        assert deliveries.calls == []

    deliveries.hold = False
    async with _serve(db_uri, tmp_path) as client:
        await _wait_for(lambda: bool(deliveries.calls))
        aid = card["params"]["approval_ref"]
        assert deliveries.calls == [
            (
                session_id,
                f'[System: approval #{aid} denied by the user: do not run Bash({{"command": '
                f'"rm -rf build", "description": "run it"}}). Feedback: no]',
            )
        ]
        assert await _pending(client, session_id) == []


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_failed_delivery_window_keeps_verdict_for_runner_recovery(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """An offline runner does not discard an answered card after retry exhaustion."""
    attempted = asyncio.Event()
    online = False
    delivered: list[str] = []

    async def _delivery(
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        persist_failure: bool = True,
    ) -> bool:
        attempted.set()
        if not online:
            return False
        delivered.append(text)
        return True

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _delivery)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-offline-verdict-recovery")
        if kind == "approval":
            assert await _request_bash(client, session_id, "pnpm lint") == "deny"
            (card,) = await _pending(client, session_id)
            elicitation_id = card["elicitation_id"]
            await _answer(client, session_id, elicitation_id, "decline")
        else:
            response = await client.post(
                f"/v1/sessions/{session_id}/async-questions",
                json={"questions": [{"question": "Which DB?"}]},
            )
            assert response.status_code == 200
            elicitation_id = response.json()["elicitation_id"]
            await _answer(
                client, session_id, elicitation_id, "accept", content={"Which DB?": "SQLite"}
            )
        await asyncio.wait_for(attempted.wait(), timeout=3)
        await asyncio.sleep(0.1)
        assert _pending_record(store, elicitation_id)
        assert delivered == []

    online = True
    async with _serve(db_uri, tmp_path):
        await _wait_for(lambda: bool(delivered))
        assert len(delivered) == 1
        assert ("denied" if kind == "approval" else "SQLite") in delivered[0]


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_answered_card_recovers_from_transient_record_read_failure(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A failed read after recording the answer must not strand its delivery."""
    original = SqlAlchemyConversationStore.list_detached_cards
    failed_reads = 0

    def _fail_first_answered_read(self: SqlAlchemyConversationStore) -> list[Any]:
        nonlocal failed_reads
        rows = original(self)
        if failed_reads == 0 and any(row.state == "answered" for row in rows):
            failed_reads += 1
            raise RuntimeError("temporary database read failure")
        return rows

    monkeypatch.setattr(
        SqlAlchemyConversationStore, "list_detached_cards", _fail_first_answered_read
    )
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-answer-read-recovery")
        if kind == "approval":
            assert await _request_bash(client, session_id, "pnpm lint") == "deny"
            (card,) = await _pending(client, session_id)
            await _answer(client, session_id, card["elicitation_id"], "accept")
        else:
            response = await client.post(
                f"/v1/sessions/{session_id}/async-questions",
                json={"questions": [{"question": "Which DB?"}]},
            )
            assert response.status_code == 200
            await _answer(
                client,
                session_id,
                response.json()["elicitation_id"],
                "accept",
                content={"Which DB?": "SQLite"},
            )
        await _wait_for(lambda: bool(deliveries.calls))
        assert failed_reads == 1
        assert len(deliveries.calls) == 1
        assert ("granted" if kind == "approval" else "SQLite") in deliveries.calls[0][1]


def _pending_record(store: SqlAlchemyConversationStore, elicitation_id: str) -> bool:
    return any(card.elicitation_id == elicitation_id for card in store.list_detached_cards())


async def test_question_card_is_answerable_after_restart(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """An async question card returns after the restart and its answer is delivered."""
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-question")
        resp = await client.post(
            f"/v1/sessions/{session_id}/async-questions",
            json={"questions": [{"question": "Which DB?", "options": [{"label": "SQLite"}]}]},
        )
        assert resp.status_code == 200, resp.text
        asked = resp.json()

    async with _serve(db_uri, tmp_path) as client:
        restored = await _pending(client, session_id)
        assert [entry["elicitation_id"] for entry in restored] == [asked["elicitation_id"]], (
            "the question card did not survive the restart"
        )
        await _answer(
            client, session_id, asked["elicitation_id"], "accept", content={"Which DB?": "SQLite"}
        )
        await _wait_for(lambda: bool(deliveries.calls))
        assert deliveries.calls == [
            (
                session_id,
                f"[System: answers to your question card #{asked['qid']}]\nWhich DB? → SQLite",
            )
        ]


async def test_grant_is_refused_when_its_record_cannot_be_dropped(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowance fails closed: an undroppable grant record means asking again."""
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-fail-closed")
        assert await _request_bash(client, session_id, "pnpm lint") == "deny"
        (card,) = await _pending(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))

        def _store_down(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(SqlAlchemyConversationStore, "delete_detached_card", _store_down)
        assert await _request_bash(client, session_id, "pnpm lint") == "deny"


async def test_expired_undelivered_grant_restarts_as_lapse_notice(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """Restart never sends an obsolete instruction to re-run a granted call."""
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-expired-offline-grant")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        deliveries.hold = True
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(
            lambda: any(
                row.elicitation_id == card["elicitation_id"] and row.state == "answered"
                for row in store.list_detached_cards()
            )
        )

    store.update_detached_card(card["elicitation_id"], expires_at=int(time.time()) - 1)
    deliveries.hold = False
    async with _serve(db_uri, tmp_path) as client:
        await _wait_for(lambda: bool(deliveries.calls))
        assert len(deliveries.calls) == 1
        assert "expired before delivery" in deliveries.calls[0][1]
        assert "re-run Bash" not in deliveries.calls[0][1]
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_consumed_grant_cancels_owed_instruction(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    """Using the grant while delivery waits makes the old re-run notice obsolete."""
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-consumed-offline-grant")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        deliveries.hold = True
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(
            lambda: any(
                row.elicitation_id == card["elicitation_id"] and row.state == "answered"
                for row in store.list_detached_cards()
            )
        )
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        assert not _pending_record(store, card["elicitation_id"])
        deliveries.hold = False
        await asyncio.sleep(0.05)
        assert deliveries.calls == []

    async with _serve(db_uri, tmp_path):
        await asyncio.sleep(0.05)
        assert deliveries.calls == []


async def test_lapsed_live_grant_cannot_approve_reissued_hook(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lapse in the persisted record revokes its still-live memory grant."""

    armed = asyncio.Event()

    async def _offline(*_args: Any, **_kwargs: Any) -> bool:
        armed.set()
        return False

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _offline)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-live-grant-lapse")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await asyncio.wait_for(armed.wait(), timeout=5)
        store.update_detached_card(card["elicitation_id"], expires_at=int(time.time()) - 1)
        await _wait_for(
            lambda: any(
                row.elicitation_id == card["elicitation_id"] and row.grant_key is None
                for row in store.list_detached_cards()
            )
        )
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_old_lapse_preserves_newer_same_command_grant(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older card lapsing cannot revoke or consume a newer grant for that command."""

    old_armed = asyncio.Event()

    async def _offline(*_args: Any, **_kwargs: Any) -> bool:
        old_armed.set()
        return False

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _offline)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-newer-same-command-grant")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(
            lambda: any(
                row.elicitation_id == card["elicitation_id"] and row.state == "answered"
                for row in store.list_detached_cards()
            )
        )
        old = next(
            row
            for row in store.list_detached_cards()
            if row.elicitation_id == card["elicitation_id"]
        )
        # The answered record is written before its callback arms the grant.
        await asyncio.wait_for(old_armed.wait(), timeout=5)
        newer = replace(
            old, elicitation_id="elicit_newer_grant", expires_at=int(time.time()) + 300
        )
        store.add_detached_card(newer)
        assert newer.grant_key is not None and newer.verdict is not None
        approval_grants.put(
            session_id,
            tuple(newer.grant_key),
            ElicitationResult.model_validate(newer.verdict),
            300,
            source_id=newer.elicitation_id,
        )
        store.update_detached_card(old.elicitation_id, expires_at=int(time.time()) - 1)
        await _wait_for(
            lambda: any(
                row.elicitation_id == old.elicitation_id and row.grant_key is None
                for row in store.list_detached_cards()
            )
        )
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        assert _pending_record(store, old.elicitation_id)
        assert not _pending_record(store, newer.elicitation_id)


async def test_delayed_old_answer_cannot_overwrite_newer_approval(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent answers preserve the newer approval when the older one lapses."""
    old_read = asyncio.Event()
    release_old = asyncio.Event()
    newer_started = asyncio.Event()
    newer_armed = asyncio.Event()
    answering: dict[asyncio.Task[Any], str] = {}
    old_id = new_id = new_aid = ""
    to_thread = asyncio.to_thread
    callback = hooks_routes._detached_card_callback

    def _track_callback(card: Any, **kwargs: Any) -> Any:
        on_result = callback(card, **kwargs)

        async def _answer_started(result: ElicitationResult | None) -> None:
            task = asyncio.current_task()
            assert task is not None
            answering[task] = card.elicitation_id
            if card.elicitation_id == new_id:
                newer_started.set()
            try:
                await on_result(result)
            finally:
                answering.pop(task, None)

        return _answer_started

    async def _controlled_store_call(write: Any, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        source = answering.get(task) if task is not None else None
        if source is None:
            return await to_thread(write, *args, **kwargs)
        # Inline callback store calls make each answer run to its next gate;
        # thread scheduling cannot choose which answer arms first.
        result = write(*args, **kwargs)
        if source == old_id and write.__name__ == "list_detached_cards" and not old_read.is_set():
            old_read.set()
            await release_old.wait()
        return result

    async def _offline(_session_id: str, text: str, **_kwargs: Any) -> bool:
        if f"#{new_aid} granted" in text:
            newer_armed.set()
        return False

    monkeypatch.setattr(hooks_routes, "_detached_card_callback", _track_callback)
    monkeypatch.setattr(asyncio, "to_thread", _controlled_store_call)
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _offline)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-concurrent-same-command-answers")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (old,) = await _pending(client, session_id)
        old_id = old["elicitation_id"]
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        new = next(card for card in await _pending(client, session_id) if card != old)
        new_id, new_aid = new["elicitation_id"], new["params"]["approval_ref"]
        await _answer(client, session_id, old_id, "accept")
        await asyncio.wait_for(old_read.wait(), timeout=5)
        await _answer(client, session_id, new_id, "accept")
        await asyncio.wait_for(newer_started.wait(), timeout=5)
        store.update_detached_card(old_id, expires_at=int(time.time()) - 1)
        release_old.set()
        await asyncio.wait_for(newer_armed.wait(), timeout=5)
        await _wait_for(lambda: not _pending_record(store, old_id))
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        assert not _pending_record(store, old_id)
        assert not _pending_record(store, new_id)
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_failed_answer_write_does_not_delay_newer_answer(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retrying answer cannot keep a later acceptance out of durable storage."""
    failed = asyncio.Event()
    old_id = ""
    write_record = hooks_routes._write_card_record

    async def _fail_old_answer(write: Any, *args: Any, **kwargs: Any) -> bool:
        if args == (old_id,) and kwargs.get("state") == "answered":
            failed.set()
            return False
        return await write_record(write, *args, **kwargs)

    monkeypatch.setattr(hooks_routes, "_write_card_record", _fail_old_answer)
    deliveries.hold = True
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-independent-answer-writes")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (old,) = await _pending(client, session_id)
        old_id = old["elicitation_id"]
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        new = next(card for card in await _pending(client, session_id) if card != old)
        await _answer(client, session_id, old_id, "accept")
        await asyncio.wait_for(failed.wait(), timeout=5)
        await _answer(client, session_id, new["elicitation_id"], "accept")
        await _wait_for(
            lambda: any(
                row.elicitation_id == new["elicitation_id"] and row.state == "answered"
                for row in store.list_detached_cards()
            )
        )

    async with _serve(db_uri, tmp_path) as client:
        assert await _request_bash(client, session_id, "pnpm build") == "allow"


async def test_restart_uses_answer_order_for_same_command_grants(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older-created card approved last survives the earlier approval's lapse."""
    attempted: list[str] = []

    async def _offline(_session_id: str, text: str, **_kwargs: Any) -> bool:
        attempted.append(text)
        return False

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _offline)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-restart-answer-order")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"

        (first,) = await _pending(client, session_id)
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        second = next(card for card in await _pending(client, session_id) if card != first)
        for card in (second, first):
            await _answer(client, session_id, card["elicitation_id"], "accept")
            await _wait_for(
                lambda aid=card["params"]["approval_ref"]: any(
                    f"#{aid} granted" in text for text in attempted
                )
            )
        now = int(time.time())
        # Pin distinct creation and answer times without a wall-clock sleep.
        store.update_detached_card(
            first["elicitation_id"], created_at=now - 2, expires_at=now + 600
        )
        store.update_detached_card(
            second["elicitation_id"], created_at=now - 1, expires_at=now + 300
        )

    async with _serve(db_uri, tmp_path) as client:
        store.update_detached_card(second["elicitation_id"], expires_at=int(time.time()) - 1)
        assert not _pending_record(store, second["elicitation_id"])
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        assert not _pending_record(store, first["elicitation_id"])
        assert not _pending_record(store, second["elicitation_id"])
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def _two_approval_cards(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    for _ in range(2):
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
    return await _pending(client, session_id)


async def test_later_denial_supersedes_accepted_grant(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-newer-denial")
        old, new = await _two_approval_cards(client, session_id)
        for card, action in ((old, "accept"), (new, "decline")):
            await _answer(client, session_id, card["elicitation_id"], action)
            await _wait_for(
                lambda action=action: len(deliveries.calls) == (1 if action == "accept" else 2)
            )
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_consumed_newer_answer_cannot_restore_superseded_grant(
    runtime_init: None, db_uri: str, tmp_path: Path, deliveries: _Deliveries
) -> None:
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-supersession-restart")
        for index, card in enumerate(await _two_approval_cards(client, session_id), start=1):
            await _answer(client, session_id, card["elicitation_id"], "accept")
            await _wait_for(lambda index=index: len(deliveries.calls) == index)
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
    async with _serve(db_uri, tmp_path) as client:
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_answer_order_precedes_ancestor_cleanup(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_blocked, release_old, old_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_id = ""
    to_thread = asyncio.to_thread
    callback = hooks_routes._detached_card_callback

    async def _gate_cleanup(write: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            write is orchestration._publish_elicitation_resolved_to_ancestors
            and args[2] == old_id
            and asyncio.current_task() in orchestration._detached_elicitation_tasks
        ):
            old_blocked.set()
            await release_old.wait()
        return await to_thread(write, *args, **kwargs)

    def _track_callback(card: Any, **kwargs: Any) -> Any:
        on_result = callback(card, **kwargs)

        async def _tracked(result: ElicitationResult | None) -> None:
            try:
                await on_result(result)
            finally:
                if card.elicitation_id == old_id:
                    old_finished.set()

        return _tracked

    monkeypatch.setattr(asyncio, "to_thread", _gate_cleanup)
    monkeypatch.setattr(hooks_routes, "_detached_card_callback", _track_callback)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-answer-before-cleanup")
        old, new = await _two_approval_cards(client, session_id)
        old_id = old["elicitation_id"]
        await _answer(client, session_id, old_id, "accept")
        await asyncio.wait_for(old_blocked.wait(), 5)
        await _answer(client, session_id, new["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))
        assert await _request_bash(client, session_id, "pnpm build") == "allow"
        release_old.set()
        await asyncio.wait_for(old_finished.wait(), 5)
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_equal_clocks_preserve_latest_verdict(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    old_read, release_old, old_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_id = ""
    answering: set[asyncio.Task[Any]] = set()
    to_thread = asyncio.to_thread
    callback = hooks_routes._detached_card_callback

    def _track_callback(card: Any, **kwargs: Any) -> Any:
        on_result = callback(card, **kwargs)

        async def _tracked(result: ElicitationResult | None) -> None:
            task = asyncio.current_task()
            assert task is not None
            if card.elicitation_id == old_id:
                answering.add(task)
            try:
                await on_result(result)
            finally:
                answering.discard(task)
                if card.elicitation_id == old_id:
                    old_finished.set()

        return _tracked

    async def _gate_snapshot(write: Any, *args: Any, **kwargs: Any) -> Any:
        result = await to_thread(write, *args, **kwargs)
        if (
            asyncio.current_task() in answering
            and write.__name__ == "list_detached_cards"
            and not old_read.is_set()
        ):
            old_read.set()
            await release_old.wait()
        return result

    monkeypatch.setattr(
        hooks_routes,
        "time",
        SimpleNamespace(monotonic=lambda: 100.0, time=time.time, time_ns=lambda: 100_000_000_000),
    )
    monkeypatch.setattr(hooks_routes, "_detached_card_callback", _track_callback)
    monkeypatch.setattr(asyncio, "to_thread", _gate_snapshot)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-equal-answer-clocks")
        old, new = await _two_approval_cards(client, session_id)
        old_id = old["elicitation_id"]
        await _answer(client, session_id, old_id, "accept")
        await asyncio.wait_for(old_read.wait(), 5)
        await _answer(client, session_id, new["elicitation_id"], "accept", {"remember": True})
        await _wait_for(lambda: bool(deliveries.calls))
        release_old.set()
        await asyncio.wait_for(old_finished.wait(), 5)
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request", json=_bash_payload("pnpm build")
        )
        assert response.status_code == 200, response.text
        decision = response.json()["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        assert decision["updatedPermissions"][0]["behavior"] == "allow"
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_supersession_rollback_and_crash_before_memory_update(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Failed deletion rolls back the answer; committed answers survive a lost callback."""
    failed, retry, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    new_id = old_id = ""
    injected = False
    store = SqlAlchemyConversationStore(db_uri)
    write_record = hooks_routes._write_card_record

    def _fail_old_delete(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool
    ) -> None:
        nonlocal injected
        if (
            not injected
            and new_id
            and statement.startswith("DELETE FROM detached_cards")
            and old_id in str(parameters)
        ):
            injected = True
            raise RuntimeError("temporary supersession delete failure")

    async def _gate_answer(write: Any, *args: Any, **kwargs: Any) -> bool:
        ok = await write_record(write, *args, **kwargs)
        if args == (new_id,) and kwargs.get("state") == "answered":
            if not ok:
                failed.set()
                await retry.wait()
            else:
                committed.set()
                await asyncio.Event().wait()
        return ok

    # All store instances share this engine; fail the deletion inside the
    # same transaction that saves the new answer.
    event.listen(store._conv_engine, "before_cursor_execute", _fail_old_delete)
    monkeypatch.setattr(hooks_routes, "_write_card_record", _gate_answer)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    try:
        async with _serve(db_uri, tmp_path) as client:
            session_id = await _new_session(client, "test-supersession-crash")
            old, new = await _two_approval_cards(client, session_id)
            old_id, new_id = old["elicitation_id"], new["elicitation_id"]
            await _answer(client, session_id, old_id, "accept")
            await _wait_for(lambda: bool(deliveries.calls))
            await _answer(client, session_id, new_id, action)
            await asyncio.wait_for(failed.wait(), 5)
            rows = {row.elicitation_id: row for row in store.list_detached_cards()}
            assert rows[old_id].verdict["action"] == "accept"
            assert rows[new_id].state == "pending"
            assert await _request_bash(client, session_id, "pnpm build") == "deny"
            retry.set()
            await asyncio.wait_for(committed.wait(), 5)
            rows = {row.elicitation_id: row for row in store.list_detached_cards()}
            assert old_id not in rows
            assert rows[new_id].verdict["action"] == action
            assert await _request_bash(client, session_id, "pnpm build") == "deny"
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", _fail_old_delete)

    async with _serve(db_uri, tmp_path) as client:
        assert await _request_bash(client, session_id, "pnpm build") == (
            "allow" if action == "accept" else "deny"
        )
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


async def test_answer_stamp_precedes_answer_history_append(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An acceptance stalled saving its answer cannot supersede a later denial."""
    blocked, resume, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_id = ""
    to_thread = asyncio.to_thread
    callback = hooks_routes._detached_card_callback

    async def _stall_append(write: Any, *args: Any, **kwargs: Any) -> Any:
        if write.__name__ == "append" and old_id and old_id in str(args):
            blocked.set()
            await resume.wait()
        return await to_thread(write, *args, **kwargs)

    def _track_callback(card: Any, **kwargs: Any) -> Any:
        on_result = callback(card, **kwargs)

        async def _tracked(result: ElicitationResult | None) -> None:
            try:
                await on_result(result)
            finally:
                if card.elicitation_id == old_id:
                    finished.set()

        return _tracked

    monkeypatch.setattr(asyncio, "to_thread", _stall_append)
    monkeypatch.setattr(hooks_routes, "_detached_card_callback", _track_callback)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-answer-before-history")
        old, new = await _two_approval_cards(client, session_id)
        old_id = old["elicitation_id"]
        old_answer = asyncio.create_task(
            _answer(client, session_id, old_id, "accept", {"remember": True})
        )
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            await _answer(client, session_id, new["elicitation_id"], "decline")
            await _wait_for(lambda: bool(deliveries.calls))
        finally:
            resume.set()
        await asyncio.wait_for(old_answer, 5)
        await asyncio.wait_for(finished.wait(), 5)
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


@pytest.mark.parametrize("deletion", ["committed", "superseded", "failed"])
async def test_grant_consumption_uses_committed_delete_cutoff(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    monkeypatch: pytest.MonkeyPatch,
    deletion: str,
) -> None:
    """A committed consumption wins; failed or superseded sources cannot allow."""
    deleting, resume, denied = asyncio.Event(), asyncio.Event(), asyncio.Event()
    new_id = ""
    to_thread = asyncio.to_thread
    callback = hooks_routes._detached_card_callback

    def _track_callback(card: Any, **kwargs: Any) -> Any:
        on_result = callback(card, **kwargs)

        async def _tracked(result: ElicitationResult | None) -> None:
            try:
                await on_result(result)
            finally:
                if card.elicitation_id == new_id:
                    denied.set()

        return _tracked

    async def _gate_delete(write: Any, *args: Any, **kwargs: Any) -> Any:
        if write.__name__ != "delete_detached_card" or "expected_verdict" not in kwargs:
            return await to_thread(write, *args, **kwargs)
        if deletion == "committed":
            result = await to_thread(write, *args, **kwargs)
            assert result is True
            deleting.set()
            await resume.wait()
            return result
        deleting.set()
        await resume.wait()
        if deletion == "failed":
            raise RuntimeError("temporary consumption delete failure")
        return await to_thread(write, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _gate_delete)
    monkeypatch.setattr(hooks_routes, "_detached_card_callback", _track_callback)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-consumption-cutoff")
        old, new = await _two_approval_cards(client, session_id)
        new_id = new["elicitation_id"]
        await _answer(client, session_id, old["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))
        consuming = asyncio.create_task(_request_bash(client, session_id, "pnpm build"))
        try:
            await asyncio.wait_for(deleting.wait(), 5)
            if deletion != "failed":
                await _answer(client, session_id, new_id, "decline")
                await asyncio.wait_for(denied.wait(), 5)
        finally:
            resume.set()
        assert await asyncio.wait_for(consuming, 5) == (
            "allow" if deletion == "committed" else "deny"
        )
        assert await _request_bash(client, session_id, "pnpm build") == "deny"


@pytest.mark.parametrize("legacy", ["single", "same_second", "different_second"])
async def test_legacy_unstamped_grant_recovery(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    deliveries: _Deliveries,
    legacy: str,
) -> None:
    """Unknown legacy answer order asks again; one identifiable grant works once."""
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-legacy-answer-order")
        card, unanswered = await _two_approval_cards(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(lambda: bool(deliveries.calls))

    original = next(
        row for row in store.list_detached_cards() if row.elicitation_id == card["elicitation_id"]
    )
    assert original.verdict is not None
    verdict = {key: value for key, value in original.verdict.items() if key != "answer_stamp"}
    expiry = int(time.time()) + 300
    store.update_detached_card(original.elicitation_id, verdict=verdict, expires_at=expiry)
    if legacy != "single":
        store.add_detached_card(
            replace(
                original,
                elicitation_id="elicit_legacy_duplicate",
                verdict=verdict,
                expires_at=expiry if legacy == "same_second" else expiry + 1,
            )
        )

    async with _serve(db_uri, tmp_path) as client:
        assert unanswered["elicitation_id"] in {
            card["elicitation_id"] for card in await _pending(client, session_id)
        }
        assert await _request_bash(client, session_id, "pnpm build") == (
            "deny" if legacy == "same_second" else "allow"
        )
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        if legacy == "same_second":
            assert await _pending(client, session_id)
            assert not _pending_record(store, original.elicitation_id)
            assert not _pending_record(store, "elicit_legacy_duplicate")
