"""Snapshot publication never exposes a partly replaced source."""

from copy import deepcopy

import pytest

from omnigent.server.task_board.models import Report, ReportError
from omnigent.stores.task_board_snapshots import digest
from omnigent.stores.task_board_store import HistoryQuery, ItemQuery, TaskBoardStore

STAMP = "2026-11-12T04:00:00Z"


def task(uid, sequence=0, title="Original"):
    return {"@type": "Task", "uid": uid, "updated": STAMP, "sequence": sequence, "title": title}


def envelope(tasks, *, snapshot_id="first", base_id=None, sequence=1, removed=(), chunks=1):
    metadata = {
        "@type": "Group",
        "uid": "feed",
        "updated": STAMP,
        "sequence": sequence,
        "omnigent.ai:sourceDisplay": {"version": 1, "asOf": STAMP, "timeZone": "UTC"},
    }
    document = {
        "group": metadata,
        "tasks": sorted(tasks, key=lambda item: item["uid"]),
        "removedUids": sorted(removed),
    }
    header = {
        "id": snapshot_id,
        "baseId": base_id,
        "chunkCount": chunks,
        "expectedTaskCount": len(tasks) - len(removed),
        "contentHash": digest(document),
        "coverageFrom": "2025-11-12",
        "coverageTo": "2026-11-13",
    }
    return metadata, header


def operation(metadata, header, name, entries=(), **changes):
    body = deepcopy(metadata)
    body["entries"] = list(entries)
    body["omnigent.ai:sourceDisplay"]["snapshot"] = {**header, "operation": name, **changes}
    return Report(list(entries), body)


def publish(store, tasks):
    metadata, header = envelope(tasks)
    receipt = store.write("owner", "example", operation(metadata, header, "begin"))
    header["sourceGeneration"] = receipt.snapshot["sourceGeneration"]
    store.write("owner", "example", operation(metadata, header, "append", tasks, chunkIndex=0))
    result = store.write("owner", "example", operation(metadata, header, "publish"))
    return metadata, header, result


def test_staging_guards_all_reads_and_writers_and_publish_retry(db_uri):
    store = TaskBoardStore(db_uri)
    metadata, header = envelope([task("a"), task("b")], chunks=2)
    begin = store.write("owner", "example", operation(metadata, header, "begin"))
    header["sourceGeneration"] = begin.snapshot["sourceGeneration"]
    append = operation(metadata, header, "append", [task("a")], chunkIndex=0)
    first = store.write("owner", "example", append)
    assert store.write("owner", "example", append) == first
    reads = [
        lambda: store.list_items("owner", ItemQuery()),
        lambda: store.get_item("owner", "example", "a"),
        lambda: store.list_history("owner", HistoryQuery()),
        lambda: store.closed_counts("owner", after=None, before=None, offset=0, source=None),
        lambda: store.read_display(
            "owner",
            "example",
            snapshot=None,
            source_generation=None,
            part="identity",
            after=None,
            before=None,
            offset=0,
            limit=100,
        ),
    ]
    for read in reads:
        with pytest.raises(ReportError, match="snapshot") as error:
            read()
        assert error.value.code == "snapshot_updating"
    with pytest.raises(ReportError) as error:
        store.write("owner", "example", Report([task("c")]))
    assert error.value.code == "snapshot_required"
    with pytest.raises(ReportError) as error:
        store.write("owner", "example", operation(metadata, header, "publish"))
    assert error.value.code == "snapshot_incomplete"
    store.write(
        "owner", "example", operation(metadata, header, "append", [task("b")], chunkIndex=1)
    )
    request = operation(metadata, header, "publish")
    result = store.write("owner", "example", request)
    assert store.write("owner", "example", request) == result
    assert {row["uid"] for row in store.list_items("owner", ItemQuery())[0]} == {"a", "b"}


def test_replacement_invalidates_cursor_and_held_chunk_is_atomic(db_uri):
    store = TaskBoardStore(db_uri)
    _, old_header, _ = publish(store, [task("a"), task("b")])
    _, cursor = store.list_items("owner", ItemQuery(limit=1))
    metadata, header = envelope(
        [task("a", 1, "New"), task("b")], snapshot_id="second", base_id="first", sequence=2
    )
    header["sourceGeneration"] = old_header["sourceGeneration"]
    store.write("owner", "example", operation(metadata, header, "begin"))
    with pytest.raises(ReportError) as error:
        store.write(
            "owner",
            "example",
            operation(
                metadata,
                header,
                "append",
                [task("a", 1, "New"), task("b", 0, "Conflict")],
                chunkIndex=0,
            ),
        )
    assert error.value.code == "held_chunk"
    store.write(
        "owner",
        "example",
        operation(metadata, header, "append", [task("a", 1, "New")], chunkIndex=0),
    )
    store.write("owner", "example", operation(metadata, header, "publish"))
    with pytest.raises(ReportError) as error:
        store.list_items("owner", ItemQuery(limit=1, cursor=cursor))
    assert error.value.code == "snapshot_changed"
    assert store.get_item("owner", "example", "a")["title"] == "New"
    assert store.get_item("owner", "example", "b")["title"] == "Original"


def test_delete_recreate_rejects_old_generation_and_zero_delta(db_uri):
    store = TaskBoardStore(db_uri)
    metadata, header, _ = publish(store, [task("a")])
    assert store.delete_source("owner", "example")
    store.write("owner", "example", Report([task("a")]))
    with pytest.raises(ReportError) as error:
        store.write(
            "owner", "example", operation(metadata, header, "append", [task("a")], chunkIndex=0)
        )
    assert error.value.code == "source_generation_changed"
    metadata, header = envelope([task("a")], snapshot_id="new", chunks=0)
    receipt = store.write("owner", "example", operation(metadata, header, "begin"))
    header["sourceGeneration"] = receipt.snapshot["sourceGeneration"]
    store.write("owner", "example", operation(metadata, header, "publish"))
    assert store.get_item("owner", "example", "a")["uid"] == "a"


def test_jcs_golden_number_and_unicode_forms():
    assert digest({"b": -0.0, "a": 1.0}) == digest({"a": 1, "b": 0})
    import rfc8785

    assert (
        rfc8785.dumps({"cost": None, "amount": 1e-7, "count": 42})
        == b'{"amount":1e-7,"cost":null,"count":42}'
    )


def test_documented_golden_vectors_and_wire_envelopes(db_uri):
    import json
    from pathlib import Path

    import rfc8785

    from omnigent.server.task_board.models import TaskBoardLimits, parse_report

    fixture = json.loads(
        (Path(__file__).parents[2] / "docs/schemas/task-board-snapshot-vectors.json").read_text()
    )
    for vector in fixture["vectors"]:
        assert rfc8785.dumps(vector["input"]).decode() == vector["canonical"]
        assert digest(vector["input"]) == vector["sha256"]
    store = TaskBoardStore(db_uri)
    generation = None
    for name in ("begin", "append", "publish"):
        envelope = deepcopy(fixture["envelopes"][name])
        if generation:
            envelope["omnigent.ai:sourceDisplay"]["snapshot"]["sourceGeneration"] = generation
        report = parse_report(json.dumps(envelope).encode(), "application/json", TaskBoardLimits())
        receipt = store.write("owner", "example", report)
        generation = receipt.snapshot["sourceGeneration"]
    assert store.get_item("owner", "example", "example-1")["title"] == "Example"


def test_abandon_resume_removal_conflicting_retry_and_concurrent_begin(db_uri):
    from concurrent.futures import ThreadPoolExecutor

    store = TaskBoardStore(db_uri)
    _, original, _ = publish(store, [task("a"), task("b")])
    metadata, header = envelope(
        [task("a", 1, "New"), task("b")],
        snapshot_id="second",
        base_id="first",
        sequence=2,
        removed=["b"],
    )
    header["sourceGeneration"] = original["sourceGeneration"]
    store.write("owner", "example", operation(metadata, header, "begin"))
    append = operation(
        metadata, header, "append", [task("a", 1, "New")], chunkIndex=0, removedUids=["b"]
    )
    store.write("owner", "example", append)
    with pytest.raises(ReportError) as error:
        store.write(
            "owner", "example", operation(metadata, header, "append", [task("a", 2)], chunkIndex=0)
        )
    assert error.value.code == "chunk_conflict"
    replacement = {**header, "id": "replacement", "abandons": "second"}
    store.write("owner", "example", operation(metadata, replacement, "begin"))
    restarted = TaskBoardStore(db_uri)
    restarted.write(
        "owner",
        "example",
        operation(
            metadata, replacement, "append", [task("a", 1, "New")], chunkIndex=0, removedUids=["b"]
        ),
    )
    restarted.write("owner", "example", operation(metadata, replacement, "publish"))
    assert [row["uid"] for row in store.list_items("owner", ItemQuery())[0]] == ["a"]
    assert store.get_item("owner", "example", "b")["omnigent.ai:gone"]

    def begin(index):
        next_metadata, next_header = envelope(
            [task("a", 1, "New"), task("b")],
            snapshot_id=f"third-{index}",
            base_id="replacement",
            sequence=3,
            removed=["b"],
            chunks=0,
        )
        next_header["sourceGeneration"] = original["sourceGeneration"]
        try:
            store.write("owner", "example", operation(next_metadata, next_header, "begin"))
            return "begun"
        except ReportError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, range(2)))
    assert sorted(results) == ["begun", "snapshot_busy"]


def test_max_sources_cursor_is_bounded_and_keeps_generation_guard(db_uri):
    store = TaskBoardStore(db_uri)
    for index in range(64):
        store.write("owner", f"source-{index:02}", Report([task("one")]))
    _, cursor = store.list_items("owner", ItemQuery(limit=1))
    assert cursor and len(cursor) < 4096
    rows, _ = store.list_items("owner", ItemQuery(limit=1, cursor=cursor))
    assert rows[0]["omnigent.ai:source"] == "source-01"
    store.delete_source("owner", "source-63")
    store.write("owner", "source-63", Report([task("one")]))
    with pytest.raises(ReportError) as error:
        store.list_items("owner", ItemQuery(limit=1, cursor=cursor))
    assert error.value.code == "snapshot_changed"


def test_raw_vendor_record_does_not_become_a_task_reference(db_uri):
    item = {**task("one"), "vendor:record": {"source": "example", "uid": "outside"}}
    store = TaskBoardStore(db_uri)
    publish(store, [item])
    assert store.get_item("owner", "example", "one")["vendor:record"]["uid"] == "outside"


def test_display_fractional_second_bounds_keep_full_precision(db_uri):
    store = TaskBoardStore(db_uri)
    metadata, header = envelope([], chunks=0)
    values = [
        {"id": "integer", "name": "Original", "publishedAt": STAMP, "source": "example"},
        {
            "id": "fraction",
            "name": "New",
            "publishedAt": STAMP.replace("00Z", "00.0000001Z"),
            "source": "example",
        },
    ]
    metadata["omnigent.ai:sourceDisplay"]["releases"] = values
    header["contentHash"] = digest({"group": metadata, "tasks": [], "removedUids": []})
    receipt = store.write("owner", "example", operation(metadata, header, "begin"))
    header["sourceGeneration"] = receipt.snapshot["sourceGeneration"]
    publication = deepcopy(metadata)
    publication["omnigent.ai:sourceDisplay"].pop("releases")
    store.write("owner", "example", operation(publication, header, "publish"))
    result = store.read_display(
        "owner",
        "example",
        snapshot="first",
        source_generation=header["sourceGeneration"],
        part="releases",
        after=STAMP,
        before="2026-11-12T04:00:01Z",
        limit=100,
    )
    assert [row["id"] for row in result["entries"]] == ["integer", "fraction"]
