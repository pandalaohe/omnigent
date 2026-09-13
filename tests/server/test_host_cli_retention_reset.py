"""Regression tests for the Host CLI-retention reset path (fixes 1-3)."""

import asyncio
from types import SimpleNamespace

import pytest

from omnigent.entities.pagination import PagedList
from omnigent.server.cli_retention import CliRetentionCoordinator


def _conv(conversation_id: str, runner_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation_id,
        runner_id=runner_id if runner_id is not None else f"runner-{conversation_id}",
        host_id="host-a",
    )


class _PagingStore:
    """In-memory conversation store honouring limit/after like the real one.

    When ``after`` names a deleted id it mimics the upstream cursor-subquery
    behaviour: an empty page with ``has_more=False``.
    """

    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)
        self.list_calls = 0
        self.get_calls: list[str] = []

    def list_conversations(self, **kwargs):
        self.list_calls += 1
        limit = kwargs.get("limit", 500)
        after = kwargs.get("after")
        ids = list(self._ids)
        if after is not None and after not in ids:
            return PagedList(data=[], first_id=None, last_id=None, has_more=False)
        start = ids.index(after) + 1 if after is not None else 0
        window = ids[start:]
        page_ids = window[:limit]
        data = [_conv(conversation_id) for conversation_id in page_ids]
        return PagedList(
            data=data,
            first_id=page_ids[0] if page_ids else None,
            last_id=page_ids[-1] if page_ids else None,
            has_more=len(window) > len(page_ids),
        )

    def get_conversation(self, conversation_id: str):
        self.get_calls.append(conversation_id)
        if conversation_id in self._ids:
            return _conv(conversation_id)
        return None


class _OkRouter:
    class _Client:
        async def post(self, url, *, json, timeout):
            del url, json, timeout
            return SimpleNamespace(status_code=200)

    def client_for_session_resources(self, session_id, *, conversation):
        return SimpleNamespace(client=self._Client(), runner_id=conversation.runner_id)


def _coordinator(store) -> CliRetentionCoordinator:
    return CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=store,
        runner_router=_OkRouter(),
    )


@pytest.mark.asyncio
async def test_host_enumeration_recovers_when_cursor_row_deleted_between_pages() -> None:
    # Enough rows to force more than one 500-row page.
    ids = [f"c-{index:04d}" for index in range(505)]
    store = _PagingStore(ids)
    coordinator = _coordinator(store)

    original_list = store.list_conversations

    def _deleting_list(**kwargs):
        # Simulate the cursor row being deleted after the first page is read:
        # the next page request names a cursor that no longer exists.
        if kwargs.get("after") == "c-0499" and "c-0499" in store._ids:
            store._ids.remove("c-0499")
        return original_list(**kwargs)

    store.list_conversations = _deleting_list  # type: ignore[method-assign]

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["incomplete"] is False
    # c-0499 was genuinely observed on the first page before its deletion, so
    # it stays in the recovered set; the tail past it is re-enumerated.
    assert sorted(result["reset"]) == sorted(ids)
    # The healthy path issues no existence check; only the anomalous empty
    # page triggers the single get_conversation call.
    assert store.get_calls == ["c-0499"]


@pytest.mark.asyncio
async def test_host_enumeration_without_anomaly_issues_no_existence_check() -> None:
    store = _PagingStore(["c-1", "c-2", "c-3"])
    coordinator = _coordinator(store)

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["incomplete"] is False
    assert sorted(result["reset"]) == ["c-1", "c-2", "c-3"]
    assert store.get_calls == []


@pytest.mark.asyncio
async def test_host_enumeration_truncated_past_bound_reports_incomplete() -> None:
    store = _PagingStore(["c-1", "c-2"])

    def _always_truncated(**kwargs):
        if kwargs.get("after") is not None:
            store.list_calls += 1
            return PagedList(data=[], first_id=None, last_id=None, has_more=False)
        # First page claims more rows remain so the loop must request page 2,
        # which always comes back empty with the cursor row gone.
        store.list_calls += 1
        return PagedList(
            data=[_conv("c-1"), _conv("c-2")],
            first_id="c-1",
            last_id="c-2",
            has_more=True,
        )

    store.list_conversations = _always_truncated  # type: ignore[method-assign]
    store.get_conversation = lambda conversation_id: store.get_calls.append(  # type: ignore[method-assign]
        conversation_id
    ) or None
    coordinator = _coordinator(store)

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["incomplete"] is True
    assert sorted(result["reset"]) == ["c-1", "c-2"]
    # Bounded: 3 passes x (first page + truncated empty page), plus one
    # existence check per truncated pass. No exception escapes.
    assert store.list_calls == 6
    assert len(store.get_calls) == 3
