"""Exercise the report boundary against the actual store, not an in-memory substitute."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
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
        "owner", ItemQuery(state="closed", closed_after=NOW.timestamp() - 7200)
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
