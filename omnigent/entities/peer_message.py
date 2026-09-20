"""Peer-message entity — persisted in the ``session_peer_messages`` table.

A :class:`SessionPeerMessage` is one durable record of a chat-style
message from one session to another session the same user owns. The
server stores a record whenever a send cannot be delivered inline and
a background sweeper delivers, expires, or fails it; the record keeps
the disposition and the reply link.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionPeerMessage:
    """
    One durable peer message between two sessions.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param sender_session_id: The sending session.
    :param receiver_session_id: The receiving session.
    :param ref: ``correlation_id`` or the id hex — the envelope always
        carries it so a reply can be matched to this record.
    :param text: Message text.
    :param state: ``pending``/``queued``/``held``/``delivering``/
        ``delivered``/``failed``/``expired``/``refused_by_user``.
    :param correlation_id: Optional caller correlation id, or ``None``.
    :param reason: Short disposition classification, or ``None``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    :param expires_at: Unix epoch seconds the sweeper expires the record.
    :param reply_peer_id: The reply record's id, or ``None``.
    :param replied_at: Unix epoch seconds the reply was recorded, or
        ``None``.
    :param workspace_id: Tenant partition key that owns this row.
    """

    id: str
    sender_session_id: str
    receiver_session_id: str
    ref: str
    text: str
    state: str
    correlation_id: str | None = None
    reason: str | None = None
    created_at: int = 0
    updated_at: int | None = None
    expires_at: int = 0
    reply_peer_id: str | None = None
    replied_at: int | None = None
    workspace_id: int = 0
