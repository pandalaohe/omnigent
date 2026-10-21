"""A durable, metering-only spool outside disposable harness homes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock, Timeout

from omnigent.usage_export.models import UsageObservation
from omnigent.usage_export.transcripts import (
    claude_measurement,
    claude_physical_identity,
    codex_measurement,
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
    ) -> None:
        """Associate only explicit terminal records with their physical turn."""
        source = self._json(
            [physical.get("physical_session_id"), physical.get("physical_subagent_id")]
        )
        scope = (session_id, harness, source)
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
            turn = uuid.uuid4().hex
        if claude:
            db.execute("INSERT OR REPLACE INTO capture_turns VALUES (?,?,?,?)", (*scope, turn))
        payload = record.get("payload")
        if not claude:
            turn = payload.get("turn_id") if isinstance(payload, dict) else None
        measurement = claude_measurement(record) if claude else codex_measurement(record)
        if measurement is not None:
            observation = {**physical, **measurement}
            if observation.get("model_id") is None:
                observation["model_id"] = physical.get("model_id")
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
                    observation["terminal"] = [_terminal_evidence(physical)]
            producer, unit = self._append(db, session_id, harness, observation)
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
        db.execute(
            "INSERT OR IGNORE INTO terminal_receipts VALUES (?,?,?,?,?)", (*scope, turn, ended_at)
        )
        ended_at = db.execute(
            "SELECT ended_at FROM terminal_receipts "
            "WHERE session_id=? AND harness=? AND physical=? AND turn_id=?",
            (*scope, turn),
        ).fetchone()[0]
        rows = db.execute(
            "SELECT l.producer_id,l.unit_key,l.payload FROM latest l JOIN unit_turns u "
            "ON l.producer_id=u.producer_id AND l.unit_key=u.unit_key "
            "WHERE u.session_id=? AND u.harness=? AND u.physical=? AND u.turn_id=?",
            (*scope, turn),
        ).fetchall()
        del ended_at
        for producer, unit, value in rows:
            self._enqueue(
                db,
                producer,
                unit,
                {**json.loads(value), "terminal": [_terminal_evidence(physical)]},
                (value,),
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
                        self._observe(db, session_id, harness, physical, record)
                    offset = transcript.tell()
                    lines += 1
            db.execute(
                "UPDATE sources SET offset=?,metadata=? WHERE path=?",
                (offset, self._json(physical), str(path)),
            )

    def scan_registered_sources(self) -> None:
        with self._connect() as db:
            sources = [Path(row[0]) for row in db.execute("SELECT path FROM sources")]
        for path in sources:
            if path.is_file():
                self.scan(path)

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

    def domain_report(self, harness: str) -> dict[str, Any]:
        """What the domain still holds locally: pending rows and open threads."""
        with self._connect() as db:
            pending = db.execute(
                "SELECT count(*) FROM observations o JOIN producers p "
                "ON p.producer_id=o.producer_id WHERE p.harness=?",
                (harness,),
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
                json.loads(row[0])
                for row in db.execute(
                    "SELECT l.payload FROM latest l JOIN producers p "
                    "ON p.producer_id=l.producer_id WHERE p.harness=?",
                    (harness,),
                )
            ]
            sources = [
                {"path": row[0], "offset": row[1]}
                for row in db.execute(
                    "SELECT path,offset FROM sources WHERE harness=?", (harness,)
                )
            ]
        return {
            "pending_observations": int(pending),
            "open_threads": int(open_threads),
            "units": units,
            "sources": sources,
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
                    if not isinstance(response.json().get("event_id"), str):
                        raise ValueError("usage_ack_missing")
                    with self._connect() as db:
                        db.execute("DELETE FROM observations WHERE sequence=?", (sequence,))
                finally:
                    if harness == "claude-sdk":
                        self._proof_lock.release()
        finally:
            self._drain_lock.release()
