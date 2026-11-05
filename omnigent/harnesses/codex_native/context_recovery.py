"""Restore a child's current assignment after a native no-summary window reset."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.harnesses.claude_native.bridge import url_component
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
    is_compaction_steer_refusal,
)
from omnigent.runtime.prompt import CODEX_CHILD_CONTEXT_RECOVERY_INSTRUCTION
from omnigent.util.atomic_json import atomic_write_json

_logger = logging.getLogger(__name__)
_NOTICE_PREFIX = "[Omnigent context recovery]"


def _current_assignment(
    rollout: Path, turn_id: str, pending: tuple[str, str] | None = None
) -> tuple[str, str, list[str]] | None:
    """Read this thread's latest assignment and pending-window steering in order."""
    boundary = None
    later: list[dict] = []
    with rollout.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "compacted":
                boundary = record.get("payload")
                later = []
            elif record.get("type") == "response_item":
                payload = record.get("payload")
                if isinstance(payload, dict) and payload.get("role") == "user":
                    later.append(payload)
    if not isinstance(boundary, dict) or boundary.get("message") != "":
        return None
    window_id = boundary.get("window_id")
    resume = boundary.get("resume_metadata")
    retained = boundary.get("retained_context")
    if (
        not isinstance(window_id, str)
        or not window_id
        or not isinstance(resume, dict)
        or (
            resume.get("last_started_turn_id") != turn_id
            and (pending is None or pending[0] != window_id)
        )
        or not isinstance(retained, dict)
        or retained.get("user_messages_incomplete") is not False
    ):
        return None
    messages = retained.get("user_messages")
    if not isinstance(messages, list):
        return None
    user_turns = [
        message["turn_id"]
        for message in messages
        if isinstance(message, dict)
        and isinstance(message.get("turn_id"), str)
        and isinstance(message.get("text"), str)
        and not message["text"].startswith((_NOTICE_PREFIX, "[System:"))
    ]
    if not user_turns:
        return None
    # A manual compact turn carries no new assignment of its own.
    source_turn = pending[1] if pending and pending[0] == window_id else user_turns[-1]
    texts = [
        message["text"]
        for message in messages
        if isinstance(message, dict)
        and message.get("turn_id") == source_turn
        and isinstance(message.get("text"), str)
    ]
    for message in later:
        metadata = message.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("turn_id"), str):
            continue
        if not (pending and pending[0] == window_id) and metadata["turn_id"] not in {
            source_turn,
            turn_id,
        }:
            continue
        texts.extend(
            block["text"]
            for block in message.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    # Standing grants are refreshed by their own gate, never by a replayed header.
    texts = [
        "\n".join(
            line for line in text.splitlines() if not line.startswith("Standing approval (")
        ).strip()
        for text in texts
        if not text.startswith((_NOTICE_PREFIX, "[System: standing approval changed:"))
    ]
    texts = [text for text in texts if text]
    return (window_id, source_turn, texts) if texts else None


@dataclass
class ChildContextRecovery:
    """One submission per window; a proven compaction refusal permits one retry."""

    pending_window: str | None = None
    refused: bool = False
    loaded_identity: tuple[str, str] | None = None
    source_turn: str | None = None

    def restore_pending(self, bridge_dir: Path, session_id: str, thread_id: str) -> None:
        """Reconcile a proven refusal after reconnect without resubmitting unknown input."""
        identity = (session_id, thread_id)
        if self.loaded_identity == identity:
            return
        self.loaded_identity = identity
        try:
            saved = json.loads((bridge_dir / "context-recovery.json").read_text())
        except (OSError, ValueError):
            return
        if (
            isinstance(saved, dict)
            and saved.get("session_id") == session_id
            and saved.get("thread_id") == thread_id
            and saved.get("refused") is True
            and isinstance(saved.get("window_id"), str)
        ):
            self.pending_window = saved["window_id"]
            self.refused = True
            self.source_turn = saved.get("source_turn_id")

    async def restore(
        self,
        *,
        client: httpx.AsyncClient,
        codex_client: CodexAppServerClient,
        session_id: str,
        bridge_dir: Path,
        rollout: Path,
        thread_id: str,
        turn_id: str,
        model_resumed: bool,
    ) -> None:
        """Steer the ongoing turn only; never start work after a stop or cancellation."""
        try:
            self.restore_pending(bridge_dir, session_id, thread_id)
            pending = (
                (self.pending_window, self.source_turn)
                if self.refused and self.pending_window and self.source_turn
                else None
            )
            assignment = await asyncio.to_thread(_current_assignment, rollout, turn_id, pending)
            if assignment is None:
                return
            window_id, source_turn, messages = assignment
            if self.pending_window != window_id:
                self.refused = False
            marker = bridge_dir / "context-recovery.json"
            identity: dict[str, object] = {
                "session_id": session_id,
                "thread_id": thread_id,
                "window_id": window_id,
            }
            if marker.exists():
                saved = json.loads(marker.read_text(encoding="utf-8"))
                if saved == identity:
                    return
                if saved == {**identity, "refused": True} or saved == {
                    **identity,
                    "refused": True,
                    "source_turn_id": source_turn,
                }:
                    self.pending_window = window_id
                    self.refused = True
            if self.refused and not model_resumed:
                return
            response = await client.get(f"/v1/sessions/{url_component(session_id)}")
            response.raise_for_status()
            if not response.json().get("parent_session_id"):
                return
            self.pending_window = window_id
            text = (
                f"{_NOTICE_PREFIX}\n{CODEX_CHILD_CONTEXT_RECOVERY_INSTRUCTION}\n\n"
                + json.dumps(messages, ensure_ascii=False)
            )
            # A lost RPC reply is ambiguous: persist before sending and never resend it.
            atomic_write_json(marker, identity)
            try:
                await asyncio.wait_for(
                    codex_client.request(
                        "turn/steer",
                        {
                            "threadId": thread_id,
                            "expectedTurnId": turn_id,
                            "input": [{"type": "text", "text": text}],
                        },
                    ),
                    timeout=5,
                )
            except CodexAppServerResponseError as error:
                if is_compaction_steer_refusal(error) and not self.refused:
                    atomic_write_json(
                        marker, {**identity, "refused": True, "source_turn_id": source_turn}
                    )
                    self.refused = True
                    self.source_turn = source_turn
                    return
                raise
            self.pending_window = None
            self.refused = False
            self.source_turn = None
        except Exception:
            self.pending_window = None
            _logger.warning(
                "Could not restore Codex child context for %s", session_id, exc_info=True
            )
