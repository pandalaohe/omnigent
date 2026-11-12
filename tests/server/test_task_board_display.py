"""Source display validation at the report boundary."""

import json
from pathlib import Path

import jsonschema
import pytest

from omnigent.server.task_board.models import ReportError, TaskBoardLimits, parse_report

STAMP = "2026-11-12T12:00:00Z"
KEY = "omnigent.ai:sourceDisplay"
SCHEMA = json.loads(
    (Path(__file__).parents[2] / "docs/schemas/task-board-report.schema.json").read_text()
)


def task(display=None, **fields):
    result = {"@type": "Task", "uid": "task-1", "updated": STAMP, **fields}
    if display is not None:
        result[KEY] = display
    return result


def report(value):
    return parse_report(json.dumps(value).encode(), "application/json", TaskBoardLimits())


def source_display():
    return {
        "version": 1,
        "asOf": STAMP,
        "timeZone": "Etc/UTC",
        "snapshot": {
            "id": "generation-1",
            "baseId": None,
            "operation": "begin",
            "chunkCount": 0,
            "expectedTaskCount": 0,
            "contentHash": "a" * 64,
            "coverageFrom": "2026-01-01",
            "coverageTo": "2026-12-31",
        },
    }


def test_legacy_and_rich_display_preserve_input():
    ref = {"source": "example", "uid": "task-1"}
    display = {
        "version": 1,
        "state": "running",
        "identity": {"key": "ENG-42", "label": "Example", "groupId": "team"},
        "links": [{"id": "issue", "label": "Issue", "href": "https://issues.example.org/42"}],
        "sessions": [
            {
                "id": "session-1",
                "task": ref,
                "role": "root",
                "agentId": "agent",
                "label": "Agent",
                "title": "Work",
                "status": "running",
                "createdAt": STAMP,
                "lastActivityAt": STAMP,
                "runningSeconds": 0,
                "cost": {"amount": None, "kind": "estimate", "tokens": 0},
            }
        ],
        "intervals": [
            {"id": "interval-1", "from": STAMP, "to": None, "agentId": "agent", "origin": "event"}
        ],
        "quotes": [{"id": "quote-1", "text": "A quote", "task": ref}],
    }
    item = task(display, progress="in-process")
    assert report(item).tasks == [item]
    jsonschema.validate(item, SCHEMA)
    legacy = task()
    assert report(legacy).tasks == [legacy]
    jsonschema.validate(legacy, SCHEMA)


def test_source_and_declared_accounts():
    display = source_display()
    ref = {"namespace": "example", "id": "account-1"}
    display["accounts"] = [
        {"ref": ref, "label": "Account", "authoritySource": "example", "timeZone": "Etc/UTC"}
    ]
    display["accountDays"] = [
        {
            "id": "day-1",
            "account": ref,
            "date": "2026-11-12",
            "amount": 0,
            "kind": "estimate",
            "sourceLabel": "Estimate",
        }
    ]
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [], KEY: display}
    assert report(group).group == group
    jsonschema.validate(group, SCHEMA)
    display["accountDays"].append({**display["accountDays"][0], "id": "day-2"})
    with pytest.raises(ReportError):
        report(group)


def repository_day():
    return {
        "repository": "Example repository",
        "date": "2026-11-03",
        "timeZone": "Asia/Taipei",
        "totalCommits": 225,
        "refs": ["refs/heads/main"],
        "head": "a" * 40,
        "collectedAt": STAMP,
    }


@pytest.mark.parametrize("head", ["a" * 40, "A1" * 32])
def test_repository_daily_commits_round_trip(head):
    display = {**source_display(), "repositoryDailyCommits": [{**repository_day(), "head": head}]}
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [], KEY: display}
    assert report(group).group == group
    jsonschema.validate(group, SCHEMA)


@pytest.mark.parametrize("head", ["not-a-sha", "a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40])
def test_repository_head_rejection_matches_schema(head):
    display = {**source_display(), "repositoryDailyCommits": [{**repository_day(), "head": head}]}
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [task()], KEY: display}
    with pytest.raises(ReportError) as error:
        report(group)
    assert error.value.status == 422
    assert error.value.path.endswith("repositoryDailyCommits[0].head")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(group, SCHEMA)


@pytest.mark.parametrize(
    "change",
    [
        {"totalCommits": -1},
        {"totalCommits": True},
        {"totalCommits": 1.5},
        {"totalCommits": 9007199254740992},
        {"repository": " "},
        {"repository": "r" * 129},
        {"date": "2026-02-30"},
        {"timeZone": "Unknown/Zone"},
        {"refs": ["main"] * 9},
        {"collectedAt": "2026-11-03T00:00:00+08:00"},
        {"unknown": 0},
    ],
)
def test_invalid_repository_daily_commits_reject_whole_report(change):
    display = {
        **source_display(),
        "repositoryDailyCommits": [{**repository_day(), **change}],
    }
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [task()], KEY: display}
    with pytest.raises(ReportError) as error:
        report(group)
    assert error.value.status == 422
    assert "repositoryDailyCommits" in error.value.path


@pytest.mark.parametrize("count", [2, 401])
def test_duplicate_or_oversized_repository_days_rejected(count):
    display = {**source_display(), "repositoryDailyCommits": [repository_day()] * count}
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [], KEY: display}
    with pytest.raises(ReportError) as error:
        report(group)
    assert error.value.status == 422


@pytest.mark.parametrize(
    "display",
    [
        {"version": 2},
        {"version": True},
        {"version": 1, "unknown": 1},
        {"version": 1, "current": None},
        {"version": 1, "state": "running"},
        {
            "version": 1,
            "links": [{"id": "x", "label": "X", "href": "https://user:pass@example.org/"}],
        },
        {
            "version": 1,
            "sessions": [
                {
                    "id": "s",
                    "task": {"source": "example", "uid": "u"},
                    "role": "root",
                    "agentId": "a",
                    "label": "A",
                    "title": "T",
                    "status": "running",
                    "createdAt": STAMP,
                    "lastActivityAt": STAMP,
                }
            ],
        },
        {"version": 1, "omitted": {"events": True}},
    ],
)
def test_invalid_display_rejected(display):
    with pytest.raises(ReportError) as error:
        report(task(display))
    assert error.value.status == 422
    assert error.value.path.startswith(KEY)


def test_group_display_requires_snapshot():
    display = source_display()
    del display["snapshot"]
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [], KEY: display}
    with pytest.raises(ReportError) as error:
        report(group)
    assert error.value.path == KEY + ".snapshot"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(group, SCHEMA)


@pytest.mark.parametrize(
    "display",
    [
        {"version": 2},
        {"version": True},
        {"version": 1, "unknown": 1},
        {"version": 1, "current": None},
        {"version": 1, "omitted": {"events": True}},
        {
            "version": 1,
            "links": [{"id": "x", "label": "X", "href": "https://user:pass@example.org/"}],
        },
    ],
)
def test_shape_rejection_matches_schema(display):
    value = task(display)
    with pytest.raises(ReportError):
        report(value)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, SCHEMA)


def test_bounds_duplicates_and_reference_validation():
    for display in (
        {"version": 1, "events": [{}] * 5001},
        {"version": 1, "badges": [{"id": "same", "label": "A"}] * 2},
        {"version": 1, "lastActivity": "2026-02-30T00:00:00Z"},
        {"version": 1, "omitted": {"events": 9007199254740992}},
    ):
        with pytest.raises(ReportError):
            report(task(display))
    display = source_display()
    display["accountDays"] = [
        {
            "id": "day",
            "account": {"namespace": "example", "id": "missing"},
            "date": "2026-11-12",
            "amount": 1,
            "kind": "estimate",
            "sourceLabel": "Estimate",
        }
    ]
    group = {"@type": "Group", "uid": "group", "updated": STAMP, "entries": [], KEY: display}
    with pytest.raises(ReportError):
        report(group)
