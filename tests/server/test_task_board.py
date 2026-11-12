"""Exercise the report boundary against the actual store, not an in-memory substitute."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from omnigent.db.db_models import SqlTaskBoardItem, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.task_board import create_task_board_router
from omnigent.server.task_board.models import ReportError, TaskBoardLimits, parse_report, utc_text
from omnigent.stores.task_board_store import ItemQuery, TaskBoardStore

STAMP = "2026-11-06T06:30:00Z"
NOW = datetime(2026, 11, 6, 7, tzinfo=UTC)


def task(uid="eng-1", **changes):
    return {"@type": "Task", "uid": uid, "updated": STAMP, "title": "Retry uploads", **changes}


def group(*tasks, **changes):
    return {
        "@type": "Group",
        "uid": "example-feed",
        "updated": STAMP,
        "entries": list(tasks),
        **changes,
    }


def parse(value):
    return parse_report(
        json.dumps(value).encode(), "application/jscalendar+json", TaskBoardLimits()
    )


@pytest.fixture
def store(db_uri):
    return TaskBoardStore(db_uri)


@pytest.fixture
async def board_client(store):
    app = FastAPI()
    app.include_router(
        create_task_board_router(
            store,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"X-Forwarded-Email": "owner@example.com"},
    ) as client:
        yield client


async def test_report_read_replay_and_owner_isolation(board_client, store, db_uri):
    locations = {"room": {"@type": "Location", "name": "Workshop"}}
    body = group(
        task(locations=locations, **{"omnigent.ai:group": {"id": "v2", "name": "Version two"}}),
        task("eng-2", progress="completed", progressUpdated=STAMP),
        task("eng-3", participants={"p1": {"@type": "Participant", "roles": {"owner": True}}}),
        title="Example tracker",
        source="https://tracker.example.com",
    )
    response = await board_client.put("/v1/me/tasks/sources/example", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "applied": 3,
        "unchanged": 0,
        "held": [],
        "returned": 0,
        "gone": 0,
        "complete": None,
        "history_added": 0,
    }
    read = (await board_client.get("/v1/me/tasks")).json()
    assert read["@type"] == "Group" and read["uid"] == "omnigent:tasks" and read["updated"]
    entries = {entry["uid"]: entry for entry in read["entries"]}
    assert entries["eng-1"]["locations"] == locations
    assert entries["eng-1"]["omnigent.ai:source"] == "example"
    assert entries["eng-1"]["progress"] == "needs-action"
    assert "percentComplete" not in entries["eng-1"]
    sources = (await board_client.get("/v1/me/tasks/sources")).json()["sources"]
    assert sources[0]["name"] == "Example tracker"
    assert sources[0]["counts"] == {"open": 2, "closed": 1, "gone": 0}
    with get_or_create_engine(db_uri).connect() as connection:
        before = connection.execute(
            select(SqlTaskBoardItem.uid, SqlTaskBoardItem.changed_at)
        ).all()
    replay = (await board_client.put("/v1/me/tasks/sources/example", json=body)).json()
    assert replay["applied"] == 0 and replay["unchanged"] == 3
    with get_or_create_engine(db_uri).connect() as connection:
        assert (
            connection.execute(select(SqlTaskBoardItem.uid, SqlTaskBoardItem.changed_at)).all()
            == before
        )
    assert store.list_items("other@example.com", ItemQuery())[0] == []
    with workspace_scope(37):
        assert store.list_sources("owner@example.com") == []
    assert (
        await board_client.get("/v1/me/tasks", headers={"X-Forwarded-Email": "other@example.com"})
    ).json()["entries"] == []


def test_ordering_whole_body_replacement_and_history_for_every_outcome(store):
    store.write("owner", "example", parse(task(percentComplete=10)), NOW)
    old = task(updated="2026-11-05T06:30:00Z", title="Old title")
    outcome = store.write("owner", "example", parse(old), NOW + timedelta(seconds=1))
    assert outcome.held == [{"uid": "eng-1", "reason": "older"}]
    assert store.list_items("owner", ItemQuery())[0][0]["title"] == "Retry uploads"
    replacement = task(sequence=1)
    assert store.write("owner", "example", parse(replacement), NOW).applied == 1
    assert "percentComplete" not in store.list_items("owner", ItemQuery())[0][0]
    same_version = task(sequence=1, title="Conflicting title")
    assert store.write("owner", "example", parse(same_version), NOW).held
    for index, report in enumerate((task(sequence=2), task(sequence=2), old)):
        history = {"id": f"h-{index}", "time": STAMP, "kind": "comment", "note": "Review"}
        report["omnigent.ai:history"] = [history, history]
        outcome = store.write(
            "owner", "example", parse(report), NOW + timedelta(minutes=index + 1)
        )
        assert outcome.history_added == 1
        assert [outcome.applied, outcome.unchanged, len(outcome.held)][index] == 1
        assert store.write("owner", "example", parse(report), NOW).history_added == 0
    item = store.list_items("owner", ItemQuery(include_history=True))[0][0]
    assert [entry["id"] for entry in item["omnigent.ai:history"]] == ["h-0", "h-1", "h-2"]


@pytest.mark.parametrize("body_outcome", ["unchanged", "held"])
def test_presence_clears_gone_even_without_a_body_replacement(store, db_uri, body_outcome):
    store.write("owner", "example", parse(task()), NOW)
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(update(SqlTaskBoardItem).values(gone_at=NOW.timestamp()))
    report = task() if body_outcome == "unchanged" else task(updated="2026-11-05T06:30:00Z")
    outcome = store.write("owner", "example", parse(report), NOW + timedelta(minutes=1))
    assert outcome.returned == 1
    assert outcome.unchanged == 1 if body_outcome == "unchanged" else len(outcome.held) == 1
    assert len(store.list_items("owner", ItemQuery())[0]) == 1


@pytest.mark.parametrize(
    ("body", "code", "path"),
    [
        ({"@type": "Group", "updated": STAMP, "entries": []}, "missing_property", "uid"),
        (group({"@type": "Task", "uid": "missing"}), "missing_property", "entries[0].updated"),
        (
            group(task(participants={"p1": {"@type": "Participant", "name": "Alice"}})),
            "missing_property",
            "entries[0].participants.p1.roles",
        ),
        (group(task(), task()), "duplicate_uid", "entries[1].uid"),
        (task(percentComplete=True), "bad_type", "percentComplete"),
        (task(updated="2026-11-06T06:30:00+00:00"), "bad_value", "updated"),
        (task(progress="blocked"), "bad_value", "progress"),
        (task(title="x" * 501), "too_long", "title"),
        (task(**{"omnigent.ai:gone": {"at": STAMP}}), "bad_value", "omnigent.ai:gone"),
    ],
)
def test_invalid_report_has_stable_first_error(body, code, path):
    with pytest.raises(ReportError) as error:
        parse(body)
    assert error.value.code == code
    assert error.value.path == path


async def test_rejected_report_writes_no_tasks_and_records_only_issue_metadata(board_client):
    body = group(task(), {"@type": "Task", "uid": "bad"})
    response = await board_client.put("/v1/me/tasks/sources/example", json=body)
    assert response.status_code == 422
    assert response.json()["path"] == "entries[1].updated"
    assert (await board_client.get("/v1/me/tasks")).json()["entries"] == []
    source = (await board_client.get("/v1/me/tasks/sources")).json()["sources"][0]
    assert source["last_write"] is None
    assert source["issues"][0]["code"] == "missing_property"
    assert source["issues"][0]["path"] == "entries[1].updated"
    assert "Retry uploads" not in json.dumps(source)
    assert (
        await board_client.put(
            "/v1/me/tasks/sources/example",
            content=json.dumps(task()),
            headers={"Content-Type": "text/plain"},
        )
    ).status_code == 415
    assert (
        await board_client.put(
            "/v1/me/tasks/sources/example",
            json=task(),
            headers={"Origin": "https://untrusted.example.com"},
        )
    ).status_code == 403


def test_filters_and_cursor_remain_bound_to_the_query(store):
    store.write(
        "owner",
        "example",
        parse(group(task("c"), task("a"), task("b", progress="completed", progressUpdated=STAMP))),
        NOW,
    )
    first, cursor = store.list_items("owner", ItemQuery(state="open", limit=1))
    assert [entry["uid"] for entry in first] == ["a"]
    second, end = store.list_items("owner", ItemQuery(state="open", limit=1, cursor=cursor))
    assert [entry["uid"] for entry in second] == ["c"] and end is None
    with pytest.raises(ReportError):
        store.list_items("owner", ItemQuery(state="closed", cursor=cursor))
    closed = store.list_items(
        "owner", ItemQuery(state="closed", closed_after=utc_text(NOW.timestamp() - 7200))
    )[0]
    assert [entry["uid"] for entry in closed] == ["b"]


@pytest.mark.parametrize(
    ("progresses", "expected"),
    [
        ([], "needs-action"),
        (["completed"], "completed"),
        (["failed", "completed"], "failed"),
        (["in-process", "needs-action"], "in-process"),
    ],
)
def test_rfc_participant_progress_default(store, progresses, expected):
    participants = {
        str(index): {"@type": "Participant", "roles": {"owner": True}, "progress": progress}
        for index, progress in enumerate(progresses)
    }
    store.write("owner", "example", parse(task(participants=participants)), NOW)
    assert store.list_items("owner", ItemQuery())[0][0]["progress"] == expected


def test_group_ignores_members_of_other_types():
    report = parse(
        group(
            {"@type": "Event", "title": "Calendar event"}, {"@type": "example.com:Other"}, task()
        )
    )
    assert [entry["uid"] for entry in report.tasks] == ["eng-1"]


def test_source_versions_preserve_sub_microsecond_precision(store):
    earlier = task(updated="2026-11-06T06:30:00.0000001Z", title="Earlier")
    later = task(updated="2026-11-06T06:30:00.0000002Z", title="Later")
    assert store.write("owner", "example", parse(earlier), NOW).applied == 1
    assert store.write("owner", "example", parse(later), NOW).applied == 1
    assert store.write("owner", "example", parse(earlier), NOW).held == [
        {"uid": "eng-1", "reason": "older"}
    ]
    assert store.list_items("owner", ItemQuery())[0][0]["title"] == "Later"


def test_created_order_compares_integer_and_fractional_seconds_exactly(store):
    body = group(
        task("a", created="2026-11-06T06:30:00.1Z"),
        task("b", created="2026-11-06T06:30:00Z"),
        task("c", created="2026-11-06T06:30:00.0000001Z"),
        task("d"),
    )
    store.write("owner", "example", parse(body), NOW)
    assert [entry["uid"] for entry in store.list_items("owner", ItemQuery())[0]] == [
        "d",
        "b",
        "c",
        "a",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"updated": "2026-11-06T06:30:00.000Z"},
        {"updated": "2026-11-06T06:30:00.10Z"},
        {"start": "2026-11-06T06:30:00.100"},
    ],
)
def test_noncanonical_fractional_times_are_rejected(changes):
    with pytest.raises(ReportError) as error:
        parse(task(**changes))
    assert error.value.code == "bad_value"


def test_generated_times_are_canonical():
    assert utc_text(0) == "1970-01-01T00:00:00Z"
    assert utc_text(0.1) == "1970-01-01T00:00:00.1Z"
    assert utc_text(0.000001) == "1970-01-01T00:00:00.000001Z"


@pytest.mark.parametrize(
    ("query", "code", "path"),
    [
        ("limit=0", "bad_value", "limit"),
        ("limit=invalid", "bad_type", "limit"),
        ("state=unknown", "bad_value", "state"),
        ("include_history=2", "bad_value", "include_history"),
        ("cursor=" + "x" * 4097, "too_long", "cursor"),
    ],
)
async def test_query_errors_use_the_report_error_contract(board_client, query, code, path):
    response = await board_client.get("/v1/me/tasks?" + query)
    assert response.status_code == 422
    assert response.json()["code"] == code and response.json()["path"] == path
    assert "detail" not in response.json()


def test_group_filters_preserve_case_and_trailing_spaces(store):
    store.write(
        "owner",
        "example",
        parse(
            group(*[task(uid, **{"omnigent.ai:group": {"id": uid}}) for uid in ("A", "a", "A ")])
        ),
        NOW,
    )
    for group_id in ("A", "a", "A "):
        assert [
            entry["uid"] for entry in store.list_items("owner", ItemQuery(group=group_id))[0]
        ] == [group_id]


def test_completeness_scope_versions_and_returned_bodies(store):
    entries = [task("open"), task("closed", progress="completed")]
    store.write("owner", "example", parse(group(*entries)), NOW)
    complete = {"omnigent.ai:complete": "open", "updated": "2026-11-06T06:30:00.0000002Z"}
    outcome = store.write("owner", "example", parse(group(**complete)), NOW)
    assert (outcome.gone, outcome.complete) == (1, "applied")
    assert [row["uid"] for row in store.list_items("owner", ItemQuery())[0]] == ["closed"]
    assert store.get_item("owner", "example", "open")["omnigent.ai:gone"]
    assert store.get_item("owner", "example", "open")["progress"] == "needs-action"
    older = {**complete, "omnigent.ai:complete": "all", "updated": "2026-11-06T06:30:00.0000001Z"}
    outcome = store.write("owner", "example", parse(group(*entries, **older)), NOW)
    assert (outcome.returned, outcome.gone, outcome.complete) == (1, 0, "skipped_older")
    assert store.write("owner", "example", parse(task("extra")), NOW).complete is None
    assert len(store.list_items("owner", ItemQuery())[0]) == 3
    outcome = store.write(
        "owner", "example", parse(group(**{**complete, "omnigent.ai:complete": "all"})), NOW
    )
    assert (outcome.gone, outcome.complete) == (3, "applied")
    assert store.list_items("owner", ItemQuery())[0] == []
    assert store.write("owner", "example", parse(group(**complete)), NOW).gone == 0
    assert len(store.list_items("owner", ItemQuery(include_gone=True))[0]) == 3


async def test_item_history_closed_seen_and_lazy_relations(board_client, store):
    history = [{"id": "new", "time": STAMP, "kind": "closed"}]
    relation = {"@type": "Relation", "relation": {"parent": True}}
    body = task(
        progress="completed",
        relatedTo={"target": relation, "missing": relation},
        **{"omnigent.ai:history": history},
    )
    store.write("owner@example.com", "example", parse(body), NOW)
    item_url = "/v1/me/tasks/item?source=example&uid=eng-1"
    item = (await board_client.get(item_url)).json()
    assert item["omnigent.ai:closedSeen"] == utc_text(NOW.timestamp())
    assert "progressUpdated" not in item
    assert item["omnigent.ai:history"] == history
    assert sorted(item["omnigent.ai:unresolved"]) == ["missing", "target"]
    store.write("other@example.com", "other", parse(task("target")), NOW)
    assert sorted((await board_client.get(item_url)).json()["omnigent.ai:unresolved"]) == [
        "missing",
        "target",
    ]
    store.write("owner@example.com", "other", parse(task("target")), NOW)
    assert (await board_client.get(item_url)).json()["omnigent.ai:unresolved"] == ["missing"]
    read = await board_client.get(
        "/v1/me/tasks", params={"closed_after": utc_text(NOW.timestamp())}
    )
    assert [entry["uid"] for entry in read.json()["entries"]] == ["eng-1"]
    assert (
        await board_client.get(item_url, headers={"X-Forwarded-Email": "other@example.com"})
    ).status_code == 404
    store.write(
        "owner@example.com",
        "example",
        parse(task(sequence=1, progress="completed")),
        NOW + timedelta(seconds=1),
    )
    assert (await board_client.get(item_url)).json()["omnigent.ai:closedSeen"] == utc_text(
        NOW.timestamp()
    )
    store.write(
        "owner@example.com", "example", parse(task(sequence=2)), NOW + timedelta(seconds=2)
    )
    assert "omnigent.ai:closedSeen" not in (await board_client.get(item_url)).json()


async def test_history_time_order_cursor_and_query_errors(board_client, store):
    for source in ("a", "b"):
        body = task(
            **{
                "omnigent.ai:history": [
                    {"id": "later", "time": "2026-11-06T06:30:00.0000002Z", "kind": "comment"},
                    {"id": "earlier", "time": "2026-11-06T06:30:00.0000001Z", "kind": "created"},
                ]
            }
        )
        store.write("owner@example.com", source, parse(body), NOW)
    params = {"limit": 1}
    found = []
    while True:
        response = await board_client.get("/v1/me/tasks/history", params=params)
        assert response.status_code == 200
        page = response.json()
        found.extend((entry["source"], entry["id"]) for entry in page["entries"])
        if "cursor" not in page:
            break
        params["cursor"] = page["cursor"]
    assert found == [("b", "later"), ("a", "later"), ("b", "earlier"), ("a", "earlier")]
    precise = (
        await board_client.get(
            "/v1/me/tasks/history",
            params={
                "after": "2026-11-06T06:30:00.0000001Z",
                "before": "2026-11-06T06:30:00.0000002Z",
            },
        )
    ).json()
    assert [(entry["source"], entry["id"]) for entry in precise["entries"]] == [
        ("b", "earlier"),
        ("a", "earlier"),
    ]
    assert (
        await board_client.get("/v1/me/tasks/history", params={**params, "source": "a"})
    ).status_code == 422
    filtered = (
        await board_client.get(
            "/v1/me/tasks/history",
            params={"source": "a", "after": STAMP, "before": "2026-11-07T00:00:00Z"},
        )
    ).json()
    assert len(filtered["entries"]) == 2
    assert (await board_client.get("/v1/me/tasks/history?before=invalid")).json()[
        "path"
    ] == "before"
    assert (await board_client.get("/v1/me/tasks?changed_after=invalid")).json()[
        "path"
    ] == "changed_after"
    for cursor in ("not-base64", "W10=", "e30=", "bnVsbA=="):
        assert (
            await board_client.get("/v1/me/tasks", params={"cursor": cursor})
        ).status_code == 422


async def test_closed_counts_offset_observed_time_and_gone(board_client, store):
    owner = "owner@example.com"
    store.write(
        owner,
        "example",
        parse(
            group(
                task("explicit", progress="completed", progressUpdated="2025-10-01T23:30:00Z"),
                task("observed", progress="failed"),
                task("open"),
            )
        ),
        NOW,
    )
    store.write(
        owner,
        "other",
        parse(task("cancelled", progress="cancelled", progressUpdated="2025-10-01T23:30:00Z")),
        NOW,
    )
    params = {"from": "2025-10-01T00:00:00Z", "to": "2025-10-03T00:00:00Z", "offset": 60}
    days = (await board_client.get("/v1/me/tasks/closed-counts", params=params)).json()["days"]
    assert days == [
        {"day": "2025-10-02", "source": "example", "closed": 1},
        {"day": "2025-10-02", "source": "other", "closed": 1},
    ]
    days = (
        await board_client.get("/v1/me/tasks/closed-counts", params={"source": "example"})
    ).json()["days"]
    assert days == [
        {"day": "2025-10-01", "source": "example", "closed": 1},
        {"day": "2026-11-06", "source": "example", "closed": 1},
    ]
    store.write(owner, "example", parse(group(**{"omnigent.ai:complete": "all"})), NOW)
    assert (
        await board_client.get("/v1/me/tasks/closed-counts", params={"source": "example"})
    ).json() == {"days": []}
    assert (await board_client.get("/v1/me/tasks/closed-counts?offset=1440")).status_code == 422


async def test_delete_source_is_owner_scoped_atomic_and_recreation_is_new(board_client, store):
    body = task(**{"omnigent.ai:history": [{"id": "h", "time": STAMP, "kind": "created"}]})
    for owner in ("owner@example.com", "other@example.com"):
        store.write(owner, "example", parse(body), NOW)
    created = (await board_client.get("/v1/me/tasks/sources")).json()["sources"][0]["created"]
    cached_sources = {"example": created}
    cache = {
        (item["omnigent.ai:source"], item["uid"]): item
        for item in (await board_client.get("/v1/me/tasks")).json()["entries"]
    }
    assert ("example", "eng-1") in cache
    assert (
        await board_client.delete(
            "/v1/me/tasks/sources/example", headers={"Origin": "https://untrusted.example.com"}
        )
    ).status_code == 403
    response = await board_client.delete("/v1/me/tasks/sources/example")
    assert response.status_code == 204 and response.content == b""
    assert (await board_client.get("/v1/me/tasks/sources")).json()["sources"] == []
    assert (await board_client.get("/v1/me/tasks/history")).json()["entries"] == []
    assert (
        await board_client.get("/v1/me/tasks/item?source=example&uid=eng-1")
    ).status_code == 404
    assert store.get_item("other@example.com", "example", "eng-1") is not None
    assert (await board_client.delete("/v1/me/tasks/sources/example")).status_code == 404
    store.write(
        "owner@example.com", "example", parse(task("replacement")), NOW + timedelta(seconds=1)
    )
    source = (await board_client.get("/v1/me/tasks/sources")).json()["sources"][0]
    assert source["created"] != created
    # Reader skipped the deletion interval; the source generation invalidates its cache.
    live_sources = {source["id"]: source["created"]}
    cache = {
        identity: item
        for identity, item in cache.items()
        if cached_sources.get(identity[0]) == live_sources.get(identity[0])
    }
    assert cache == {}
    for source_id, generation in live_sources.items():
        if cached_sources.get(source_id) != generation:
            page = (await board_client.get("/v1/me/tasks", params={"source": source_id})).json()
            cache.update(
                ((item["omnigent.ai:source"], item["uid"]), item) for item in page["entries"]
            )
    assert cache == {
        ("example", "replacement"): {
            **task("replacement"),
            "progress": "needs-action",
            "omnigent.ai:source": "example",
        }
    }


async def test_change_reader_converges_across_pages_and_concurrent_reports(board_client, store):
    owner = "owner@example.com"
    entries = [task(f"eng-{index:04}") for index in range(2500)]
    store.write(owner, "example", parse(group(*entries)), NOW)
    params = {"changed_after": STAMP, "limit": 1000}
    first = (await board_client.get("/v1/me/tasks", params=params)).json()
    assert len(first["entries"]) == 1000
    through = first["omnigent.ai:changedThrough"]
    cache = {item["uid"]: item for item in first["entries"]}
    changed = datetime.now(UTC)
    entries[0] = task("eng-0000", sequence=1, title="Changed during read")
    store.write(
        owner,
        "example",
        parse(group(*entries[:-20], task("new"), **{"omnigent.ai:complete": "open"})),
        changed,
    )
    page = first
    pages = 1
    while "omnigent.ai:cursor" in page:
        page = (
            await board_client.get(
                "/v1/me/tasks", params={**params, "cursor": page["omnigent.ai:cursor"]}
            )
        ).json()
        pages += 1
        cache.update((item["uid"], item) for item in page["entries"])
    assert pages == 3
    params = {
        "changed_after": utc_text(datetime.fromisoformat(through).timestamp() - 60),
        "limit": 7,
    }
    page = (await board_client.get("/v1/me/tasks", params=params)).json()
    tombstones = 0
    while True:
        for item in page["entries"]:
            if "omnigent.ai:gone" in item:
                cache.pop(item["uid"], None)
                tombstones += 1
            else:
                cache[item["uid"]] = item
        if "omnigent.ai:cursor" not in page:
            break
        page = (
            await board_client.get(
                "/v1/me/tasks", params={**params, "cursor": page["omnigent.ai:cursor"]}
            )
        ).json()
    assert tombstones == 20
    assert cache == {
        entry["uid"]: {
            **entry,
            "progress": "needs-action",
            "omnigent.ai:source": "example",
        }
        for entry in [*entries[:-20], task("new")]
    }


def test_history_only_and_returned_are_changes_for_every_body_outcome(store):
    for index, body in enumerate((task(sequence=1), task(sequence=1), task())):
        body["omnigent.ai:history"] = [{"id": str(index), "time": STAMP, "kind": "comment"}]
        when = NOW + timedelta(seconds=index)
        store.write("owner", "example", parse(body), when)
        entries, _ = store.list_items(
            "owner", ItemQuery(changed_after=utc_text(when.timestamp()), read_at=when.timestamp())
        )
        assert [entry["uid"] for entry in entries] == ["eng-1"]
    when = NOW + timedelta(minutes=1)
    store.write("owner", "example", parse(group(**{"omnigent.ai:complete": "all"})), when)
    store.write("owner", "example", parse(task()), when + timedelta(seconds=1))
    entries, _ = store.list_items(
        "owner",
        ItemQuery(changed_after=utc_text(when.timestamp() + 1), read_at=when.timestamp() + 1),
    )
    assert len(entries) == 1 and "omnigent.ai:gone" not in entries[0]


async def test_default_report_limits_never_truncate_or_partially_write(board_client):
    oversized = group(*(task(str(index)) for index in range(10_001)))
    response = await board_client.put("/v1/me/tasks/sources/entries", json=oversized)
    assert response.status_code == 413 and response.json()["code"] == "too_many_entries"
    response = await board_client.put(
        "/v1/me/tasks/sources/body",
        content=b" " * (8 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413 and response.json()["code"] == "body_too_large"
    assert (await board_client.get("/v1/me/tasks")).json()["entries"] == []
    for index in range(62):
        assert (
            await board_client.put(f"/v1/me/tasks/sources/source-{index}", json=task())
        ).status_code == 200
    response = await board_client.put("/v1/me/tasks/sources/extra", json=task())
    assert response.status_code == 409 and response.json()["code"] == "source_limit"
    sources = (await board_client.get("/v1/me/tasks/sources")).json()["sources"]
    assert len(sources) == 64 and "extra" not in {source["id"] for source in sources}
    for source in sources[:2]:
        assert source["issues"][0]["code"] in {"too_many_entries", "body_too_large"}


async def test_issue_retention_keeps_only_twenty_metadata_records(board_client):
    for _ in range(22):
        response = await board_client.put(
            "/v1/me/tasks/sources/example", json={"@type": "Task", "uid": "private task text"}
        )
        assert response.status_code == 422
    source = (await board_client.get("/v1/me/tasks/sources")).json()["sources"][0]
    assert len(source["issues"]) == 20
    assert "private task text" not in json.dumps(source)


@pytest.mark.parametrize("limits", [{"max_entries": 0}, {"max_sources": True}, {"unknown": 3}, []])
def test_limits_reject_invalid_configuration(limits):
    with pytest.raises(ValueError):
        TaskBoardLimits.from_config({"task_board": {"limits": limits}})


async def test_upload_finished_after_a_read_watermark_is_seen_by_the_next_read(
    board_client, monkeypatch
):
    clock = [NOW]

    class RequestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr("omnigent.server.routes.task_board.datetime", RequestClock)
    monkeypatch.setattr("omnigent.stores.task_board_store.datetime", RequestClock)
    report = json.dumps(task()).encode()
    observed = []

    async def upload():
        yield report[:20]
        clock[0] = NOW + timedelta(seconds=70)
        response = await board_client.get("/v1/me/tasks", params={"changed_after": STAMP})
        observed.append(response.json()["omnigent.ai:changedThrough"])
        clock[0] = NOW + timedelta(seconds=80)
        yield report[20:]

    response = await board_client.put(
        "/v1/me/tasks/sources/example",
        content=upload(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    response = await board_client.get(
        "/v1/me/tasks",
        params={
            "changed_after": utc_text(datetime.fromisoformat(observed[0]).timestamp() - 60),
        },
    )
    assert [item["uid"] for item in response.json()["entries"]] == ["eng-1"]


def test_parallel_new_sources_cannot_overrun_the_owner_limit(store):
    for index in range(63):
        store.write("owner", f"source-{index}", parse(task()), NOW)

    def write(source):
        try:
            return store.write("owner", source, parse(task()), NOW).applied
        except ReportError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(write, ["last-a", "last-b"]))
    assert sorted(outcomes, key=str) == [1, "source_limit"]
    assert len(store.list_sources("owner")) == 64


async def test_time_window_queries_keep_fractional_precision(board_client, store):
    store.write(
        "owner@example.com",
        "example",
        parse(
            group(
                task(
                    "earlier", progress="completed", progressUpdated="2026-11-06T06:30:00.0000001Z"
                ),
                task(
                    "later", progress="completed", progressUpdated="2026-11-06T06:30:00.0000002Z"
                ),
            )
        ),
        NOW,
    )
    after, before = "2026-11-06T06:30:00.0000001Z", "2026-11-06T06:30:00.0000002Z"
    response = await board_client.get(
        "/v1/me/tasks", params={"closed_after": after, "closed_before": before}
    )
    assert [item["uid"] for item in response.json()["entries"]] == ["earlier"]
    response = await board_client.get(
        "/v1/me/tasks/closed-counts", params={"from": after, "to": before}
    )
    assert response.json()["days"] == [{"day": "2026-11-06", "source": "example", "closed": 1}]
    response = await board_client.get(
        "/v1/me/tasks", params={"changed_after": "2026-11-06T07:00:00.0000001Z"}
    )
    assert response.json()["entries"] == []


async def test_sources_doc_example_and_report_json_schema(board_client):
    root = Path(__file__).resolve().parents[2]
    doc_text = (root / "docs" / "task-board-sources.md").read_text(encoding="utf-8")
    schema = json.loads(
        (root / "docs" / "schemas" / "task-board-report.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    json_blocks = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", doc_text, flags=re.DOTALL)
    ]
    report_example = next(block for block in json_blocks if block.get("@type") == "Group")
    for valid_body in (
        report_example,
        task(
            **{
                "omnigent.ai:actions": [
                    {
                        "kind": "new-session",
                        "label": "Continue",
                        "prompt": "",
                        "project": "Example",
                    }
                ]
                * 8
            }
        ),
        task(recurrenceRules=[{"@type": "RecurrenceRule", "frequency": "daily"}]),
        task(
            participants={
                "alice": {
                    "@type": "Participant",
                    "roles": {"owner": True},
                    "sendTo": {"imip": "mailto:alice@example.com"},
                }
            }
        ),
        group(task(), {"@type": "Event", "uid": "ev-1", "updated": "2026-11-06T07:00:00Z"}),
    ):
        validator.validate(valid_body)
        parse(valid_body)

    for invalid in (
        *(
            task(**{"omnigent.ai:actions": value})
            for value in (
                None,
                {},
                [None],
                [{"kind": "future", "label": "Go", "prompt": ""}],
                [{"kind": "new-session", "label": "Go"}],
                [{"kind": "new-session", "label": "", "prompt": ""}],
                [{"kind": "new-session", "label": " ", "prompt": ""}],
                [{"kind": "new-session", "label": "x" * 65, "prompt": ""}],
                [{"kind": "new-session", "label": "Go", "prompt": "x" * 2001}],
                [{"kind": "new-session", "label": "Go", "prompt": "", "project": "x" * 129}],
                [{"kind": "new-session", "label": "Go", "prompt": "", "project": None}],
                [{"kind": "new-session", "label": "Go", "prompt": ""}] * 9,
            )
        ),
        {"@type": "Group", "updated": STAMP, "entries": []},
        {"@type": "Group", "uid": "g", "entries": []},
        {"@type": "Group", "uid": "g", "updated": STAMP},
        {"@type": "Group", "uid": "g", "updated": STAMP, "entries": [{"@type": "Task"}]},
        {"@type": "Task", "updated": STAMP},
        {"@type": "Task", "uid": "eng-1"},
        task(updated="2026-11-06T06:30:00.000Z"),
        task(updated="2026-99-99T99:99:99Z"),
        task(updated="2026-02-31T06:30:00Z"),
        task(updated="0000-11-06T06:30:00Z"),
        task(updated="2026-11-06T06:30:00Z\n"),
        task(due="2026-11-10T18:00:00Z"),
        task(due="2026-13-40T25:00:00"),
        task(due="2026-02-29T18:00:00"),
        task(progress="done"),
        task(title=None),
        task(participants={"alice": {"@type": "Participant", "name": "Alice"}}),
        task(participants={"alice": {"name": "Alice", "roles": {"owner": True}}}),
        task(links={"page": {"@type": "Link", "rel": "alternate"}}),
        task(links={"page": {"@type": "Link", "href": "/relative"}}),
        task(**{"omnigent.ai:source": "example"}),
        task(**{"omnigent.ai:gone": STAMP}),
        task(**{"omnigent.ai:closedSeen": STAMP}),
        task(**{"omnigent.ai:unresolved": ["eng-2"]}),
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)
        with pytest.raises(ReportError):
            parse(invalid)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            task(participants={"alice": {"@type": "Participant", "name": "Alice", "roles": {}}})
        )

    response = await board_client.put(
        "/v1/me/tasks/sources/example-eng",
        content=json.dumps(report_example),
        headers={"Content-Type": "application/jscalendar+json"},
    )
    assert response.status_code == 200
    expected_summary = {
        "applied": 1,
        "unchanged": 0,
        "held": [],
        "returned": 0,
        "gone": 0,
        "complete": "applied",
        "history_added": 2,
    }
    assert response.json() == expected_summary
    assert expected_summary in json_blocks
    stored = (
        await board_client.get(
            "/v1/me/tasks/item", params={"source": "example-eng", "uid": "eng-123"}
        )
    ).json()
    assert stored["omnigent.ai:key"] == "ENG-123"
    assert stored["omnigent.ai:actions"] == report_example["entries"][0]["omnigent.ai:actions"]
    assert stored["omnigent.ai:group"] == {"id": "transfer-v2", "name": "Transfer reliability"}
    assert stored["omnigent.ai:waiting"]["on"] == "dependency"
    assert stored["omnigent.ai:unresolved"] == ["eng-100", "eng-120"]
    assert [event["id"] for event in stored["omnigent.ai:history"]] == ["evt-1", "evt-2"]

    for prop in (
        "omnigent.ai:complete",
        "omnigent.ai:depends-on",
        "omnigent.ai:key",
        "omnigent.ai:sourceState",
        "omnigent.ai:resolution",
        "omnigent.ai:waiting",
        "omnigent.ai:group",
        "omnigent.ai:next",
        "omnigent.ai:actions",
        "omnigent.ai:workedBy",
        "omnigent.ai:sourceFields",
        "omnigent.ai:history",
        "omnigent.ai:source",
        "omnigent.ai:gone",
        "omnigent.ai:closedSeen",
        "omnigent.ai:unresolved",
        "omnigent.ai:cursor",
        "omnigent.ai:changedThrough",
    ):
        assert re.search(
            rf"^\| `{re.escape(prop)}` \| \*\*Omnigent-defined\*\* \|",
            doc_text,
            flags=re.MULTILINE,
        ), f"Missing Omnigent-defined table row for {prop}"
    for fragment in (
        "scopes: [usage, tasks]",
        "OMNIGENT_USAGE_READ_TOKEN",
        "OMNIGENT_AUTH_ENABLED=0",
        "changed_after",
        "DELETE",
    ):
        assert fragment in doc_text
