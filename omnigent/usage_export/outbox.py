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
from omnigent.usage_export.transcripts import claude_measurement, codex_measurement, pi_measurement


class UsageOutbox:
    """Commit observed counters before ACK or private-home removal.

    A caller owns the directory and provides only transcripts it controls.
    No background pruning or storage of prompts, headers, or credentials.
    """

    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "outbox.sqlite3"
        self._drain_lock = FileLock(str(directory / "drain.lock"), mode=0o600)
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
                CREATE TABLE IF NOT EXISTS pi_roots (
                    path TEXT PRIMARY KEY, session_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pi_seen (
                    session_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, entry_id)
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
                **{key: value for key, value in observation.items() if value is not None},
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
        if harness in {"pi-native", "pi"}:
            measurement = pi_measurement(record)
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
            self._append(db, session_id, harness, {**physical, **measurement})
            return
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
                receipt = db.execute(
                    "SELECT ended_at FROM terminal_receipts "
                    "WHERE session_id=? AND harness=? AND physical=? AND turn_id=?",
                    (*scope, turn),
                ).fetchone()
                if receipt is not None:
                    observation["ended_at"] = receipt[0]
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
        for producer, unit, value in rows:
            self._enqueue(
                db, producer, unit, {**json.loads(value), "ended_at": ended_at}, (value,)
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
            self._observe(db, session_id, harness, physical, record)

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
                        self._observe(db, session_id, harness, physical, record)
                    offset = transcript.tell()
                    lines += 1
            db.execute(
                "UPDATE sources SET offset=?,metadata=? WHERE path=?",
                (offset, self._json(physical), str(path)),
            )

    def scan_registered_sources(self) -> None:
        with self._connect() as db:
            pi_roots = list(db.execute("SELECT path,session_id FROM pi_roots"))
        for root, session_id in pi_roots:
            self._discover_pi_sources(Path(root), session_id, baseline=False)
        with self._connect() as db:
            sources = [Path(row[0]) for row in db.execute("SELECT path FROM sources")]
        for path in sources:
            if path.is_file():
                self.scan(path)

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

    async def drain(self, client: httpx.AsyncClient, *, limit: int = 100) -> None:
        """ACK in FIFO order; a lost ACK resends the identical measurement."""
        try:
            self._drain_lock.acquire(timeout=0)
        except Timeout:
            return
        try:
            for _ in range(limit):
                with self._connect() as db:
                    row = db.execute(
                        "SELECT sequence,producer_id,payload FROM observations "
                        "ORDER BY sequence LIMIT 1"
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
                observation = {
                    "ended_at": None,
                    "time_quality": "reported",
                    "cost_usd": None,
                    "cost_basis": "unknown",
                    "cost_estimated": None,
                    "account_hint": None,
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
            self._drain_lock.release()
