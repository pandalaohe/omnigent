"""Session writes tolerate short admission contention without hiding other conflicts."""

from unittest.mock import Mock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


async def test_admission_retry_preserves_arguments_and_result() -> None:
    result = object()
    write = Mock(
        side_effect=[
            OmnigentError("Host admission lease is busy", code=ErrorCode.CONFLICT),
            result,
        ]
    )
    assert (
        await orchestration._retry_host_admission_lease(
            write, "session", archived=True, interval_s=0
        )
        is result
    )
    assert write.call_count == 2
    write.assert_called_with("session", archived=True)


@pytest.mark.parametrize(
    ("code", "message", "timeout_s", "attempts"),
    [
        (ErrorCode.CONFLICT, "Host admission lease is busy", 0, 1),
        (ErrorCode.CONFLICT, "Host admission lease is busy", 2, 2),
        (ErrorCode.CONFLICT, "Worktree was removed during admission", 5, 1),
        (ErrorCode.FORBIDDEN, "Host admission lease is busy", 5, 1),
    ],
)
async def test_admission_retry_surfaces_terminal_errors(
    monkeypatch: pytest.MonkeyPatch,
    code: ErrorCode,
    message: str,
    timeout_s: float,
    attempts: int,
) -> None:
    clock = iter([0.0, 1.0, 2.0])
    # Patch the module reference, leaving asyncio's own clock untouched.
    monkeypatch.setattr(orchestration, "time", Mock(monotonic=lambda: next(clock)))
    error = OmnigentError(message, code=code)
    write = Mock(side_effect=error)
    with pytest.raises(OmnigentError) as caught:
        await orchestration._retry_host_admission_lease(write, timeout_s=timeout_s, interval_s=0)
    assert caught.value is error
    assert write.call_count == attempts


@pytest.mark.parametrize("archived", [True, False])
async def test_archive_route_retries_admission_before_mutation(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    session = await create_test_session(client, name="admission-archive")
    if not archived:
        response = await client.patch(f"/v1/sessions/{session['id']}", json={"archived": True})
        assert response.status_code == 200, response.text
    original = SqlAlchemyConversationStore.update_conversation_with_changes
    attempts = 0

    def update(store, conversation_id, **kwargs):
        nonlocal attempts
        if conversation_id == session["id"] and kwargs.get("archived") is archived:
            attempts += 1
            if attempts == 1:
                raise OmnigentError("Host admission lease is busy", code=ErrorCode.CONFLICT)
        return original(store, conversation_id, **kwargs)

    monkeypatch.setattr(SqlAlchemyConversationStore, "update_conversation_with_changes", update)
    response = await client.patch(f"/v1/sessions/{session['id']}", json={"archived": archived})
    assert response.status_code == 200, response.text
    assert response.json()["archived"] is archived
    assert attempts == 2
