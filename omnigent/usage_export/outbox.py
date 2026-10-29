"""A durable, metering-only spool outside disposable harness homes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock, Timeout

from omnigent.usage_export.models import UsageObservation
from omnigent.usage_export.transcripts import (
    antigravity_measurement,
    claude_measurement,
    claude_physical_identity,
    codex_measurement,
    pi_measurement,
)


def _terminal_evidence(physical: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "physical_session_id": physical.get("physical_session_id"),
        "physical_subagent_id": physical.get("physical_subagent_id"),
        "evidence": "terminal_record",
    }


class UsageOutbox:
    """Commit observed counters before ACK or private-home removal.

    A caller owns the directory and provides only transcripts it controls.
    No background pruning or storage of prompts, headers, or credentials.
    """

    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "outbox.sqlite3"
        self._drain_lock = FileLock(str(directory / "drain.lock"), mode=0o600)
        self._proof_lock = FileLock(str(directory / "claude-proof.lock"), mode=0o600)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS producers (
                    session_id TEXT NOT NULL, harness TEXT NOT NULL,
                    producer_id TEXT NOT NULL, binding TEXT,
                    PRIMARY KEY (session_id, harness)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    producer_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS latest (
                    producer_id TEXT NOT NULL, unit_key TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (producer_id, unit_key)
                );
                CREATE TABLE IF NOT EXISTS deferred_measurements (
                    session_id TEXT, harness TEXT, physical TEXT, turn_key TEXT,
                    unit_key TEXT, payload TEXT, origin TEXT,
                    PRIMARY KEY (session_id,harness,physical,turn_key,unit_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    harness TEXT NOT NULL, offset INTEGER NOT NULL,
                    identity TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capture_turns (
                    session_id TEXT, harness TEXT, physical TEXT, turn_id TEXT NOT NULL,
                    PRIMARY KEY (session_id,harness,physical)
                );
                CREATE TABLE IF NOT EXISTS unit_turns (
                    session_id TEXT, harness TEXT, physical TEXT, turn_id TEXT,
                    producer_id TEXT, unit_key TEXT,
                    PRIMARY KEY (producer_id,unit_key)
                );
                CREATE TABLE IF NOT EXISTS terminal_receipts (
                    session_id TEXT, harness TEXT, physical TEXT, turn_id TEXT,
                    ended_at TEXT NOT NULL,
                    PRIMARY KEY (session_id,harness,physical,turn_id)
                );
                CREATE TABLE IF NOT EXISTS pi_roots (
                    path TEXT PRIMARY KEY, session_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pi_seen (
                    session_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, entry_id)
                );
                CREATE TABLE IF NOT EXISTS terminal_proofs (
                    session_id TEXT, harness TEXT, physical TEXT, turn_id TEXT,
                    proof TEXT NOT NULL, PRIMARY KEY (session_id,harness,physical,turn_id)
                );
                CREATE TABLE IF NOT EXISTS admission_links (
                    session_id TEXT, harness TEXT, physical TEXT, turn_key TEXT,
                    admission_id TEXT NOT NULL, route_check TEXT,
                    PRIMARY KEY (session_id,harness,physical,turn_key)
                );
                CREATE TABLE IF NOT EXISTS claude_path_proofs (
                    session_id TEXT NOT NULL, harness TEXT NOT NULL,
                    physical_session_id TEXT NOT NULL,
                    physical_subagent_id TEXT NOT NULL,
                    path TEXT, conflicted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id,harness,physical_session_id,physical_subagent_id)
                );
                CREATE TABLE IF NOT EXISTS server_acks (
                    producer_id TEXT NOT NULL, producer_unit_id TEXT NOT NULL,
                    observation TEXT NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY (producer_id,producer_unit_id)
                );
                CREATE TABLE IF NOT EXISTS capture_runs (
                    session_id TEXT NOT NULL, harness TEXT NOT NULL,
                    pid INTEGER NOT NULL, process_started REAL NOT NULL,
                    capture_started REAL NOT NULL,
                    PRIMARY KEY (session_id,harness,pid,process_started)
                );
                CREATE TABLE IF NOT EXISTS pending_admissions (
                    session_id TEXT NOT NULL, harness TEXT NOT NULL,
                    admission TEXT NOT NULL,
                    PRIMARY KEY (session_id,harness)
                );
                CREATE TABLE IF NOT EXISTS runtime_routes (
                    session_id TEXT NOT NULL, harness TEXT NOT NULL, route TEXT NOT NULL,
                    PRIMARY KEY (session_id,harness)
                );
                CREATE TABLE IF NOT EXISTS capture_roots (
                    path TEXT PRIMARY KEY, session_id TEXT NOT NULL, harness TEXT NOT NULL,
                    main_thread TEXT
                );
                CREATE TABLE IF NOT EXISTS unit_origins (
                    producer_id TEXT, unit_key TEXT, path TEXT, position INTEGER,
                    PRIMARY KEY (producer_id,unit_key)
                );
                CREATE TABLE IF NOT EXISTS cutover_checkpoints (
                    host_id TEXT, harness TEXT, generation INTEGER, sources TEXT NOT NULL,
                    PRIMARY KEY (host_id,harness,generation)
                );
                CREATE TABLE IF NOT EXISTS physical_turn_spans (
                    session_id TEXT, harness TEXT, physical TEXT, turn_key TEXT,
                    started_at REAL NOT NULL, ended_at REAL,
                    PRIMARY KEY (session_id,harness,physical,turn_key)
                );
                CREATE TABLE IF NOT EXISTS physical_children (
                    session_id TEXT, harness TEXT, physical TEXT, parent TEXT,
                    born_at REAL, resolved INTEGER NOT NULL DEFAULT 0,
                    admission_id TEXT, route_check TEXT,
                    PRIMARY KEY (session_id,harness,physical)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _append(
        self,
        db: sqlite3.Connection,
        session_id: str,
        harness: str,
        observation: dict[str, Any],
    ) -> tuple[str, str]:
        db.execute(
            "INSERT OR IGNORE INTO producers VALUES (?, ?, ?, NULL)",
            (session_id, harness, uuid.uuid4().hex),
        )
        producer_id = db.execute(
            "SELECT producer_id FROM producers WHERE session_id=? AND harness=?",
            (session_id, harness),
        ).fetchone()[0]
        observation = {"meter_epoch": producer_id, **observation}
        key = self._json(
            [
                observation.get("physical_session_id"),
                observation.get("physical_subagent_id"),
                observation["producer_unit_id"],
            ]
        )
        latest = db.execute(
            "SELECT payload FROM latest WHERE producer_id=? AND unit_key=?", (producer_id, key)
        ).fetchone()
        observation["producer_unit_id"] = hashlib.sha256(key.encode()).hexdigest()
        self._enqueue(db, producer_id, key, observation, latest)
        return producer_id, key

    def _enqueue(
        self,
        db: sqlite3.Connection,
        producer_id: str,
        key: str,
        observation: dict[str, Any],
        latest: tuple[str] | None,
    ) -> None:
        if latest is not None:
            previous = json.loads(latest[0])
            observation = {
                **previous,
                **{
                    key: value
                    for key, value in observation.items()
                    if value is not None and value != []
                },
                "occurred_at": previous.get("occurred_at") or observation.get("occurred_at"),
                "thread_kind": previous.get("thread_kind", observation.get("thread_kind")),
            }
            observation["source_refs"] = list(
                {
                    self._json(ref): ref
                    for ref in [
                        *previous.get("source_refs", []),
                        *observation.get("source_refs", []),
                    ]
                }.values()
            )
        payload = self._json(observation)
        if latest is not None and latest[0] == payload:
            return
        db.execute(
            "INSERT INTO observations (producer_id,payload) VALUES (?,?)", (producer_id, payload)
        )
        db.execute("INSERT OR REPLACE INTO latest VALUES (?,?,?)", (producer_id, key, payload))

    def _observe(
        self,
        db: sqlite3.Connection,
        session_id: str,
        harness: str,
        physical: Mapping[str, Any],
        record: Mapping[str, Any],
        source_position: tuple[str, int] | None = None,
    ) -> None:
        """Associate only explicit terminal records with their physical turn."""
        if harness in {"pi-native", "pi", "antigravity-native"}:
            measurement = (
                antigravity_measurement(record)
                if harness == "antigravity-native"
                else pi_measurement(record)
            )
            if measurement is None:
                return
            if harness == "pi-native":
                entry_identity = self._json(
                    [measurement["producer_unit_id"], record.get("timestamp")]
                )
                seen = db.execute(
                    "INSERT OR IGNORE INTO pi_seen VALUES (?,?)",
                    (session_id, entry_identity),
                )
                if seen.rowcount == 0:
                    return
            producer, unit = self._append(db, session_id, harness, {**physical, **measurement})
            if source_position is not None:
                db.execute(
                    "INSERT OR IGNORE INTO unit_origins VALUES (?,?,?,?)",
                    (producer, unit, *source_position),
                )
            return
        source = self._json(
            [physical.get("physical_session_id"), physical.get("physical_subagent_id")]
        )
        scope = (session_id, harness, source)
        stamp = record.get("timestamp")
        born_at = None
        if isinstance(stamp, str):
            with suppress(ValueError):
                normalized = UsageObservation.utc_time(stamp)
                if normalized is not None:
                    born_at = datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()
        parent_physical = physical.get("parent_physical_session_id")
        if physical.get("physical_subagent_id") is not None:
            parent_physical = physical.get("physical_session_id")
        if parent_physical is not None:
            db.execute(
                "INSERT OR IGNORE INTO physical_children "
                "(session_id,harness,physical,parent,born_at) VALUES (?,?,?,?,?)",
                (*scope, self._json([parent_physical, None]), born_at),
            )
        claude = harness.startswith("claude-")
        state = db.execute(
            "SELECT turn_id FROM capture_turns WHERE session_id=? AND harness=? AND physical=?",
            scope,
        ).fetchone()
        turn = state[0] if state is not None else uuid.uuid4().hex
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        tool_reply = (
            isinstance(content, list)
            and content
            and all(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in content
            )
        )
        if (
            claude
            and record.get("type") == "user"
            and not tool_reply
            and record.get("isCompactSummary") is not True
        ):
            turn = record.get("uuid") or uuid.uuid4().hex
        if claude:
            db.execute("INSERT OR REPLACE INTO capture_turns VALUES (?,?,?,?)", (*scope, turn))
            if (
                record.get("type") == "user"
                and not tool_reply
                and not record.get("isCompactSummary")
            ):
                if born_at is not None and parent_physical is None:
                    db.execute(
                        "INSERT OR IGNORE INTO physical_turn_spans VALUES (?,?,?,?,?,NULL)",
                        (*scope, turn, born_at),
                    )
                pending = db.execute(
                    "SELECT admission FROM pending_admissions WHERE session_id=? AND harness=?",
                    (session_id, harness),
                ).fetchone()
                if pending and physical.get("physical_subagent_id") is None:
                    admission = json.loads(pending[0])
                    db.execute(
                        "INSERT OR IGNORE INTO admission_links VALUES (?,?,?,?,?,?)",
                        (*scope, turn, admission["admission_id"], admission.get("route_check")),
                    )
                    db.execute(
                        "DELETE FROM pending_admissions WHERE session_id=? AND harness=?",
                        (session_id, harness),
                    )
        payload = record.get("payload")
        if not claude:
            turn = payload.get("turn_id") if isinstance(payload, dict) else None
            if turn is not None and record.get("type") == "turn_context" and born_at is not None:
                db.execute(
                    "INSERT OR IGNORE INTO physical_turn_spans VALUES (?,?,?,?,?,NULL)",
                    (*scope, turn, born_at),
                )
        if parent_physical is not None and turn is not None:
            inherited = db.execute(
                "SELECT admission_id,route_check FROM physical_children WHERE session_id=? AND"
                " harness=? AND physical=? AND resolved=1",
                scope,
            ).fetchone()
            if inherited and inherited[0] is not None:
                db.execute(
                    "INSERT OR IGNORE INTO admission_links VALUES (?,?,?,?,?,?)",
                    (*scope, turn, *inherited),
                )
        measurement = claude_measurement(record) if claude else codex_measurement(record)
        if measurement is not None:
            observation = {**physical, **measurement}
            observation.pop("parent_physical_session_id", None)
            if turn is not None:
                observation["turn_key"] = turn
            if observation.get("model_id") is None:
                observation["model_id"] = physical.get("model_id")
            pending = db.execute(
                "SELECT 1 FROM pending_admissions WHERE session_id=? AND harness=?",
                (session_id, harness),
            ).fetchone()
            unresolved = db.execute(
                "SELECT 1 FROM physical_children WHERE session_id=? AND harness=? "
                "AND physical=? AND resolved=0",
                scope,
            ).fetchone()
            if (pending and not claude) or unresolved:
                # Retain only metering fields until the physical turn's
                # admission is proven; admission_links precedes enqueue.
                previous = db.execute(
                    "SELECT payload FROM deferred_measurements WHERE session_id=? "
                    "AND harness=? AND physical=? AND turn_key=? AND unit_key=?",
                    (*scope, turn or "", observation["producer_unit_id"]),
                ).fetchone()
                if previous:
                    prior = json.loads(previous[0])
                    observation = {
                        **prior,
                        **{
                            key: value
                            for key, value in observation.items()
                            if value is not None and value != []
                        },
                        "occurred_at": prior.get("occurred_at") or observation.get("occurred_at"),
                        "source_refs": list(
                            {
                                self._json(ref): ref
                                for ref in [
                                    *prior.get("source_refs", []),
                                    *observation.get("source_refs", []),
                                ]
                            }.values()
                        ),
                    }
                db.execute(
                    "INSERT OR REPLACE INTO deferred_measurements VALUES (?,?,?,?,?,?,?)",
                    (
                        *scope,
                        turn or "",
                        observation["producer_unit_id"],
                        self._json(observation),
                        self._json(source_position),
                    ),
                )
                return
            if turn is not None:
                link = db.execute(
                    "SELECT admission_id,route_check FROM admission_links "
                    "WHERE session_id=? AND harness=? AND physical=? AND turn_key=?",
                    (*scope, turn),
                ).fetchone()
                if link is not None:
                    observation["admission_id"] = link[0]
                    if link[1] is not None:
                        observation["route_check"] = link[1]
                receipt = db.execute(
                    "SELECT ended_at FROM terminal_receipts "
                    "WHERE session_id=? AND harness=? AND physical=? AND turn_id=?",
                    (*scope, turn),
                ).fetchone()
                if receipt is not None:
                    proof = db.execute(
                        "SELECT proof FROM terminal_proofs WHERE session_id=? "
                        "AND harness=? AND physical=? AND turn_id=?",
                        (*scope, turn),
                    ).fetchone()
                    observation["terminal"] = [
                        json.loads(proof[0]) if proof else _terminal_evidence(physical)
                    ]
            producer, unit = self._append(db, session_id, harness, observation)
            if source_position is not None:
                db.execute(
                    "INSERT OR IGNORE INTO unit_origins VALUES (?,?,?,?)",
                    (producer, unit, *source_position),
                )
            if turn is not None:
                db.execute(
                    "INSERT OR IGNORE INTO unit_turns VALUES (?,?,?,?,?,?)",
                    (*scope, turn, producer, unit),
                )
        terminal = (
            claude and record.get("type") == "system" and record.get("subtype") == "turn_duration"
        ) or (
            not claude
            and record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") in {"task_complete", "turn_aborted"}
        )
        if not terminal or turn is None:
            return
        stamp = record.get("timestamp")
        if not isinstance(stamp, str):
            return
        try:
            ended_at = UsageObservation.utc_time(stamp)
        except ValueError:
            return
        proof = {**_terminal_evidence(physical), "source_ref": record.get("uuid") or turn}
        self._record_terminal(db, scope, turn, proof, ended_at)

    def _record_terminal(
        self,
        db: sqlite3.Connection,
        scope: tuple[str, str, str],
        turn: str,
        proof: dict[str, Any],
        ended_at: str | None,
    ) -> None:
        db.execute(
            "INSERT OR IGNORE INTO terminal_receipts VALUES (?,?,?,?,?)", (*scope, turn, ended_at)
        )
        db.execute(
            "INSERT OR REPLACE INTO terminal_proofs VALUES (?,?,?,?,?)",
            (*scope, turn, self._json(proof)),
        )
        if ended_at is not None:
            try:
                end = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                end = float(ended_at)
            db.execute(
                "UPDATE physical_turn_spans SET ended_at=? WHERE session_id=? AND harness=? "
                "AND physical=? AND turn_key=?",
                (end, *scope, turn),
            )
        rows = db.execute(
            "SELECT l.producer_id,l.unit_key,l.payload FROM latest l JOIN unit_turns u "
            "ON l.producer_id=u.producer_id AND l.unit_key=u.unit_key "
            "WHERE u.session_id=? AND u.harness=? AND u.physical=? AND u.turn_id=?",
            (*scope, turn),
        ).fetchall()
        for producer, unit, value in rows:
            self._enqueue(
                db,
                producer,
                unit,
                {**json.loads(value), "terminal": [proof]},
                (value,),
            )

    def record_thread_end(
        self,
        session_id: str,
        harness: str,
        physical_session_id: str,
        child: str | None,
        source_ref: str,
    ) -> None:
        scope = (session_id, harness, self._json([physical_session_id, child]))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT turn_id FROM capture_turns WHERE session_id=? "
                "AND harness=? AND physical=?",
                scope,
            ).fetchone()
            if row:
                self._record_terminal(
                    db,
                    scope,
                    row[0],
                    {
                        "physical_session_id": physical_session_id,
                        "physical_subagent_id": child,
                        "evidence": "stop_hook",
                        "source_ref": source_ref,
                    },
                    str(time.time()),
                )

    def append(
        self,
        session_id: str,
        harness: str,
        physical: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if harness == "claude-sdk":
                parent = physical.get("physical_session_id")
                child = physical.get("physical_subagent_id")
                if (
                    not isinstance(parent, str)
                    or not parent
                    or (child is not None and not isinstance(child, str))
                ):
                    raise ValueError("missing_physical_identity")
                proof = db.execute(
                    "SELECT path,conflicted FROM claude_path_proofs WHERE "
                    "session_id=? AND harness=? AND physical_session_id=? AND "
                    "physical_subagent_id=?",
                    (session_id, harness, parent, child or ""),
                ).fetchone()
                physical = {
                    **physical,
                    "physical_transcript_path": proof[0] if proof and not proof[1] else None,
                }
            self._observe(db, session_id, harness, physical, record)

    def bind_claude_path(
        self,
        session_id: str,
        physical_session_id: str,
        physical_subagent_id: str | None,
        path: str | None,
    ) -> None:
        """Persist hook path evidence and release earlier metering-only SDK rows."""
        if path is not None:
            try:
                source = Path(path)
                if not source.is_absolute() or not source.is_file():
                    return
                if str(source.resolve(strict=True)) != path or len(path.encode("utf-8")) > 1024:
                    return
                physical = claude_physical_identity(source)
            except (OSError, ValueError):
                return
            if (
                physical["physical_session_id"] != physical_session_id
                or physical["physical_subagent_id"] != physical_subagent_id
            ):
                path = None
        child = physical_subagent_id or ""
        scope = (session_id, "claude-sdk", physical_session_id, child)
        with self._proof_lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT path,conflicted FROM claude_path_proofs WHERE "
                "session_id=? AND harness=? AND physical_session_id=? AND physical_subagent_id=?",
                scope,
            ).fetchone()
            conflicted = path is None or (
                current is not None and (current[1] or current[0] != path)
            )
            if current is None:
                db.execute(
                    "INSERT INTO claude_path_proofs VALUES (?,?,?,?,?,?)",
                    (*scope, path if not conflicted else None, int(conflicted)),
                )
            elif conflicted:
                db.execute(
                    "UPDATE claude_path_proofs SET path=NULL,conflicted=1 WHERE "
                    "session_id=? AND harness=? AND physical_session_id=? AND "
                    "physical_subagent_id=?",
                    scope,
                )
            if conflicted:
                return
            db.execute(
                "UPDATE deferred_measurements SET payload=json_set(payload,"
                "'$.physical_transcript_path',?) WHERE session_id=? AND harness='claude-sdk' "
                "AND physical=?",
                (path, session_id, self._json([physical_session_id, physical_subagent_id])),
            )
            producer = db.execute(
                "SELECT producer_id FROM producers WHERE session_id=? AND harness='claude-sdk'",
                (session_id,),
            ).fetchone()
            if producer is None:
                return
            for table, keys in (("observations", "sequence"), ("latest", "producer_id,unit_key")):
                rows = db.execute(
                    f"SELECT {keys},payload FROM {table} WHERE producer_id=? AND "
                    "json_extract(payload,'$.physical_session_id')=? AND "
                    "coalesce(json_extract(payload,'$.physical_subagent_id'),'')=? AND "
                    "json_extract(payload,'$.physical_transcript_path') IS NULL",
                    (producer[0], physical_session_id, child),
                ).fetchall()
                for row in rows:
                    payload = json.loads(row[-1])
                    payload["physical_transcript_path"] = path
                    if table == "observations":
                        db.execute(
                            "UPDATE observations SET payload=? WHERE sequence=?",
                            (self._json(payload), row[0]),
                        )
                    else:
                        db.execute(
                            "UPDATE latest SET payload=? WHERE producer_id=? AND unit_key=?",
                            (self._json(payload), row[0], row[1]),
                        )

    def link_admission(
        self,
        session_id: str,
        harness: str,
        physical_session_id: str,
        physical_subagent_id: str | None,
        turn_key: str,
        admission_id: str,
        *,
        route_check: str | None = None,
    ) -> None:
        """Record the admitted turn before its first observation is enqueued."""
        if not isinstance(admission_id, str) or not admission_id:
            raise ValueError("missing_admission_id")
        physical = self._json([physical_session_id, physical_subagent_id])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._link_in_transaction(
                db, session_id, harness, physical, turn_key, admission_id, route_check
            )

    def _link_in_transaction(
        self,
        db: sqlite3.Connection,
        session_id: str,
        harness: str,
        physical: str,
        turn_key: str,
        admission_id: str,
        route_check: str | None,
    ) -> None:
        existing = db.execute(
            "SELECT admission_id FROM admission_links "
            "WHERE session_id=? AND harness=? AND physical=? AND turn_key=?",
            (session_id, harness, physical, turn_key),
        ).fetchone()
        if existing is not None:
            if existing[0] != admission_id:
                raise ValueError("admission_reused")
            return
        db.execute(
            "INSERT INTO admission_links VALUES (?,?,?,?,?,?)",
            (session_id, harness, physical, turn_key, admission_id, route_check),
        )
        for producer, unit, value in db.execute(
            "SELECT l.producer_id,l.unit_key,l.payload FROM latest l JOIN unit_turns u "
            "ON l.producer_id=u.producer_id AND l.unit_key=u.unit_key "
            "WHERE u.session_id=? AND u.harness=? AND u.physical=? AND u.turn_id=?",
            (session_id, harness, physical, turn_key),
        ).fetchall():
            observation = json.loads(value)
            if db.execute(
                "SELECT 1 FROM server_acks WHERE producer_id=? AND producer_unit_id=?",
                (producer, observation["producer_unit_id"]),
            ).fetchone():
                raise ValueError("admission_link_after_ack")
            for sequence, raw in db.execute(
                "SELECT sequence,payload FROM observations WHERE producer_id=? AND "
                "json_extract(payload,'$.producer_unit_id')=?",
                (producer, observation["producer_unit_id"]),
            ).fetchall():
                payload = {
                    **json.loads(raw),
                    "admission_id": admission_id,
                    "route_check": route_check,
                }
                db.execute(
                    "UPDATE observations SET payload=? WHERE sequence=?",
                    (self._json(payload), sequence),
                )
            observation.update(admission_id=admission_id, route_check=route_check)
            db.execute(
                "UPDATE latest SET payload=? WHERE producer_id=? AND unit_key=?",
                (self._json(observation), producer, unit),
            )

    def begin_admission(self, session_id: str, harness: str, admission: Mapping[str, Any]) -> None:
        """Fence delivery while a turn/start response has not supplied its turn id."""
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending_admissions VALUES (?,?,?)",
                (session_id, harness, self._json(dict(admission))),
            )

    def record_live_route(self, session_id: str, harness: str, route: dict[str, str]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO runtime_routes VALUES (?,?,?)",
                (session_id, harness, self._json(route)),
            )
            row = db.execute(
                "SELECT admission FROM pending_admissions WHERE session_id=? AND harness=?",
                (session_id, harness),
            ).fetchone()
            if row:
                admission = json.loads(row[0])
                admission["route_check"] = (
                    "match" if admission.get("route") == route else "mismatch"
                )
                db.execute(
                    "UPDATE pending_admissions SET admission=? WHERE session_id=? AND harness=?",
                    (self._json(admission), session_id, harness),
                )

    def live_routes(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [
                {
                    "harness": harness,
                    "host_id": json.loads(binding)["host_id"],
                    **json.loads(route),
                }
                for harness, binding, route in db.execute(
                    "SELECT p.harness,p.binding,r.route FROM runtime_routes r JOIN producers p "
                    "ON p.session_id=r.session_id AND p.harness=r.harness "
                    "WHERE p.binding IS NOT NULL"
                )
            ]

    def session_route(self, session_id: str, harness: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT p.binding,r.route FROM runtime_routes r JOIN producers p "
                "ON p.session_id=r.session_id AND p.harness=r.harness "
                "WHERE r.session_id=? AND r.harness=? AND p.binding IS NOT NULL",
                (session_id, harness),
            ).fetchone()
            return (
                {
                    "host_id": json.loads(row[0])["host_id"],
                    "harness": harness,
                    **json.loads(row[1]),
                }
                if row
                else None
            )

    def pending_admission(self, session_id: str, harness: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT admission FROM pending_admissions WHERE session_id=? AND harness=?",
                (session_id, harness),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def finish_admission(self, session_id: str, harness: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM pending_admissions WHERE session_id=? AND harness=?",
                (session_id, harness),
            )

    def end_admitted_turn(self, session_id: str, harness: str, admission_id: str) -> None:
        """Executor completion closes its main physical turn; children need their own proof."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            turns = db.execute(
                "SELECT DISTINCT u.physical,u.turn_id FROM unit_turns u "
                "JOIN admission_links a ON a.session_id=u.session_id AND a.harness=u.harness "
                "AND a.physical=u.physical AND a.turn_key=u.turn_id "
                "JOIN latest l ON l.producer_id=u.producer_id AND l.unit_key=u.unit_key "
                "WHERE a.session_id=? AND a.harness=? AND a.admission_id=? "
                "AND json_extract(l.payload,'$.thread_kind')='main'",
                (session_id, harness, admission_id),
            ).fetchall()
            for physical, turn in turns:
                parent, child = json.loads(physical)
                self._record_terminal(
                    db,
                    (session_id, harness, physical),
                    turn,
                    {
                        "physical_session_id": parent,
                        "physical_subagent_id": child,
                        "evidence": "executor_end",
                        "source_ref": admission_id,
                    },
                    str(time.time()),
                )
            db.execute(
                "DELETE FROM pending_admissions WHERE session_id=? AND harness=?",
                (session_id, harness),
            )

    def admission_for_turn(
        self,
        session_id: str,
        harness: str,
        physical_session_id: str,
        physical_subagent_id: str | None,
        turn_key: str,
    ) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT admission_id FROM admission_links "
                "WHERE session_id=? AND harness=? AND physical=? AND turn_key=?",
                (
                    session_id,
                    harness,
                    self._json([physical_session_id, physical_subagent_id]),
                    turn_key,
                ),
            ).fetchone()
            return row[0] if row is not None else None

    def register_source(
        self,
        path: Path,
        session_id: str,
        harness: str,
        physical: Mapping[str, Any],
        *,
        start_offset: int = 0,
    ) -> None:
        """Register a proven boundary once; reconnect never resets its cursor."""
        stat = path.stat()
        if not 0 <= start_offset <= stat.st_size:
            raise ValueError("invalid_source_boundary")
        identity = self._json([stat.st_dev, stat.st_ino])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT session_id,harness,identity FROM sources WHERE path=?", (str(path),)
            ).fetchone()
            if current is not None:
                if current != (session_id, harness, identity):
                    raise ValueError("usage_source_changed")
                return
            db.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?)",
                (
                    str(path),
                    session_id,
                    harness,
                    start_offset,
                    identity,
                    self._json(dict(physical)),
                ),
            )

    def scan(self, path: Path, *, final: bool = False) -> None:
        """Atomically checkpoint complete JSONL lines with their observations."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT session_id,harness,offset,identity,metadata FROM sources WHERE path=?",
                (str(path),),
            ).fetchone()
            if source is None:
                raise ValueError("usage_source_unregistered")
            session_id, harness, offset, identity, metadata = source
            physical = json.loads(metadata)
            with path.open("rb") as transcript:
                stat = path.stat()
                if self._json([stat.st_dev, stat.st_ino]) != identity or stat.st_size < offset:
                    raise ValueError("usage_source_changed")
                transcript.seek(offset)
                lines = 0
                while final or lines < 1000:
                    line = transcript.readline(16 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 16 * 1024 * 1024:
                        raise ValueError("usage_source_line_too_large")
                    if not line.endswith(b"\n") and not final:
                        break
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("invalid_usage_source_record")
                    payload = record.get("payload")
                    if harness == "pi-native" and record.get("type") == "session":
                        physical["physical_session_id"] = record.get("id")
                        physical["upstream_thread_id"] = record.get("id")
                    if harness.startswith("codex-") and isinstance(payload, dict):
                        if record.get("type") == "session_meta":
                            physical["physical_session_id"] = payload.get("id")
                            physical["upstream_thread_id"] = payload.get("id")
                        elif record.get("type") == "turn_context":
                            physical["model_id"] = payload.get("model")
                    # Clone history retains the original thread qualification.
                    inherited = (
                        harness.startswith("codex-")
                        and record.get("type") == "token_usage_record"
                        and isinstance(payload, dict)
                        and payload.get("thread_id") != physical.get("physical_session_id")
                    )
                    if not inherited:
                        self._observe(
                            db,
                            session_id,
                            harness,
                            physical,
                            record,
                            (str(path), transcript.tell()),
                        )
                    offset = transcript.tell()
                    lines += 1
            db.execute(
                "UPDATE sources SET offset=?,metadata=? WHERE path=?",
                (offset, self._json(physical), str(path)),
            )

    def register_root(
        self, path: Path, session_id: str, harness: str, main_thread: str | None
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO capture_roots VALUES (?,?,?,?)",
                (str(path), session_id, harness, main_thread),
            )

    def checkpoint(self, host_id: str, harness: str, generation: int) -> dict[str, int]:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO cutover_checkpoints VALUES (?,?,?,?)",
                (
                    host_id,
                    harness,
                    generation,
                    self._json(
                        dict(
                            db.execute(
                                "SELECT path,offset FROM sources WHERE harness=?", (harness,)
                            )
                        )
                    ),
                ),
            )
            return json.loads(
                db.execute(
                    "SELECT sources FROM cutover_checkpoints WHERE host_id=? "
                    "AND harness=? AND generation=?",
                    (host_id, harness, generation),
                ).fetchone()[0]
            )

    def scan_registered_sources(self, *, harness: str | None = None, final: bool = False) -> None:
        if harness in {None, "pi-native"}:
            with self._connect() as db:
                pi_roots = list(db.execute("SELECT path,session_id FROM pi_roots"))
            for root, session_id in pi_roots:
                self._discover_pi_sources(Path(root), session_id, baseline=False)
        with self._connect() as db:
            roots = db.execute(
                "SELECT path,session_id,harness,main_thread FROM capture_roots "
                "WHERE ? IS NULL OR harness=?",
                (harness, harness),
            ).fetchall()
            claude = db.execute(
                "SELECT path,session_id,harness,metadata FROM sources "
                "WHERE harness LIKE 'claude-%' AND (? IS NULL OR harness=?)",
                (harness, harness),
            ).fetchall()
        from omnigent.usage_export.runtime import capture_codex_home

        for root, session_id, source_harness, main_thread in roots:
            capture_codex_home(
                Path(root),
                session_id,
                source_harness,
                main_thread,
                final=final,
                registered_scan=True,
            )
        for raw_path, session_id, source_harness, metadata in claude:
            if json.loads(metadata).get("physical_subagent_id") is not None:
                continue
            path = Path(raw_path)
            for child in (path.parent / path.stem / "subagents").glob("agent-*.jsonl"):
                if not self.has_source(child):
                    self.register_source(
                        child, session_id, source_harness, claude_physical_identity(child)
                    )
        with self._connect() as db:
            sources = [
                Path(row[0])
                for row in db.execute(
                    "SELECT path FROM sources WHERE ? IS NULL OR harness=?", (harness, harness)
                )
            ]
        for path in sources:
            if path.is_file():
                self.scan(path, final=final)

    def register_pi_root(self, directory: Path, session_id: str) -> None:
        """Freeze inherited files only on first registration; retain reconnect cursors."""
        with self._connect() as db:
            row = db.execute(
                "SELECT session_id FROM pi_roots WHERE path=?", (str(directory),)
            ).fetchone()
        if row is not None:
            if row[0] != session_id:
                raise ValueError("pi_usage_root_changed")
            self._discover_pi_sources(directory, session_id, baseline=False, rebase=True)
            return
        self._discover_pi_sources(directory, session_id, baseline=True, rebase=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO pi_roots VALUES (?,?)", (str(directory), session_id))

    def _discover_pi_sources(
        self, directory: Path, session_id: str, *, baseline: bool, rebase: bool = False
    ) -> None:
        if not directory.is_dir():
            return
        for path in directory.glob("*.jsonl"):
            if not path.is_file():
                continue
            if self.has_source(path):
                if not rebase:
                    continue
                stat = path.stat()
                with self._connect() as db:
                    current = db.execute(
                        "SELECT identity,offset,metadata FROM sources WHERE path=?",
                        (str(path),),
                    ).fetchone()
                identity = self._json([stat.st_dev, stat.st_ino])
                if current[0] != identity or current[1] > stat.st_size:
                    offset = self._remember_pi_baseline(path, session_id)
                    metadata = json.loads(current[2])
                    physical_id = self._pi_header_id(path)
                    metadata["physical_session_id"] = physical_id
                    metadata["upstream_thread_id"] = physical_id
                    with self._connect() as db:
                        db.execute(
                            "UPDATE sources SET identity=?,offset=?,metadata=? WHERE path=?",
                            (identity, offset, self._json(metadata), str(path)),
                        )
                continue
            physical = {
                "harness_family": "pi",
                "identity_namespace": "pi/session/v1",
                "physical_transcript_path": str(path.resolve()),
                "thread_kind": "main",
            }
            if baseline:
                physical_id = self._pi_header_id(path)
                physical["physical_session_id"] = physical_id
                physical["upstream_thread_id"] = physical_id
                offset = self._remember_pi_baseline(path, session_id)
            else:
                offset = 0
            self.register_source(
                path,
                session_id,
                "pi-native",
                physical,
                start_offset=offset,
            )

    @staticmethod
    def _pi_header_id(path: Path) -> str:
        with path.open("rb") as transcript:
            header = json.loads(transcript.readline(1024 * 1024))
        if (
            not isinstance(header, dict)
            or header.get("type") != "session"
            or not isinstance(header.get("id"), str)
            or not header["id"]
        ):
            raise ValueError("missing_physical_identity")
        return header["id"]

    def _remember_pi_baseline(self, path: Path, session_id: str) -> int:
        """Exclude inherited usage entries later copied into a Pi branch file."""
        with path.open("rb") as transcript, self._connect() as db:
            while True:
                line = transcript.readline(16 * 1024 * 1024 + 1)
                if not line or not line.endswith(b"\n"):
                    break
                if len(line) > 16 * 1024 * 1024:
                    raise ValueError("usage_source_line_too_large")
                record = json.loads(line)
                if isinstance(record, dict) and pi_measurement(record) is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO pi_seen VALUES (?,?)",
                        (session_id, self._json([record["id"], record.get("timestamp")])),
                    )
            return transcript.tell() - (len(line) if line and not line.endswith(b"\n") else 0)

    def has_source(self, path: Path) -> bool:
        with self._connect() as db:
            return (
                db.execute("SELECT 1 FROM sources WHERE path=?", (str(path),)).fetchone()
                is not None
            )

    async def prepare(self, client: httpx.AsyncClient, session_id: str, harness: str) -> None:
        """Persist admission while the session still exists, before harness launch."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO producers VALUES (?, ?, ?, NULL)",
                (session_id, harness, uuid.uuid4().hex),
            )
            producer_id, binding = db.execute(
                "SELECT producer_id,binding FROM producers WHERE session_id=? AND harness=?",
                (session_id, harness),
            ).fetchone()
        if binding is not None:
            return
        response = await client.post(
            "/v1/usage-producers",
            json={"producer_id": producer_id, "session_id": session_id, "harness": harness},
            timeout=30,
        )
        response.raise_for_status()
        binding = response.json()
        if (
            set(binding)
            != {"producer_id", "session_id", "agent_id", "host_id", "harness", "parent_session_id"}
            or binding["producer_id"] != producer_id
            or binding["session_id"] != session_id
            or binding["harness"] != harness
        ):
            raise ValueError("usage_producer_binding_conflict")
        with self._connect() as db:
            db.execute(
                "UPDATE producers SET binding=? WHERE producer_id=?",
                (self._json(binding), producer_id),
            )

    def domain_sources(self, harness: str) -> list[Path]:
        with self._connect() as db:
            return [
                Path(row[0])
                for row in db.execute("SELECT path FROM sources WHERE harness=?", (harness,))
            ]

    def mark_capture(self, session_id: str, harness: str) -> None:
        import psutil

        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO capture_runs VALUES (?,?,?,?,?)",
                (session_id, harness, os.getpid(), psutil.Process().create_time(), time.time()),
            )

    def domain_report(self, harness: str) -> dict[str, Any]:
        """What the domain still holds locally: pending rows and open threads."""
        with self._connect() as db:
            pending = db.execute(
                "SELECT count(*) FROM observations o JOIN producers p "
                "ON p.producer_id=o.producer_id WHERE p.harness=?",
                (harness,),
            ).fetchone()[0]
            pending += db.execute(
                "SELECT count(*) FROM deferred_measurements WHERE harness=?", (harness,)
            ).fetchone()[0]
            open_threads = db.execute(
                "SELECT count(*) FROM unit_turns u JOIN producers p "
                "ON p.producer_id=u.producer_id WHERE p.harness=? AND NOT EXISTS ("
                "SELECT 1 FROM terminal_receipts t WHERE t.session_id=u.session_id "
                "AND t.harness=u.harness AND t.physical=u.physical "
                "AND t.turn_id=u.turn_id)",
                (harness,),
            ).fetchone()[0]
            units = [
                {
                    **{
                        key: value
                        for key, value in json.loads(row[2] or "{}").items()
                        if key != "producer_id"
                    },
                    **json.loads(row[0]),
                    "server_ack": json.loads(row[1]) if row[1] else None,
                    "origin": {"path": row[3], "position": row[4]} if row[3] else None,
                }
                for row in db.execute(
                    "SELECT l.payload,a.event,p.binding,u.path,u.position FROM latest l "
                    "JOIN producers p "
                    "ON p.producer_id=l.producer_id LEFT JOIN server_acks a "
                    "ON a.producer_id=l.producer_id AND a.producer_unit_id="
                    "json_extract(l.payload,'$.producer_unit_id') "
                    "AND a.observation=l.payload LEFT JOIN unit_origins u "
                    "ON u.producer_id=l.producer_id AND u.unit_key=l.unit_key WHERE p.harness=?",
                    (harness,),
                )
            ]
            sources = [
                {"path": row[0], "offset": row[1], "session_id": row[2]}
                for row in db.execute(
                    "SELECT path,offset,session_id FROM sources WHERE harness=?", (harness,)
                )
            ]
            producers = [
                {"session_id": row[0], "binding": json.loads(row[1]) if row[1] else None}
                for row in db.execute(
                    "SELECT session_id,binding FROM producers WHERE harness=?", (harness,)
                )
            ]
            capture_runs = db.execute(
                "SELECT session_id,pid,process_started,capture_started FROM capture_runs "
                "WHERE harness=?",
                (harness,),
            ).fetchall()
        import psutil

        live_capture: dict[str, float] = {}
        for session, pid, started, capture_started in capture_runs:
            try:
                if psutil.Process(pid).create_time() == started:
                    live_capture[session] = min(
                        live_capture.get(session, capture_started), capture_started
                    )
            except psutil.Error:
                continue
        return {
            "pending_observations": int(pending),
            "open_threads": int(open_threads),
            "units": units,
            "sources": sources,
            "producers": producers,
            "live_capture": live_capture,
        }

    async def drain(
        self, client: httpx.AsyncClient, *, limit: int = 100, harness: str | None = None
    ) -> None:
        """ACK in FIFO order; a lost ACK resends the identical measurement."""
        try:
            self._drain_lock.acquire(timeout=0)
        except Timeout:
            return
        try:
            self._resolve_children(harness)
            self._flush_deferred(harness)
            for _ in range(limit):
                with self._connect() as db:
                    row = db.execute(
                        "SELECT o.sequence,o.producer_id,o.payload FROM observations o "
                        "JOIN producers p ON p.producer_id=o.producer_id "
                        "WHERE (? IS NULL OR p.harness=?) AND (p.harness!='claude-sdk' OR EXISTS ("
                        "SELECT 1 FROM claude_path_proofs c WHERE "
                        "c.session_id=p.session_id AND c.harness=p.harness AND "
                        "c.physical_session_id=json_extract("
                        "o.payload,'$.physical_session_id') AND "
                        "c.physical_subagent_id=coalesce(json_extract("
                        "o.payload,'$.physical_subagent_id'),'') AND c.conflicted=0 AND "
                        "c.path=json_extract(o.payload,'$.physical_transcript_path'))) "
                        "AND NOT EXISTS (SELECT 1 FROM pending_admissions a "
                        "WHERE a.session_id=p.session_id AND a.harness=p.harness) "
                        "AND NOT EXISTS (SELECT 1 FROM physical_children c "
                        "WHERE c.session_id=p.session_id AND c.harness=p.harness "
                        "AND "
                        "c.physical=json_array(json_extract(o.payload,'$.physical_session_id'),"
                        " "
                        "json_extract(o.payload,'$.physical_subagent_id')) AND c.resolved=0) "
                        "ORDER BY o.sequence LIMIT 1",
                        (harness, harness),
                    ).fetchone()
                    if row is None:
                        return
                    sequence, producer_id, payload = row
                    session_id, harness, binding_text = db.execute(
                        "SELECT session_id,harness,binding FROM producers WHERE producer_id=?",
                        (producer_id,),
                    ).fetchone()
                if binding_text is None:
                    response = await client.post(
                        "/v1/usage-producers",
                        json={
                            "producer_id": producer_id,
                            "session_id": session_id,
                            "harness": harness,
                        },
                        timeout=30,
                    )
                    response.raise_for_status()
                    binding = response.json()
                    with self._connect() as db:
                        db.execute(
                            "UPDATE producers SET binding=? WHERE producer_id=?",
                            (self._json(binding), producer_id),
                        )
                else:
                    binding = json.loads(binding_text)
                if harness == "claude-sdk":
                    try:
                        self._proof_lock.acquire(timeout=0)
                    except Timeout:
                        return
                try:
                    if harness == "claude-sdk":
                        with self._connect() as db:
                            current = db.execute(
                                "SELECT payload FROM observations WHERE sequence=?", (sequence,)
                            ).fetchone()
                            if current is None or current[0] != payload:
                                continue
                            physical = json.loads(payload)
                            proof = db.execute(
                                "SELECT path,conflicted FROM claude_path_proofs WHERE "
                                "session_id=? AND harness=? AND physical_session_id=? AND "
                                "physical_subagent_id=?",
                                (
                                    session_id,
                                    harness,
                                    physical["physical_session_id"],
                                    physical.get("physical_subagent_id") or "",
                                ),
                            ).fetchone()
                            if (
                                proof is None
                                or proof[1]
                                or proof[0] != physical.get("physical_transcript_path")
                            ):
                                continue
                    observation = {
                        "time_quality": "reported",
                        "cost_usd": None,
                        "cost_basis": "unknown",
                        "cost_estimated": None,
                        **{key: value for key, value in binding.items() if key != "producer_id"},
                        **json.loads(payload),
                    }
                    if observation.get("occurred_at") is None:
                        observation["time_quality"] = "unknown"
                    response = await client.post(
                        "/v1/usage-observations",
                        json={"producer_id": producer_id, "observation": observation},
                        timeout=30,
                    )
                    response.raise_for_status()
                    event = response.json()
                    if not isinstance(event.get("event_id"), str):
                        raise ValueError("usage_ack_missing")
                    with self._connect() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO server_acks VALUES (?,?,?,?)",
                            (
                                producer_id,
                                json.loads(payload)["producer_unit_id"],
                                payload,
                                self._json(event),
                            ),
                        )
                        db.execute("DELETE FROM observations WHERE sequence=?", (sequence,))
                finally:
                    if harness == "claude-sdk":
                        self._proof_lock.release()
        finally:
            self._drain_lock.release()

    def _flush_deferred(self, harness: str | None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT session_id,harness,physical,turn_key,unit_key,payload,origin "
                "FROM deferred_measurements WHERE (? IS NULL OR harness=?)",
                (harness, harness),
            ).fetchall()
            for session, source_harness, physical, turn, key, value, origin in rows:
                scope = (session, source_harness)
                if (
                    db.execute(
                        "SELECT 1 FROM pending_admissions WHERE session_id=? AND harness=?",
                        scope,
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM physical_children WHERE session_id=? AND harness=? "
                        "AND physical=? AND resolved=0",
                        (*scope, physical),
                    ).fetchone()
                ):
                    continue
                observation = json.loads(value)
                link = db.execute(
                    "SELECT admission_id,route_check FROM admission_links WHERE session_id=? "
                    "AND harness=? AND physical=? AND turn_key=?",
                    (*scope, physical, turn),
                ).fetchone()
                if link is None:
                    inherited = db.execute(
                        "SELECT admission_id,route_check FROM physical_children WHERE "
                        "session_id=? "
                        "AND harness=? AND physical=? AND resolved=1",
                        (*scope, physical),
                    ).fetchone()
                    if inherited and inherited[0]:
                        link = inherited
                        db.execute(
                            "INSERT OR IGNORE INTO admission_links VALUES (?,?,?,?,?,?)",
                            (*scope, physical, turn, *link),
                        )
                if link:
                    observation.update(admission_id=link[0], route_check=link[1])
                proof = db.execute(
                    "SELECT proof FROM terminal_proofs WHERE session_id=? AND harness=? "
                    "AND physical=? AND turn_id=?",
                    (*scope, physical, turn),
                ).fetchone()
                if proof:
                    observation["terminal"] = [json.loads(proof[0])]
                producer, unit = self._append(db, session, source_harness, observation)
                db.execute(
                    "INSERT OR IGNORE INTO unit_turns VALUES (?,?,?,?,?,?)",
                    (*scope, physical, turn, producer, unit),
                )
                position = json.loads(origin)
                if position:
                    db.execute(
                        "INSERT OR IGNORE INTO unit_origins VALUES (?,?,?,?)",
                        (producer, unit, *position),
                    )
                db.execute(
                    "DELETE FROM deferred_measurements WHERE session_id=? AND harness=? "
                    "AND physical=? AND turn_key=? AND unit_key=?",
                    (*scope, physical, turn, key),
                )

    def _resolve_children(self, harness: str | None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            remaining = db.execute(
                "SELECT session_id,harness,physical,parent,born_at FROM physical_children "
                "WHERE resolved=0 AND (? IS NULL OR harness=?)",
                (harness, harness),
            ).fetchall()
            while remaining:
                progressed = False
                for session_id, source_harness, physical, parent, born in remaining[:]:
                    scope = (session_id, source_harness)
                    if db.execute(
                        "SELECT 1 FROM pending_admissions WHERE session_id=? AND harness=?", scope
                    ).fetchone():
                        continue
                    parent_child = db.execute(
                        "SELECT resolved,admission_id,route_check,born_at FROM "
                        "physical_children WHERE session_id=? AND harness=? AND physical=?",
                        (*scope, parent),
                    ).fetchone()
                    if parent_child is not None and not parent_child[0]:
                        continue
                    complete = True
                    for path, offset, metadata in db.execute(
                        "SELECT path,offset,metadata FROM sources WHERE session_id=? AND "
                        "harness=?",
                        scope,
                    ):
                        identity = json.loads(metadata)
                        if (
                            self._json(
                                [
                                    identity.get("physical_session_id"),
                                    identity.get("physical_subagent_id"),
                                ]
                            )
                            == parent
                        ):
                            if Path(path).is_file() and Path(path).stat().st_size > offset:
                                complete = False
                    if not complete:
                        continue
                    matches = []
                    if born is not None:
                        matches = db.execute(
                            "SELECT a.admission_id,a.route_check FROM physical_turn_spans s "
                            "JOIN admission_links a ON a.session_id=s.session_id AND "
                            "a.harness=s.harness "
                            "AND a.physical=s.physical AND a.turn_key=s.turn_key "
                            "WHERE s.session_id=? AND s.harness=? AND s.physical=? "
                            "AND s.started_at<=? AND (s.ended_at IS NULL OR s.ended_at>=?)",
                            (*scope, parent, born, born),
                        ).fetchall()
                        if (
                            not matches
                            and parent_child
                            and parent_child[1]
                            and parent_child[3] is not None
                            and parent_child[3] <= born
                        ):
                            matches = [(parent_child[1], parent_child[2])]
                    admission_id, route_check = matches[0] if len(matches) == 1 else (None, None)
                    if admission_id is not None:
                        turns = db.execute(
                            "SELECT DISTINCT turn_id FROM unit_turns WHERE session_id=? AND "
                            "harness=? AND physical=?",
                            (*scope, physical),
                        ).fetchall()
                        for (turn,) in turns:
                            self._link_in_transaction(
                                db, *scope, physical, turn, admission_id, route_check
                            )
                    db.execute(
                        "UPDATE physical_children SET resolved=1,admission_id=?,route_check=? "
                        "WHERE session_id=? AND harness=? AND physical=?",
                        (admission_id, route_check, *scope, physical),
                    )
                    remaining.remove((session_id, source_harness, physical, parent, born))
                    progressed = True
                if not progressed:
                    break
