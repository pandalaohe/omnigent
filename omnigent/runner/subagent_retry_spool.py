"""Durable delivery candidates; replay never starts another child attempt."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities.subagent_retry import RetryChain, retry_identity
from omnigent.entities.subagent_terminal import TerminalEnvelope


class RetryFinalIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_id: str
    terminal: TerminalEnvelope
    chain: RetryChain | None = None
    reason: str
    output: str | None
    agent: str
    title: str
    work_id: str
    limit: int = Field(default=3, strict=True, ge=1)

    @property
    def notice_id(self) -> str:
        return retry_identity("subagent-final", self.chain_id)


class RetryFinalSpool:
    """SQLite FULL commits retain a candidate until the server final receipt."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Refusing symlinked retry spool")
        try:
            db = sqlite3.connect(self.path, timeout=5)
        except sqlite3.Error as exc:
            raise OSError("Retry final spool is unavailable") from exc
        try:
            with db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS final_intents "
                    "(chain_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, "
                    "notice_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "acknowledged INTEGER NOT NULL DEFAULT 0)"
                )
                yield db
        except sqlite3.Error as exc:
            raise OSError("Retry final spool is unavailable") from exc
        finally:
            db.close()

    def put(self, intent: RetryFinalIntent) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT parent_id,payload,acknowledged FROM final_intents WHERE chain_id=?",
                (intent.chain_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO final_intents (chain_id,parent_id,notice_id,payload) "
                    "VALUES (?,?,?,?)",
                    (
                        intent.chain_id,
                        intent.parent_id,
                        intent.notice_id,
                        intent.model_dump_json(),
                    ),
                )
                return
            if row[2] or row[0] != intent.parent_id:
                return
            previous = RetryFinalIntent.model_validate_json(row[1])
            old, new = previous.terminal, intent.terminal
            if old.child_id != new.child_id:
                raise ValueError("Retry final candidate changed child identity")
            rank = {"failed": 0, "cancelled": 1, "stopped": 1, "killed": 1, "completed": 2}
            newer_input = new.input_generation is not None and (
                old.input_generation is None or new.input_generation > old.input_generation
            )
            same_input = (new.input_id, new.input_generation) == (
                old.input_id,
                old.input_generation,
            )
            if newer_input or (same_input and rank[new.status] > rank[old.status]):
                merged = previous.model_copy(update={"terminal": new, "output": intent.output})
                db.execute(
                    "UPDATE final_intents SET payload=? WHERE chain_id=? AND acknowledged=0",
                    (merged.model_dump_json(), intent.chain_id),
                )

    def check(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA user_version=1")

    def pending(self, parent_id: str) -> list[RetryFinalIntent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM final_intents WHERE parent_id=? AND acknowledged=0 "
                "ORDER BY chain_id",
                (parent_id,),
            ).fetchall()
        return [RetryFinalIntent.model_validate_json(row[0]) for row in rows]

    def parents(self) -> set[str]:
        with self._connect() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT parent_id FROM final_intents WHERE acknowledged=0"
                )
            }

    def acknowledge(self, chain_id: str, notice_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE final_intents SET acknowledged=1 WHERE chain_id=? AND notice_id=?",
                (chain_id, notice_id),
            )

    def transfer_parent(self, old_id: str, new_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for chain_id, raw in db.execute(
                "SELECT chain_id,payload FROM final_intents WHERE parent_id=? AND acknowledged=0",
                (old_id,),
            ).fetchall():
                intent = RetryFinalIntent.model_validate_json(raw)
                moved = intent.model_copy(
                    update={
                        "parent_id": new_id,
                        "terminal": intent.terminal.model_copy(update={"parent_id": new_id}),
                    }
                )
                db.execute(
                    "UPDATE final_intents SET parent_id=?,payload=? "
                    "WHERE chain_id=? AND acknowledged=0",
                    (new_id, moved.model_dump_json(), chain_id),
                )
