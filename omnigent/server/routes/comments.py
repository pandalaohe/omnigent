"""Routes for per-session review comments.

Comments can be sent to the agent as a formatted message via the
``/comments/send`` endpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, model_validator

from omnigent.db.enum_codecs import COMMENT_STATUS
from omnigent.entities import Comment
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    attribution_user,
    get_user_id,
    require_access,
)
from omnigent.server.routes._errors import session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.comment_store.visitor_comments import (
    VISITOR_FEEDBACK_HEADER,
    comment_rows,
    is_visitor_author,
    visitor_author_label,
)
from omnigent.stores.permission_store import PermissionStore


def _escape_line_breaks(text: str) -> str:
    """Escape every character that could break out of a single output line.

    Used for file paths, which are not quoted: C0 controls and the Unicode
    line and paragraph separators become ``\\uXXXX`` escapes so a path always
    stays on its ``File:`` line. All other characters (including ``\\`` and
    ``"``) are unchanged.

    :param text: The path or other single-line value to escape.
    :returns: The text with line-break-capable characters escaped.
    """
    return "".join(
        f"\\u{ord(char):04x}" if ord(char) < 0x20 or char in "\u0085\u2028\u2029" else char
        for char in text
    )


def _excerpt_lines(anchor_content: str | None) -> list[str]:
    """Quote an anchor snippet as ``Excerpt:`` and ``> ``-prefixed lines.

    Each line of the anchor is kept verbatim — indentation and blank lines
    included, a blank line rendering as ``>``. ``str.splitlines`` splits on
    every Unicode line boundary, so no raw separator survives into the output.

    :param anchor_content: The selected-text snapshot, or ``None``.
    :returns: The excerpt block lines, or an empty list when there is no
        anchor content.
    """
    if not anchor_content:
        return []
    return ["Excerpt:"] + [f"> {line}" if line else ">" for line in anchor_content.splitlines()]


def _quoted_comment_body(body: str) -> str:
    """Quote a comment body as one escaped JSON string literal.

    JSON escaping covers quotes, backslashes, and C0 controls; the Unicode
    line and paragraph separators it leaves raw are escaped here too, so a
    body can never span output lines or forge a following entry.

    :param body: The comment body text.
    :returns: The body as one double-quoted line.
    """
    quoted = json.dumps(body, ensure_ascii=False)
    for separator in ("\u0085", "\u2028", "\u2029"):
        quoted = quoted.replace(separator, f"\\u{ord(separator):04x}")
    return quoted


def _comment_sections(
    comments: list[Comment],
    *,
    bullet_label: Callable[[Comment], str] | None = None,
) -> list[str]:
    """Render the ``File:`` sections for *comments*.

    Groups comments by file path (alphabetical) and sorts within each group
    by ``start_index`` ascending. Each entry carries the character range
    (start–end), the anchor content as an ``Excerpt:`` block quoted line by
    line, and the body as one escaped double-quoted line, so a line break or
    an embedded quote cannot forge another entry. The range lets the agent
    locate the relevant section without pre-computed line numbers.

    :param comments: The comments to render.
    :param bullet_label: Optional label inserted into each entry's comment
        line, used to attribute visitor comments to their author.
    :returns: The section lines, or an empty list when there are no comments.
    """
    by_path: dict[str, list[Comment]] = {}
    for c in comments:
        by_path.setdefault(c.path, []).append(c)

    lines: list[str] = []
    for path in sorted(by_path):
        lines.append("")
        lines.append(f"File: {_escape_line_breaks(path)}")
        for index, c in enumerate(sorted(by_path[path], key=lambda c: c.start_index)):
            if index:
                lines.append("")
            lines.append(f"Location: characters {c.start_index}–{c.end_index}")
            lines.extend(_excerpt_lines(c.anchor_content))
            prefix = (
                f"Visitor comment ({bullet_label(c)})"
                if bullet_label is not None
                else "User comment"
            )
            lines.append(f"{prefix}: {_quoted_comment_body(c.body)}")

    return lines


def _format_message(comments: list[Comment]) -> str:
    """Format a list of comments into a human-readable message for the agent.

    The owner's comments keep the standard listing, each body on its own
    ``User comment:`` line. Visitor comments are appended in their own
    untrusted-feedback section, each body on a labelled
    ``Visitor comment (…):`` line, so a comment relayed from a shared link
    is never presented as an instruction from the user.

    :param comments: The comments to format.
    :returns: A multi-line string suitable for posting to the agent.
    """
    owner_comments = [c for c in comments if not is_visitor_author(c.created_by)]
    visitor_comments = [c for c in comments if is_visitor_author(c.created_by)]

    lines = ["Please address the following review comments."]
    lines.extend(_comment_sections(owner_comments))
    if visitor_comments:
        lines.append("")
        lines.append(VISITOR_FEEDBACK_HEADER)
        lines.extend(
            _comment_sections(
                visitor_comments,
                bullet_label=lambda c: visitor_author_label(c.created_by or ""),
            )
        )
    return "\n".join(lines)


# ── Request models ─────────────────────────────────────────────────────────────


class AddCommentRequest(BaseModel):
    """Request body for ``POST /sessions/{id}/comments``.

    :param path: File path relative to workspace root,
        e.g. ``"src/App.tsx"``.
    :param body: The comment text.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param anchor_content: Plain-text snapshot of the selected range, used
        to re-anchor the comment after file edits. ``None`` if not provided.
    """

    path: str
    body: str
    start_index: int
    end_index: int
    anchor_content: str | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> AddCommentRequest:
        """Reject semantically invalid range field combinations.

        :returns: The validated request unchanged.
        :raises ValueError: If any range field is out of bounds or inconsistent.
        """
        if self.start_index < 0:
            raise ValueError("start_index must be >= 0")
        if self.end_index < self.start_index:
            raise ValueError("end_index must be >= start_index")
        return self


class UpdateCommentRequest(BaseModel):
    """Request body for ``PATCH /sessions/{id}/comments/{comment_id}``.

    :param status: New status, e.g. ``"addressed"``. ``None`` leaves
        it unchanged.
    :param body: New comment body. ``None`` leaves it unchanged.
    """

    status: str | None = None
    body: str | None = None


class SendCommentsRequest(BaseModel):
    """Request body for ``POST .../comments/send``.

    :param comment_ids: IDs of comments to send.
    :param instruction: Optional custom instruction prefix; defaults
        to the standard "Please address the following file review
        comments." header.
    :param mark_addressed: When ``True`` (the default), each sent comment
        is marked ``addressed``. ``False`` formats and returns the message
        without touching comment status, so a caller can mark the comments
        only after the message actually reached the agent.
    """

    comment_ids: list[str]
    instruction: str | None = None
    mark_addressed: bool = True


# ── Router factory ─────────────────────────────────────────────────────────────


def create_comments_router(
    store: CommentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """Build the comments router.

    All routes are scoped to ``/sessions/{session_id}/comments``.

    When both ``permission_store`` and ``conversation_store`` are provided
    (multi-user mode), every handler enforces session-level access:
    read endpoints require ``LEVEL_READ``, mutating endpoints require
    ``LEVEL_EDIT``.

    :param store: The shared :class:`CommentStore` instance.
    :param auth_provider: Auth provider used to identify the requesting
        user. ``None`` in single-user mode (no attribution stored).
    :param permission_store: Permission store used to check session-level
        access grants. ``None`` disables permission enforcement.
    :param conversation_store: Conversation store used by the permission
        checker for sub-agent session delegation. Must be provided when
        ``permission_store`` is not ``None``.
    :returns: A configured :class:`APIRouter`.
    :raises ValueError: If ``permission_store`` is provided without
        ``conversation_store``.
    """
    if permission_store is not None and conversation_store is None:
        raise ValueError("conversation_store is required when permission_store is provided")
    router = APIRouter()

    async def _require_session_access(user_id: str | None, session_id: str, level: int) -> None:
        """Require access and a real session before comment store mutations.

        :param user_id: The authenticated caller, or the single-user sentinel.
        :param session_id: The session to check, e.g. ``"conv_abc123"``.
        :param level: Required permission level for auth-enabled servers.
        :raises OmnigentError: 404 when the session does not exist, or the
            auth helper's 401/403/404 when permission enforcement is active.
        """
        if permission_store is not None:
            assert conversation_store is not None
            await require_access(user_id, session_id, level, permission_store, conversation_store)
        if conversation_store is not None:
            conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conversation is None:
                raise session_not_found()

    async def _require_comment_author(
        user_id: str | None,
        comment_id: str,
        session_id: str,
        *,
        visitor_deletable: bool = False,
    ) -> None:
        """Enforce that the caller authored the comment they are mutating.

        Used to gate the author-only operations — editing a comment's
        ``body`` and deleting a comment — on top of the session-level
        ``LEVEL_EDIT`` gate, which callers MUST run first. A session
        collaborator with edit access can still mark *anyone's* comment
        addressed (a shared review-workflow action), but cannot rewrite or
        delete another user's comment.

        Visitor comments (written through a share link, so they have no
        account author) are the exception to deletion: *visitor_deletable*
        lets an editor remove them, while body edits stay author-only
        because the delete path is the only caller that passes it.

        Comments with no recorded author (``created_by is None`` — legacy
        comments created before per-user attribution, or single-user mode)
        remain editable/deletable by any editor, since there is no author to
        protect. This helper is only invoked when permission enforcement is
        active (``permission_store`` set), so single-user mode never reaches
        it regardless.

        The synchronous store read is dispatched to a worker thread to keep
        the event loop unblocked, matching :func:`require_access`.

        :param user_id: The authenticated caller, e.g. ``"bob@example.com"``.
        :param comment_id: The comment being mutated, e.g. ``"a1b2c3d4-..."``.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param visitor_deletable: When ``True``, a visitor-authored comment
            passes without an author match (DELETE only).
        :raises OmnigentError: 404 if the comment is not found in this
            session; 403 if the caller is not the comment's author.
        """
        comment = await asyncio.to_thread(store.get, comment_id, session_id)
        if comment is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        if visitor_deletable and is_visitor_author(comment.created_by):
            return
        if comment.created_by is not None and comment.created_by != user_id:
            raise OmnigentError(
                "Only the comment author can edit or delete this comment",
                code=ErrorCode.FORBIDDEN,
            )

    @router.post("/sessions/{session_id}/comments")
    async def add_comment(
        request: Request,
        session_id: str,
        body: AddCommentRequest,
    ) -> dict[str, Any]:
        """Create a new review comment.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param body: Comment payload including path, body text, and the
            two range fields (start_index, end_index).
        :returns: The created comment as a serialized dict.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id, LEVEL_EDIT)
        comment = store.add(
            conversation_id=session_id,
            path=body.path,
            body=body.body,
            start_index=body.start_index,
            end_index=body.end_index,
            anchor_content=body.anchor_content,
            # Map the single-user "local" sentinel to None (matching the
            # sessions/messages write paths) so single-user comments record
            # no author and stay editable/deletable by any editor — both the
            # author-only server gate (``_require_comment_author``) and the
            # client's Edit/Delete affordances key off ``created_by is None``.
            created_by=attribution_user(user_id),
        )
        return asdict(comment)

    @router.get("/sessions/{session_id}/comments")
    async def list_comments(
        request: Request,
        session_id: str,
        path: str | None = None,
        include_visitor_drafts: bool = False,
    ) -> list[dict[str, Any]]:
        """List comments for a session, optionally filtered by file.

        Requires ``LEVEL_READ`` on the session in multi-user mode.

        Visitor comments still in ``draft`` are omitted (the owner has not
        sent them to the agent); the owner's read path passes
        ``include_visitor_drafts=true`` to see them. Every returned visitor
        row carries ``source: "visitor"`` and an untrusted-data note.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The session to query, e.g. ``"conv_abc123"``.
        :param path: When provided, only return comments for this file,
            e.g. ``"src/App.tsx"``.
        :param include_visitor_drafts: When ``True``, include visitor
            comments that are still drafts.
        :returns: List of serialized comment dicts.
        :raises OmnigentError: 401/403/404 if the user lacks read permission.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id, LEVEL_READ)
        comments = store.list_for_conversation(session_id, path=path)
        return comment_rows(comments, include_visitor_drafts=include_visitor_drafts)

    @router.patch("/sessions/{session_id}/comments/{comment_id}")
    async def update_comment(
        request: Request,
        session_id: str,
        comment_id: str,
        body: UpdateCommentRequest,
    ) -> dict[str, Any]:
        """Update a comment's status and/or body text.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode.
        Editing the ``body`` additionally requires the caller to be the
        comment's author: rewriting another user's comment is forbidden,
        while changing only the ``status`` (e.g. marking it ``"addressed"``)
        stays open to any editor as a shared review-workflow action.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param comment_id: The comment to update, e.g. ``"a1b2c3d4-..."``.
        :param body: Fields to update; ``None`` fields are left unchanged.
        :returns: The updated serialized comment.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
             403 if a body edit is attempted on another user's comment,
            or 404 if the comment is not found.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id, LEVEL_EDIT)
        if permission_store is not None:
            # Rewriting comment text is author-only; a status-only change is a
            # shared review-workflow action that any editor (and the agent's
            # update_comment tool) may perform.
            if body.body is not None:
                await _require_comment_author(user_id, comment_id, session_id)
        # Validate the status only after existence/ownership checks, so a
        # request targeting a comment the caller can't see still returns 404
        # (not a 400 that would leak the comment's existence). The check keeps
        # an unknown status out of the store, where the enum codec would raise
        # into an opaque 500; the column is a closed enum (draft/addressed).
        if body.status is not None and body.status not in COMMENT_STATUS:
            if store.get(comment_id, session_id) is None:
                raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
            raise OmnigentError(
                f"invalid status {body.status!r}; must be one of {sorted(COMMENT_STATUS)}",
                code=ErrorCode.INVALID_INPUT,
            )
        comment = store.update_comment(comment_id, session_id, status=body.status, body=body.body)
        if comment is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return asdict(comment)

    @router.delete("/sessions/{session_id}/comments/{comment_id}")
    async def delete_comment(
        request: Request,
        session_id: str,
        comment_id: str,
    ) -> dict[str, Any]:
        """Delete a comment.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode, and
        additionally that the caller is the comment's author — one
        collaborator may not delete another user's comment. A visitor
        comment has no account author, so any editor may delete it.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param comment_id: The comment to delete, e.g. ``"a1b2c3d4-..."``.
        :returns: ``{"deleted": true}``.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
            403 if the caller is not the comment's author, or 404 if the
            comment is not found or does not belong to this session.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id, LEVEL_EDIT)
        if permission_store is not None:
            await _require_comment_author(user_id, comment_id, session_id, visitor_deletable=True)
        deleted = store.delete(comment_id, session_id)
        if deleted is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return {"deleted": True}

    @router.post("/sessions/{session_id}/comments/send")
    async def send_to_agent(
        request: Request,
        session_id: str,
        body: SendCommentsRequest,
    ) -> dict[str, Any]:
        """Mark comments as addressed and format them into an agent message.

        Fetches each requested comment, marks it ``addressed`` (unless
        ``mark_addressed`` is ``False``), and formats the full set into a
        grouped, sorted message string suitable for pasting into the chat
        composer. ``mark_addressed: false`` exists so a caller can format
        the message, deliver it, and only then mark the comments.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode because
        it can transition comment status from ``draft`` to ``addressed``.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param body: List of comment IDs to send, with an optional
            custom instruction prefix and the mark-addressed switch.
        :returns: ``{"formatted_message": str, "sent_comment_ids": list[str]}``.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
            or 404 if any requested comment is not found or does not belong to
            this session.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id, LEVEL_EDIT)

        # Fetch (and, unless disabled, mark-addressed for) every requested
        # comment runs N sync DB gets + up to N sync updates. Do the whole
        # batch in one worker-thread hop so it never blocks the single-worker
        # event loop (and can't serialize concurrent requests behind it).
        def _fetch_and_mark() -> list[Comment]:
            """Resolve every comment id, then mark each addressed."""
            fetched: list[Comment] = []
            for cid in body.comment_ids:
                comment = store.get(cid, session_id)
                if comment is None:
                    raise OmnigentError(f"Comment not found: {cid}", code=ErrorCode.NOT_FOUND)
                fetched.append(comment)
            if body.mark_addressed:
                for comment in fetched:
                    store.update_comment(comment.id, session_id, status="addressed")
            return fetched

        to_send = await asyncio.to_thread(_fetch_and_mark)

        formatted = _format_message(to_send)
        return {
            "formatted_message": formatted,
            "sent_comment_ids": [c.id for c in to_send],
        }

    return router
