"""Regression tests for the Host CLI-retention reset path (fixes 1-3)."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from omnigent.entities.pagination import PagedList
from omnigent.server import cli_retention as cli_retention_module
from omnigent.server.cli_retention import (
    CliRetentionCoordinator,
    CliRetentionHostLeaseLost,
)
from omnigent.server.routes.hosts import _reset_completion_status


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
    # it stays in the recovered set; the tail past it is re-enumerated. The
    # id list is a bounded sample; the count carries the full set.
    assert result["reset_count"] == len(ids)
    assert len(result["reset"]) == cli_retention_module._RESET_ID_SAMPLE_CAP
    assert result["unavailable"] == []
    assert result["unbound_count"] == 0
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
    store.get_conversation = lambda conversation_id: (
        store.get_calls.append(  # type: ignore[method-assign]
            conversation_id
        )
        or None
    )
    coordinator = _coordinator(store)

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["incomplete"] is True
    assert sorted(result["reset"]) == ["c-1", "c-2"]
    # Bounded: 3 passes x (first page + truncated empty page), plus one
    # existence check per truncated pass. No exception escapes.
    assert store.list_calls == 6
    assert len(store.get_calls) == 3


class _SinglePageStore:
    """One complete page plus a get_conversation that always hits."""

    def __init__(self, conversations: list[SimpleNamespace]) -> None:
        self._conversations = list(conversations)

    def list_conversations(self, **kwargs):
        data = self._conversations
        return PagedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=False,
        )

    def get_conversation(self, conversation_id: str):
        for conversation in self._conversations:
            if conversation.id == conversation_id:
                return conversation
        return None


@pytest.mark.asyncio
async def test_reset_pass_over_deadline_reports_unattempted_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_retention_module, "_RESET_OVERALL_DEADLINE_S", 0.05)
    ids = [f"s-{index:02d}" for index in range(20)]
    store = _SinglePageStore([_conv(conversation_id) for conversation_id in ids])

    class _SlowClient:
        def __init__(self) -> None:
            self.started: list[str] = []

        async def post(self, url, *, json, timeout):
            del json, timeout
            self.started.append(url)
            await asyncio.sleep(0.5)
            return SimpleNamespace(status_code=200)

    clients = {conversation_id: _SlowClient() for conversation_id in ids}

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(client=clients[session_id], runner_id=conversation.runner_id)

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=store,
        runner_router=_Router(),
    )

    started_at = time.monotonic()
    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)
    elapsed = time.monotonic() - started_at

    # Only the first concurrency window gets issued before the deadline; the
    # rest are never attempted — reported as such, not as reset, not as failed.
    assert result["reset"] == ids[: cli_retention_module._RESET_MAX_CONCURRENCY]
    assert result["unavailable"] == []
    assert result["not_attempted"] == ids[cli_retention_module._RESET_MAX_CONCURRENCY :]
    assert set(result["reset"]) | set(result["not_attempted"]) == set(ids)
    # Prompt: in-flight requests settle but nothing new is issued.
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_reset_pass_isolation_one_failure_does_not_abort_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_retention_module, "_RESET_OVERALL_DEADLINE_S", 60.0)
    ids = ["ok-1", "boom", "ok-2"]
    store = _SinglePageStore([_conv(conversation_id) for conversation_id in ids])

    class _Client:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

        async def post(self, url, *, json, timeout):
            del url, json, timeout
            if self.session_id == "boom":
                raise RuntimeError("runner exploded")
            return SimpleNamespace(status_code=200)

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(client=_Client(session_id), runner_id=conversation.runner_id)

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=store,
        runner_router=_Router(),
    )

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["reset"] == ["ok-1", "ok-2"]
    assert result["unavailable"] == ["boom"]
    assert result["not_attempted"] == []


@pytest.mark.asyncio
async def test_reset_pass_keeps_lease_fencing_before_external_commands() -> None:
    store = _SinglePageStore([_conv("guarded")])
    posts: list[str] = []

    class _Client:
        async def post(self, url, *, json, timeout):
            del json, timeout
            posts.append(url)
            return SimpleNamespace(status_code=200)

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(client=_Client(), runner_id=conversation.runner_id)

    calls = 0

    class _LosingLease:
        async def ensure_owned(self) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise CliRetentionHostLeaseLost("host-a")

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=store,
        runner_router=_Router(),
    )

    result = await coordinator.reset_host_under_lease(
        "host-a", policy_revision=8, lease=_LosingLease()
    )

    # The pre-enumeration check owns the lease; the in-loop check loses it, so
    # no POST is ever issued and the conversation keeps failed accounting.
    assert calls == 2
    assert posts == []
    assert result["reset"] == []
    assert result["unavailable"] == ["guarded"]
    assert result["not_attempted"] == []


@pytest.mark.asyncio
async def test_reset_with_mostly_runnerless_host_returns_counts_not_id_lists() -> None:
    historical = [f"hist-{index:04d}" for index in range(300)]
    conversations = [_conv("live-ok")] + [
        SimpleNamespace(id=conversation_id, runner_id=None, host_id="host-a")
        for conversation_id in historical
    ]
    store = _SinglePageStore(conversations)

    class _Client:
        async def post(self, url, *, json, timeout):
            del url, json, timeout
            return SimpleNamespace(status_code=200)

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(client=_Client(), runner_id=conversation.runner_id)

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=store,
        runner_router=_Router(),
    )

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["reset"] == ["live-ok"]
    assert result["reset_count"] == 1
    assert result["unbound_count"] == 300
    assert len(result["unbound_sample"]) == cli_retention_module._RESET_ID_SAMPLE_CAP
    assert result["unavailable"] == []
    assert result["not_attempted"] == []
    # The runner-less population alone must not force partial/pending.
    assert _reset_completion_status(result) == "legacy"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # Runner-bound failure is a real incompleteness.
        (
            {
                "reset": ["ok"],
                "reset_count": 1,
                "unavailable": ["bad"],
                "unavailable_count": 1,
                "unbound_count": 0,
                "not_attempted": [],
                "not_attempted_count": 0,
                "incomplete": False,
            },
            "partial",
        ),
        (
            {
                "reset": [],
                "reset_count": 0,
                "unavailable": ["bad"],
                "unavailable_count": 1,
                "unbound_count": 0,
                "not_attempted": [],
                "not_attempted_count": 0,
                "incomplete": False,
            },
            "pending",
        ),
        # Never attempted is distinct from failed and also incomplete.
        (
            {
                "reset": ["ok"],
                "reset_count": 1,
                "unavailable": [],
                "unavailable_count": 0,
                "unbound_count": 0,
                "not_attempted": ["slow"],
                "not_attempted_count": 1,
                "incomplete": False,
            },
            "partial",
        ),
        # A truncated enumeration never reports a completed reset.
        (
            {
                "reset": ["ok"],
                "reset_count": 1,
                "unavailable": [],
                "unavailable_count": 0,
                "unbound_count": 5000,
                "not_attempted": [],
                "not_attempted_count": 0,
                "incomplete": True,
            },
            "partial",
        ),
        (
            {
                "reset": [],
                "reset_count": 0,
                "unavailable": [],
                "unavailable_count": 0,
                "unbound_count": 0,
                "not_attempted": [],
                "not_attempted_count": 0,
                "incomplete": True,
            },
            "pending",
        ),
        # Legacy snapshots without count keys keep the pre-split meaning.
        ({"reset": ["ok"], "unavailable": ["old"]}, "partial"),
        ({"reset": [], "unavailable": ["old"]}, "pending"),
        ({"reset": ["ok"], "unavailable": []}, "legacy"),
        ({"reset": [], "unavailable": []}, "legacy"),
        ({"reset": None, "unavailable": []}, "pending"),
    ],
)
def test_reset_completion_status_mapping(result: dict, expected: str) -> None:
    assert _reset_completion_status(result) == expected
