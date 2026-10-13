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

from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import _elicitation_registry
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_route
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

    async def _offline(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _offline)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    store = SqlAlchemyConversationStore(db_uri)
    async with _serve(db_uri, tmp_path) as client:
        session_id = await _new_session(client, "test-live-grant-lapse")
        assert await _request_bash(client, session_id, "pnpm build") == "deny"
        (card,) = await _pending(client, session_id)
        await _answer(client, session_id, card["elicitation_id"], "accept")
        await _wait_for(
            lambda: any(
                row.elicitation_id == card["elicitation_id"] and row.state == "answered"
                for row in store.list_detached_cards()
            )
        )
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

    async def _offline(*_args: Any, **_kwargs: Any) -> bool:
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
