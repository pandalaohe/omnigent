"""Owner-locked task source publication, retries, and read pins."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import rfc8785
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlTaskBoardHistory,
    SqlTaskBoardItem,
    SqlTaskBoardSource,
    current_workspace_id,
)
from omnigent.server.task_board.models import (
    OPEN_PROGRESS,
    Report,
    ReportError,
    WriteOutcome,
    task_progress,
    utc_sort_key,
    utc_timestamp,
)


@dataclass
class SnapshotOutcome(WriteOutcome):
    snapshot: dict[str, Any] = field(default_factory=dict)


def conflict(code: str, path: str = "snapshot") -> None:
    raise ReportError(code, path, "Source snapshot is unavailable or conflicts", status=409)


def digest(value: Any) -> str:
    try:
        return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except (rfc8785.CanonicalizationError, UnicodeError):
        raise ReportError("bad_value", "snapshot", "Value cannot be canonicalized") from None


def source_pin(source: SqlTaskBoardSource) -> dict[str, Any]:
    state = source.snapshot_state or {}
    return {
        "sourceGeneration": source.source_generation
        or digest([source.workspace_id, source.owner_id, source.source_id, source.created_at]),
        "id": state.get("published", {}).get("id"),
        "asOf": state.get("published", {}).get("asOf"),
        "phase": state.get("phase", "legacy"),
    }


def _scope(model: Any, source: SqlTaskBoardSource) -> tuple[Any, ...]:
    return (
        model.workspace_id == source.workspace_id,
        model.owner_id == source.owner_id,
        model.source_id == source.source_id,
    )


def guard_sources(
    session: Session,
    owner: str,
    source_id: str | None,
    *,
    snapshot: str | None = None,
    source_generation: str | None = None,
    cursor_pins: str | None = None,
    changed_after: str | None = None,
) -> dict[str, Any]:
    statement = select(SqlTaskBoardSource).where(
        SqlTaskBoardSource.workspace_id == current_workspace_id(),
        SqlTaskBoardSource.owner_id == owner,
        SqlTaskBoardSource.source_id != "",
    )
    if source_id is not None:
        statement = statement.where(SqlTaskBoardSource.source_id == source_id)
    rows = session.scalars(statement.order_by(SqlTaskBoardSource.source_id).with_for_update())
    pins = {}
    for row in rows:
        pin = source_pin(row)
        if source_generation and pin["sourceGeneration"] != source_generation:
            conflict("source_generation_changed", "source_generation")
        if row.snapshot_state:
            if changed_after is not None:
                raise ReportError("bad_value", "changed_after", "Snapshots require a full read")
            if pin["phase"] != "published":
                conflict("snapshot_updating")
            if snapshot is not None and pin["id"] != snapshot:
                conflict("snapshot_changed")
        elif snapshot is not None:
            conflict("snapshot_changed")
        pins[row.source_id] = pin
    if source_id is not None and not pins and (snapshot or source_generation or cursor_pins):
        raise ReportError("not_found", "source", "Source no longer exists", status=404)
    if cursor_pins is not None and cursor_pins != digest(pins):
        conflict("snapshot_changed", "cursor")
    return pins


def result_document(
    session: Session,
    source: SqlTaskBoardSource,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    histories: dict[str, list[dict[str, Any]]] = {}
    for row in session.scalars(
        select(SqlTaskBoardHistory).where(*_scope(SqlTaskBoardHistory, source))
    ):
        histories.setdefault(row.uid, []).append(row.entry)
    tasks, removed = [], []
    for row in session.scalars(select(SqlTaskBoardItem).where(*_scope(SqlTaskBoardItem, source))):
        body = dict(row.body)
        if row.uid in histories:
            body["omnigent.ai:history"] = sorted(histories[row.uid], key=lambda event: event["id"])
        tasks.append(body)
        if row.gone_at is not None:
            removed.append(row.uid)
    return {
        "group": metadata,
        "tasks": sorted(tasks, key=lambda task: task["uid"]),
        "removedUids": sorted(removed),
    }


def _validate_references(source: SqlTaskBoardSource, document: dict[str, Any]) -> None:
    uids = {task["uid"] for task in document["tasks"]}

    for index, task in enumerate(document["tasks"]):
        display = task.get("omnigent.ai:sourceDisplay", {})
        refs: list[tuple[str, dict[str, Any]]] = []
        for field_name in ("sessions", "quotes", "events"):
            refs.extend(
                (f"{field_name}[{i}].task", item["task"])
                for i, item in enumerate(display.get(field_name, []))
            )
        for i, item in enumerate(display.get("relations", [])):
            refs.extend((f"relations[{i}].{end}", item[end]) for end in ("from", "to"))
        refs.extend(
            (f"hub.members[{i}]", ref)
            for i, ref in enumerate(display.get("hub", {}).get("members", []))
        )
        for path, ref in refs:
            if ref["source"] == source.source_id and ref["uid"] not in uids:
                conflict("snapshot_conflict", f"tasks[{index}].omnigent.ai:sourceDisplay.{path}")


def apply_snapshot(
    session: Session,
    source: SqlTaskBoardSource,
    report: Report,
    apply_delta: Callable[[Report], WriteOutcome],
) -> WriteOutcome:
    group = report.group or {}
    display = group.get("omnigent.ai:sourceDisplay", {})
    header = display.get("snapshot")
    state = dict(source.snapshot_state or {})
    if header is None:
        if state:
            conflict("snapshot_required")
        return apply_delta(report)
    if "omnigent.ai:complete" in group:
        raise ReportError("bad_value", "omnigent.ai:complete", "Snapshots use explicit removals")
    pin = source_pin(source)
    operation = header["operation"]
    if header.get("sourceGeneration") != pin["sourceGeneration"]:
        # A first begin may discover an existing legacy source's generation.
        initial_retry = (
            operation == "begin"
            and state.get("id") == header["id"]
            and header.get("sourceGeneration") is None
        )
        if not initial_retry and (
            operation != "begin" or state or header.get("sourceGeneration") is not None
        ):
            conflict("source_generation_changed", "snapshot.sourceGeneration")
    request_hash = digest(group)
    index = str(header.get("chunkIndex", ""))
    receipt_key = operation + ":" + index
    receipts = dict(state.get("receipts", {}))
    if state.get("id") == header["id"] and receipt_key in receipts:
        receipt = receipts[receipt_key]
        if receipt["hash"] != request_hash:
            conflict("chunk_conflict" if operation == "append" else "snapshot_conflict")
        return SnapshotOutcome(**receipt["outcome"])
    manifest = {
        key: value
        for key, value in header.items()
        if key not in {"operation", "chunkIndex", "removedUids", "sourceGeneration", "abandons"}
    }
    manifest.update(
        {
            "asOf": display["asOf"],
            "timeZone": display["timeZone"],
            "updated": group["updated"],
            "sequence": group.get("sequence", 0),
            "uid": group["uid"],
        }
    )
    published = state.get("published", {})
    version = (utc_sort_key(group["updated"]), group.get("sequence", 0))
    if operation == "begin":
        if report.tasks or header.get("removedUids"):
            raise ReportError("bad_value", "entries", "Begin cannot write tasks")
        if state.get("phase") == "staging" and header.get("abandons") != state.get("id"):
            conflict("snapshot_busy")
        if header.get("baseId") != published.get("id"):
            conflict("snapshot_changed", "snapshot.baseId")
        if published and (
            header["id"] == published["id"] or version <= tuple(published["version"])
        ):
            conflict("snapshot_conflict")
        metadata = {key: value for key, value in group.items() if key != "entries"}
        metadata["omnigent.ai:sourceDisplay"] = {
            key: value for key, value in display.items() if key != "snapshot"
        }
        for release in display.get("releases", []):
            if release["source"] != source.source_id:
                raise ReportError(
                    "bad_value", "releases.source", "Release source must match report source"
                )
        state = {
            "phase": "staging",
            "id": header["id"],
            "manifest": manifest,
            "metadata": metadata,
            "receipts": {},
            "uids": [],
            "published": published,
            "counts": state.get("counts"),
        }
        receipts = {}
        outcome = SnapshotOutcome()
    else:
        if state.get("id") != header["id"] or state.get("phase") != "staging":
            conflict("snapshot_changed")
        if state["manifest"] != manifest:
            conflict("snapshot_conflict")
        if set(display) - {"version", "asOf", "timeZone", "snapshot"}:
            raise ReportError(
                "bad_value", "omnigent.ai:sourceDisplay", "Only begin supplies metadata"
            )
        if operation == "append":
            outcome = _append(session, source, report, header, state, apply_delta)
        else:
            outcome = _publish(session, source, report, state)
    outcome.snapshot = {
        **source_pin(source),
        "id": header["id"],
        "asOf": display["asOf"],
        "phase": state["phase"],
        "operation": operation,
    }
    receipts[receipt_key] = {"hash": request_hash, "outcome": asdict(outcome)}
    state["receipts"] = receipts
    source.snapshot_state = state
    return outcome


def _append(
    session: Session,
    source: SqlTaskBoardSource,
    report: Report,
    header: dict[str, Any],
    state: dict[str, Any],
    apply_delta: Callable[[Report], WriteOutcome],
) -> SnapshotOutcome:
    index = header.get("chunkIndex")
    if type(index) is not int or not 0 <= index < header["chunkCount"]:
        raise ReportError("bad_value", "snapshot.chunkIndex", "Invalid chunk index")
    removals = header.get("removedUids", [])
    uids = [task["uid"] for task in report.tasks] + removals
    if not uids:
        raise ReportError("bad_value", "entries", "An append must contain a delta")
    if len(set(uids)) != len(uids) or set(uids).intersection(state["uids"]):
        conflict("chunk_conflict", "entries")
    rows = {
        row.uid: row
        for row in session.scalars(
            select(SqlTaskBoardItem).where(*_scope(SqlTaskBoardItem, source))
        )
    }
    for task in report.tasks:
        row = rows.get(task["uid"])
        if row is None:
            continue
        incoming = (utc_sort_key(task["updated"]), task.get("sequence", 0))
        stored = (utc_sort_key(row.body["updated"]), row.sequence)
        body = {key: value for key, value in task.items() if key != "omnigent.ai:history"}
        if incoming < stored or (incoming == stored and body != row.body):
            conflict("held_chunk", f"entries.{task['uid']}")
    if any(uid not in rows for uid in removals):
        conflict("snapshot_conflict", "snapshot.removedUids")
    result = apply_delta(Report(report.tasks))
    for uid in removals:
        rows[uid].gone_at = rows[uid].changed_at = utc_timestamp(state["manifest"]["asOf"])
    state["uids"] = [*state["uids"], *uids]
    return SnapshotOutcome(**asdict(result), snapshot={})


def _publish(
    session: Session,
    source: SqlTaskBoardSource,
    report: Report,
    state: dict[str, Any],
) -> SnapshotOutcome:
    header = state["manifest"]
    if report.tasks or (report.group or {}).get("omnigent.ai:sourceDisplay", {}).get(
        "snapshot", {}
    ).get("removedUids"):
        raise ReportError("bad_value", "entries", "Publish cannot write tasks")
    indices = {key for key in state["receipts"] if key.startswith("append:")}
    if len(indices) != header["chunkCount"]:
        conflict("snapshot_incomplete")
    session.flush()
    document = result_document(session, source, state["metadata"])
    count = len(document["tasks"]) - len(document["removedUids"])
    if count != header["expectedTaskCount"] or digest(document) != header["contentHash"]:
        conflict("snapshot_conflict", "snapshot.contentHash")
    _validate_references(source, document)
    source.source_display = state["metadata"]["omnigent.ai:sourceDisplay"]
    for key in ("title", "source"):
        if key in state["metadata"]:
            setattr(source, "name" if key == "title" else key, state["metadata"][key])
    removed = set(document["removedUids"])
    alive = [task for task in document["tasks"] if task["uid"] not in removed]
    opened = sum(task_progress(task) in OPEN_PROGRESS for task in alive)
    state["counts"] = {"open": opened, "closed": len(alive) - opened, "gone": len(removed)}
    state["phase"] = "published"
    state["published"] = {
        "id": state["id"],
        "asOf": header["asOf"],
        "version": [utc_sort_key(header["updated"]), header["sequence"]],
    }
    return SnapshotOutcome()
