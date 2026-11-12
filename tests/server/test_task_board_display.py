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
            "links": [{"id": "x", "label": "X", "href": "https://user:pass@example.com/"}],
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
            "links": [{"id": "x", "label": "X", "href": "https://user:pass@example.com/"}],
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
