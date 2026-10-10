"""Attribute a dispatched child's settling turn to a peer sender.

A peer message reaches a child as one user envelope (see
:func:`omnigent.server.routes.sessions.routes_peer.format_peer_envelope`).
When the child's native turn ends, the server reads the child's transcript
and decides statelessly whether that turn started from a verified foreign
peer envelope: the newest assistant message equal to the edge output (X),
the first non-meta user message older than it (E), and a window between
them holding only assistant / reasoning / tool items. The result lets the
runner hand the parent a silent copy instead of a result wake, and lets the
blocked-approval notifier reach the peer sender instead of the mother.

Attribution errs toward waking: any doubt returns ``None`` so today's
delivery path runs. Every fact read here is already in the child's
transcript; nothing new is exposed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    ReasoningData,
)
from omnigent.server.routes._sessions.helpers import _message_text
from omnigent.server.routes.sessions.routes_peer import (
    _PEER_ENVELOPE_HEADER_RE,
    PEER_INPUT_LOOKBACK_ITEMS,
    peer_thread_origin_is_receiver,
)
from omnigent.stores import ConversationStore
from omnigent.stores.peer_message_store import PeerMessageStore

_logger = logging.getLogger(__name__)

# The forwarded excerpt is bounded so a long peer message cannot bloat the
# terminal-status body the runner retains across reconnects.
_EXCERPT_MAX_CHARS = 600

_PAGE_SIZE = 100


@dataclass(frozen=True)
class PeerTurn:
    """The peer sender a child's settled turn is attributed to.

    :param parent_session_id: The child's mother, who receives the copy.
    :param peer_id: E's peer record id (the envelope's ``msg=``).
    :param result_item_id: X's transcript item id, the turn's answer.
    :param ref: E's header ``ref``.
    :param sender_session_id: The peer sender (E's record sender).
    :param sender_title: Sender title exactly as written in E's header.
    :param sender_origin: Sender origin exactly as written in E's header.
    :param excerpt: The peer message text, capped at 600 chars.
    """

    parent_session_id: str
    peer_id: str
    result_item_id: str
    ref: str
    sender_session_id: str
    sender_title: str
    sender_origin: str
    excerpt: str

    def as_dict(self) -> dict[str, str]:
        """Return the annotation carried in the forwarded status body."""
        return {
            "parent_session_id": self.parent_session_id,
            "peer_id": self.peer_id,
            "result_item_id": self.result_item_id,
            "ref": self.ref,
            "sender_session_id": self.sender_session_id,
            "sender_title": self.sender_title,
            "sender_origin": self.sender_origin,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class _PeerHeader:
    """The fields parsed from a current-format envelope header."""

    sender_session_id: str
    msg_id: str
    title: str
    origin: str
    ref: str
    body: str


@dataclass(frozen=True)
class _Envelope:
    """A verified foreign envelope's record and parsed header."""

    record: SessionPeerMessage
    header: _PeerHeader


@dataclass(frozen=True)
class _ForeignTurn:
    """A verified foreign envelope and, when classifying, the answer item."""

    record: SessionPeerMessage
    header: _PeerHeader
    x_item: ConversationItem | None


def _parse_current_header(text: str) -> _PeerHeader | None:
    """Parse a current-format envelope header, or ``None`` for any other shape."""
    match = _PEER_ENVELOPE_HEADER_RE.match(text)
    if match is None:
        return None
    return _PeerHeader(
        sender_session_id=match.group("sender"),
        msg_id=match.group("msg"),
        title=match.group("title"),
        origin=match.group("origin"),
        ref=match.group("ref"),
        body=text[match.end() :],
    )


def _single_text_block(content: list[dict[str, Any]]) -> str | None:
    """Return the text of a message's sole text block, else ``None``.

    The envelope is delivered as exactly one text block; a message with
    attachments or extra blocks is not the delivered envelope.
    """
    if len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, dict) or block.get("type") not in (
        "input_text",
        "output_text",
        "text",
    ):
        return None
    text = block.get("text")
    return text if isinstance(text, str) else None


def _delivered_text(text: str) -> str:
    """Return text in terminal-delivered form without trailing whitespace.

    Mirrors ``omnigent/harnesses/claude_native/bridge.py::_paste_payload_bytes``.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in normalized if ord(ch) >= 0x20 or ch in "\n\t").rstrip()


def _verified_envelope(
    peer_store: PeerMessageStore, child: Conversation, data: MessageData
) -> _Envelope | None:
    """Return the record and header when *data* is a verified foreign envelope.

    Verifies the single text block, a current-format header, the record's
    existence / receiver / state / sender, that the body and stored text
    match in delivered form, that the sender is not the child's mother,
    and that the thread was not started by the child itself.
    """
    text = _single_text_block(data.content)
    if text is None:
        return None
    header = _parse_current_header(text)
    if header is None:
        return None
    record = peer_store.get(header.msg_id)
    if record is None:
        return None
    if record.receiver_session_id != child.id:
        return None
    if record.state not in ("delivering", "delivered"):
        return None
    if record.sender_session_id != header.sender_session_id:
        return None
    if _delivered_text(header.body) != _delivered_text(record.text):
        return None
    if record.sender_session_id == child.parent_conversation_id:
        return None
    if peer_thread_origin_is_receiver(peer_store, record):
        return None
    return _Envelope(record=record, header=header)


def _meta_disallowed(data: MessageData) -> bool:
    """Whether a meta user message is a Claude task completion or hand-back.

    Those items are the child's own result plumbing, not peer input; their
    presence in the window means the turn is not cleanly a foreign turn.
    """
    if data.subagent_return_id:
        return True
    text = _message_text(data.content)
    if text is None:
        return False
    from omnigent.harnesses.claude_native.bridge import _is_task_completion_text

    return _is_task_completion_text(text)


def _iter_items_newest_first(
    conversation_store: ConversationStore, conversation_id: str, max_items: int
) -> Iterator[ConversationItem]:
    """Yield a session's items newest first, bounded to *max_items*."""
    after: str | None = None
    seen = 0
    while seen < max_items:
        page = conversation_store.list_items(
            conversation_id, limit=_PAGE_SIZE, order="desc", after=after
        )
        if not page.data:
            return
        for item in page.data:
            yield item
            seen += 1
            if seen >= max_items:
                return
        if page.last_id is None:
            return
        after = page.last_id


def _scan_foreign_turn(
    conversation_store: ConversationStore,
    peer_store: PeerMessageStore,
    child: Conversation,
    *,
    output: str | None,
    require_x: bool,
) -> _ForeignTurn | None:
    """Walk the child's transcript and verify a foreign-started turn.

    With ``require_x`` the newest assistant message equal to *output* is X and
    the window ``(E, X]`` must hold only assistant, reasoning and tool items;
    without it the newest non-meta user message is E and everything newer must
    hold only those items. A function output whose call is outside the window,
    a task-completion meta message, an unverifiable E, or any other item kind
    returns ``None``.
    """
    if require_x and (output is None or not output.strip()):
        return None

    x_item: ConversationItem | None = None
    x_found = not require_x
    unmatched_outputs: set[str] = set()
    for item in _iter_items_newest_first(conversation_store, child.id, PEER_INPUT_LOOKBACK_ITEMS):
        data = item.data
        if not x_found:
            # Items newer than X are outside the window; only X itself matters.
            # X is the newest assistant text that equals the edge output
            # exactly — surrounding whitespace is a different message.
            if isinstance(data, MessageData) and not data.is_meta and data.role == "assistant":
                text = _message_text(data.content)
                if text is not None and text == output:
                    x_found = True
                    x_item = item
            continue
        if isinstance(data, MessageData):
            if data.is_meta:
                if _meta_disallowed(data):
                    return None
                continue
            if data.role == "assistant":
                continue
            # The first non-meta user message older than X is E.
            envelope = _verified_envelope(peer_store, child, data)
            if envelope is None:
                return None
            if unmatched_outputs:
                return None
            return _ForeignTurn(
                record=envelope.record,
                header=envelope.header,
                x_item=x_item,
            )
        if isinstance(data, FunctionCallData):
            unmatched_outputs.discard(data.call_id)
            continue
        if isinstance(data, FunctionCallOutputData):
            unmatched_outputs.add(data.call_id)
            continue
        if isinstance(data, ReasoningData):
            continue
        # An item kind outside the allowed window set cannot be vouched for.
        return None
    return None


def classify_child_turn(
    conversation_store: ConversationStore,
    peer_store: PeerMessageStore,
    child: Conversation,
    output: str | None,
) -> PeerTurn | None:
    """Return the peer turn *child*'s settled edge answers, or ``None``.

    Blocking; call through ``asyncio.to_thread``. Any doubt or error returns
    ``None`` so the edge follows today's delivery path.
    """
    try:
        if child.parent_conversation_id is None or child.kind != "sub_agent":
            return None
        turn = _scan_foreign_turn(
            conversation_store, peer_store, child, output=output, require_x=True
        )
        if turn is None or turn.x_item is None:
            return None
        record = turn.record
        return PeerTurn(
            parent_session_id=child.parent_conversation_id,
            peer_id=record.id,
            result_item_id=turn.x_item.id,
            ref=turn.header.ref,
            sender_session_id=record.sender_session_id,
            sender_title=turn.header.title,
            sender_origin=turn.header.origin,
            excerpt=record.text[:_EXCERPT_MAX_CHARS],
        )
    except Exception:
        _logger.warning("Peer turn classification failed", exc_info=True)
        return None


def foreign_turn_sender(
    conversation_store: ConversationStore,
    peer_store: PeerMessageStore,
    child: Conversation,
) -> str | None:
    """Return the sender of the child's active foreign peer turn, or ``None``.

    The same envelope verification as :func:`classify_child_turn` without X:
    the child's newest non-meta user message must be a verified foreign
    envelope and nothing disallowed may sit newer than it. Blocking; call
    through ``asyncio.to_thread``.
    """
    try:
        turn = _scan_foreign_turn(
            conversation_store, peer_store, child, output=None, require_x=False
        )
        if turn is None:
            return None
        return turn.record.sender_session_id
    except Exception:
        _logger.warning("Foreign peer turn lookup failed", exc_info=True)
        return None
