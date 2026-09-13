"""Round-3 regression tests for gaps left by ``dabc95611``.

Each test FAILS on the pre-round-3 source and passes after the fix:

1. Rotation onto a host WITH a policy is still skipped and counted as
   rotated — and the new host row is actually consulted to prove it
   (the old code never looked the host up).
2. Rotation onto a policy-less host (``cli_retention_policy=None``, which
   ``_rotate_host_id`` copies verbatim) or onto a host id with no row at
   all is stranded: it is reset like an orphan and counted under the new
   ``stranded`` key instead of being skipped as rotated.
3. A conversation whose row vanished between the reconnect snapshot and
   the re-read is reset from the snapshot and counted under the new
   ``vanished`` key instead of being dropped silently.
4. (Route level, in ``tests/server/integration/test_hosts_api.py``:) an
   external ``Task.cancel()`` arriving after a fan-out child already set
   ``lease.lost`` via ``ensure_owned`` still propagates instead of being
   swallowed by the heartbeat-cancel handler.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.pagination import PagedList
from omnigent.server.cli_retention import CliRetentionCoordinator


def _conv(conversation_id: str, **attrs: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "id": conversation_id,
        "runner_id": f"runner-{conversation_id}",
        "host_id": "host-gone",
    }
    fields.update(attrs)
    return SimpleNamespace(**fields)


def _policy() -> SimpleNamespace:
    return SimpleNamespace(version=1, idle_threshold_minutes=60)


class _OkRouter:
    """Runner router that records reset POSTs and always succeeds."""

    def __init__(self, posts: list) -> None:
        self._posts = posts

    class _Client:
        def __init__(self, posts: list) -> None:
            self._posts = posts

        async def post(self, url: str, *, json: dict, timeout: float) -> SimpleNamespace:
            del timeout
            self._posts.append((url, json))
            return SimpleNamespace(status_code=200)

    def client_for_session_resources(
        self, session_id: str, *, conversation: SimpleNamespace
    ) -> SimpleNamespace:
        return SimpleNamespace(client=self._Client(self._posts), runner_id=conversation.runner_id)


def _round3_coordinator(
    posts: list,
    *,
    conv_rows: dict[str, SimpleNamespace | None],
    host_rows: dict[str, SimpleNamespace | None],
    seed_revision: int = 7,
) -> tuple[CliRetentionCoordinator, list[str]]:
    """Coordinator with a scripted re-read view and scripted host rows."""
    host_lookups: list[str] = []

    class _HostStore:
        def get_host(self, host_id: str) -> SimpleNamespace | None:
            host_lookups.append(host_id)
            return host_rows.get(host_id)

    class _ConversationStore:
        def list_conversations(self, **kwargs: object) -> PagedList:
            return PagedList(data=[])

        def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
            return conv_rows.get(conversation_id)

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=_OkRouter(posts),
    )
    coordinator._last_results[(current_workspace_id(), "host-gone")] = {
        "configured": True,
        "policy_revision": seed_revision,
    }
    return coordinator, host_lookups


@pytest.mark.asyncio
async def test_release_after_delete_rotation_with_live_policy_stays_rotated() -> None:
    """A rebound session governed by a live policy is not reset."""
    posts: list = []
    coordinator, host_lookups = _round3_coordinator(
        posts,
        conv_rows={"sess-1": _conv("sess-1", host_id="host-new")},
        host_rows={
            "host-new": SimpleNamespace(host_id="host-new", cli_retention_policy=_policy())
        },
    )

    result = await coordinator.release_host_after_delete(
        "host-gone", conversations=[_conv("sess-1")]
    )

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 0
    assert result["rotated_count"] == 1
    assert result["stranded_count"] == 0
    assert result["vanished_count"] == 0
    assert posts == []
    # The skip is earned by consulting the new host row, not assumed.
    assert host_lookups == ["host-new"]


@pytest.mark.asyncio
async def test_release_after_delete_rotation_without_policy_is_stranded() -> None:
    """Policy-less and missing new hosts are reset, not skipped as rotated."""
    posts: list = []
    coordinator, host_lookups = _round3_coordinator(
        posts,
        conv_rows={
            "sess-1": _conv("sess-1", host_id="host-bare"),
            "sess-2": _conv("sess-2", host_id="host-bare"),
            "sess-3": _conv("sess-3", host_id="host-missing"),
        },
        host_rows={
            "host-bare": SimpleNamespace(host_id="host-bare", cli_retention_policy=None),
        },
    )
    stale = [_conv("sess-1"), _conv("sess-2"), _conv("sess-3")]

    result = await coordinator.release_host_after_delete("host-gone", conversations=stale)

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 3
    assert result["rotated_count"] == 0
    assert result["stranded_count"] == 3
    assert sorted(result["stranded"]) == ["sess-1", "sess-2", "sess-3"]
    assert [(url, payload) for url, payload in posts] == [
        (f"/v1/sessions/{sid}/cli-retention/reset", {"host_id": "host-gone", "policy_revision": 7})
        for sid in ("sess-1", "sess-2", "sess-3")
    ]
    # One lookup per distinct new host, however many sessions point at it.
    assert sorted(host_lookups) == ["host-bare", "host-missing"]


@pytest.mark.asyncio
async def test_release_after_delete_mixed_batch_reports_every_outcome() -> None:
    """Orphan, rotated, stranded, and vanished rows are each counted once."""
    posts: list = []
    coordinator, _host_lookups = _round3_coordinator(
        posts,
        conv_rows={
            "sess-orphan": _conv("sess-orphan", host_id=None),
            "sess-rotated": _conv("sess-rotated", host_id="host-live"),
            "sess-stranded": _conv("sess-stranded", host_id="host-bare"),
            # sess-vanished has no row: deleted between snapshot and re-read.
        },
        host_rows={
            "host-live": SimpleNamespace(host_id="host-live", cli_retention_policy=_policy()),
            "host-bare": SimpleNamespace(host_id="host-bare", cli_retention_policy=None),
        },
    )
    stale = [
        _conv("sess-orphan"),
        _conv("sess-rotated"),
        _conv("sess-stranded"),
        _conv("sess-vanished"),
    ]

    result = await coordinator.release_host_after_delete("host-gone", conversations=stale)

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 3
    assert result["rotated_count"] == 1
    assert result["rotated"] == ["sess-rotated"]
    assert result["stranded_count"] == 1
    assert result["stranded"] == ["sess-stranded"]
    assert result["vanished_count"] == 1
    assert result["vanished"] == ["sess-vanished"]
    assert sorted(url for url, _payload in posts) == [
        "/v1/sessions/sess-orphan/cli-retention/reset",
        "/v1/sessions/sess-stranded/cli-retention/reset",
        "/v1/sessions/sess-vanished/cli-retention/reset",
    ]
