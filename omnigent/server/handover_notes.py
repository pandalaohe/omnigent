"""Identify server-authored handover notes in persisted transcripts."""

from __future__ import annotations

import hashlib

from omnigent.entities import Conversation, ConversationItem, MessageData
from omnigent.server.auto_rotation import FAILED_ATTEMPT_LABEL_KEY


def handover_stable_id(session_id: str, text: str) -> str:
    """Return the dedupe id for one handover note on one session."""
    return hashlib.sha256(f"session_handover:{session_id}:{text}".encode()).hexdigest()[:32]


def annotate_handover_note(item: ConversationItem, session: Conversation) -> ConversationItem:
    """Mark a legacy note for display when its original persisted id proves its origin."""
    data = item.data
    if (
        not isinstance(data, MessageData)
        or data.role != "assistant"
        or data.is_handover_note
        or len(data.content) != 1
    ):
        return item
    block = data.content[0]
    text = block.get("text")
    if block.get("type") != "output_text" or not isinstance(text, str) or not text:
        return item

    candidates = [text, f"{text}\0initial"]
    failed_attempt = session.labels.get(FAILED_ATTEMPT_LABEL_KEY)
    if failed_attempt is not None:
        candidates.append(f"{text}\0{failed_attempt}")
    if any(item.id == handover_stable_id(session.id, candidate) for candidate in candidates):
        return item.model_copy(update={"data": data.model_copy(update={"is_handover_note": True})})
    return item
