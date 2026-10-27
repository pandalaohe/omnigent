"""Mother-scoped advisory permissions, snapshotted independently of instructions."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import uuid4

from cachetools import TTLCache

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN, current_workspace_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.stores.conversation_store import (
    ROTATION_SUCCESSOR_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    SUCCEEDS_LABEL_KEY,
    ConversationStore,
    LabelGroupChange,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore

if TYPE_CHECKING:
    from omnigent.server.schemas import SessionEventInput

STANDING_PASS_LABEL_KEY = "omnigent.standing_pass"
logger = logging.getLogger(__name__)


class PassEvent(TypedDict):
    op: Literal["materialise", "carry", "revoke", "grant"]
    kinds: list[str]
    source: str
    at: int


class StandingPass(TypedDict):
    kinds: list[str]
    exceptions: list[str]
    since: int
    events: list[PassEvent]


class StandingPassConflict(RuntimeError):
    """The pass changed in every compare-and-set attempt."""


def pass_strings(value: Any, *, limit: int = 32) -> list[str]:
    """Accept bounded, single-line strings without interpreting their vocabulary."""
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"expected a list with at most {limit} strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 120:
            raise ValueError("each string must contain 1–120 characters")
        item = " ".join(item.split())
        if item not in result:
            result.append(item)
    return result


def _empty() -> StandingPass:
    return {"kinds": [], "exceptions": [], "since": int(time.time()), "events": []}


def _parse(raw: str) -> StandingPass:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("record must be an object")
        kinds = pass_strings(value["kinds"])
        exceptions = pass_strings(value["exceptions"], limit=5)
        since = value["since"]
        events = value["events"]
        if (
            type(since) is not int
            or not 0 <= since <= 253402300799
            or not isinstance(events, list)
        ):
            raise ValueError("invalid record timestamps or history")
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("op") not in {"materialise", "carry", "revoke", "grant"}
                or not isinstance(event.get("source"), str)
                or type(event.get("at")) is not int
            ):
                raise ValueError("invalid event")
            pass_strings(event.get("kinds"))
        return {"kinds": kinds, "exceptions": exceptions, "since": since, "events": events}
    except (ValueError, KeyError, TypeError):
        logger.warning("Invalid standing pass label; treating it as empty")
        return _empty()


def _record_text(labels: dict[str, str]) -> str | None:
    raw = labels.get(STANDING_PASS_LABEL_KEY)
    if raw is None:
        return None
    try:
        head = json.loads(raw)
        if isinstance(head, dict) and type(head.get("chunks")) is int and head["chunks"] > 0:
            return "".join(labels[f"{STANDING_PASS_LABEL_KEY}.{i}"] for i in range(head["chunks"]))
    except (ValueError, KeyError):
        pass
    return raw


def mutate_standing_pass(
    store: ConversationStore,
    root_id: str,
    apply: Callable[[StandingPass | None], StandingPass],
) -> StandingPass:
    """CAS the whole record in one transaction across 256-character label rows."""
    for _ in range(5):
        root = store.get_conversation(root_id)
        if root is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        expected = {
            key: value
            for key, value in root.labels.items()
            if key.startswith(STANDING_PASS_LABEL_KEY)
        }
        raw = _record_text(expected)
        record = apply(_parse(raw) if raw is not None else None)
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        chunks = [
            encoded[i : i + LABEL_VALUE_MAX_LEN]
            for i in range(0, len(encoded), LABEL_VALUE_MAX_LEN)
        ]
        updates = {f"{STANDING_PASS_LABEL_KEY}.{i}": value for i, value in enumerate(chunks)}
        updates[STANDING_PASS_LABEL_KEY] = json.dumps(
            {"chunks": len(chunks), "revision": uuid4().hex}
        )

        def change(
            current: dict[str, str],
            expected: dict[str, str] = expected,
            updates: dict[str, str] = updates,
            record: StandingPass = record,
        ) -> LabelGroupChange:
            if current != expected:
                return LabelGroupChange(upserts={}, deletes=set())
            if record["events"] and record["events"][-1]["op"] in {"grant", "revoke"}:
                _require_mutable_root(store, root_id)
            return LabelGroupChange(upserts=updates, deletes=current.keys() - updates.keys())

        stored = store.mutate_label_group(root_id, STANDING_PASS_LABEL_KEY, change)
        if stored == updates:
            return record
    raise StandingPassConflict("standing pass changed during all 5 mutation attempts")


def standing_pass_line(record: StandingPass) -> str:
    revoked = sorted(
        {kind for event in record["events"] if event["op"] == "revoke" for kind in event["kinds"]}
        - set(record["kinds"])
    )
    if not record["kinds"] and not revoked:
        return ""
    since = datetime.fromtimestamp(record["since"], timezone.utc).date().isoformat()
    exceptions = "; ".join(json.dumps(s, ensure_ascii=False) for s in record["exceptions"])
    kinds = ", ".join(json.dumps(s, ensure_ascii=False) for s in record["kinds"])
    return (
        f"Standing pass (this mother session, since {since}): {kinds or 'none'}. "
        f"Exceptions: {exceptions or 'none'}. Revoked: {', '.join(revoked) or 'none'}. "
        "Danger-list actions are never covered; "
        "call sys_standing_pass read before a pass-kind action."
    )


# Approved tool calls cross the runner HTTP hop once. Keys bind the workspace,
# session and exact delta; neither request arguments nor headers can mint one.
# custom-lint: disable-next=workspace-scoped-cache -- keys include current_workspace_id()
_approved: TTLCache[tuple[int, str, tuple[str, ...]], str] = TTLCache(maxsize=4096, ttl=300)
_approval_lock = threading.Lock()


def remember_grant_approval(session_id: str, arguments: dict[str, Any], source: str) -> None:
    if arguments.get("op") != "grant":
        return
    kinds = tuple(pass_strings(arguments.get("kinds", [])))
    with _approval_lock:
        _approved[(current_workspace_id(), session_id, kinds)] = source


def consume_grant_approval(session_id: str, kinds: list[str]) -> str | None:
    with _approval_lock:
        return _approved.pop((current_workspace_id(), session_id, tuple(kinds)), None)


def _require_mutable_root(store: ConversationStore, session_id: str) -> None:
    current = store.get_conversation(session_id)
    if current is None or current.parent_conversation_id is not None:
        raise OmnigentError(
            "Only the mother session can change its pass", code=ErrorCode.FORBIDDEN
        )
    if current.labels.get(SUCCEEDED_BY_LABEL_KEY):
        raise OmnigentError("Predecessor session is read-only", code=ErrorCode.FORBIDDEN)
    predecessor_id = current.labels.get(SUCCEEDS_LABEL_KEY)
    if predecessor_id and store.get_succession(predecessor_id, current.id) is None:
        raise OmnigentError("Session succession is not complete", code=ErrorCode.CONFLICT)


class StandingPassService:
    def __init__(
        self,
        store: ConversationStore,
        permission_store: PermissionStore | None = None,
        prefs_store: SqlAlchemyUserPreferencesStore | None = None,
        project_store: ProjectStore | None = None,
    ) -> None:
        self.store = store
        self.permission_store = permission_store
        self.prefs_store = prefs_store
        self.project_store = project_store

    def root(self, conversation: Conversation) -> Conversation:
        current = self.store.get_conversation(conversation.id)
        if current is None:
            raise ValueError("standing pass session no longer exists")
        conversation = current
        seen: set[str] = set()
        while conversation.parent_conversation_id:
            if conversation.id in seen:
                raise ValueError("cycle in standing pass parent chain")
            seen.add(conversation.id)
            parent = self.store.get_conversation(conversation.parent_conversation_id)
            if parent is None:
                raise ValueError("standing pass parent no longer exists")
            conversation = parent
        return conversation

    def resolve(self, conversation: Conversation) -> StandingPass:
        root = self.root(conversation)
        raw = _record_text(root.labels)
        if raw is not None:
            return _parse(raw)
        predecessor_id = root.labels.get(SUCCEEDS_LABEL_KEY)
        predecessor = self.store.get_conversation(predecessor_id) if predecessor_id else None
        prepared = False
        if predecessor is not None:
            try:
                prepared = (
                    json.loads(predecessor.labels.get(ROTATION_SUCCESSOR_LABEL_KEY, "null")).get(
                        "id"
                    )
                    == root.id
                )
            except (ValueError, AttributeError):
                prepared = False
        receipt = self.store.get_succession(predecessor_id, root.id) if predecessor_id else None
        if predecessor_id and (prepared or receipt is not None):
            if predecessor is None:
                raise ValueError("standing pass predecessor no longer exists")
            initial = deepcopy(self.resolve(predecessor))
            if receipt is None:
                # Prepared successors may initialize before the source is frozen.
                # Read through until succession locks the predecessor's final pass.
                return initial
            op, source = "carry", predecessor_id
        else:
            owner = collab_owner_for(root, self.store, self.permission_store)
            defaults = read_collab_settings(self.prefs_store, owner).standing_pass_default
            config: dict[str, Any] = {}
            if root.project_id and self.project_store is not None:
                from omnigent.server.routes.sessions.routes_peer import effective_owner_id

                project = self.project_store.get(
                    root.project_id,
                    user_id=effective_owner_id(root, self.store, self.permission_store),
                )
                config = project.config if project is not None else {}
            orchestration = config.get("orchestration", {})
            project_pass = (
                orchestration.get("standing_pass", {}) if isinstance(orchestration, dict) else {}
            )
            try:
                remove = pass_strings(project_pass.get("remove", []))
                exceptions = pass_strings(project_pass.get("exceptions", []), limit=5)
            except (ValueError, AttributeError):
                remove, exceptions = list(defaults), []
            initial = _empty()
            initial["kinds"] = [kind for kind in defaults if kind not in remove]
            initial["exceptions"] = exceptions
            op, source = "materialise", "defaults"
        initial["events"].append(
            {"op": op, "kinds": list(initial["kinds"]), "source": source, "at": int(time.time())}
        )
        return mutate_standing_pass(self.store, root.id, lambda current: current or initial)

    def line(self, conversation: Conversation) -> str:
        return standing_pass_line(self.resolve(conversation))

    def first_message(
        self, conversation: Conversation, body: SessionEventInput
    ) -> SessionEventInput:
        if (
            conversation.parent_conversation_id is None
            or body.type != "message"
            or body.data.get("role", "user") != "user"
            or self.store.list_items(conversation.id, type="message", limit=1).data
        ):
            return body
        line = self.line(conversation)
        content = body.data.get("content", [])
        prefix = {"type": "input_text", "text": line}
        if line and isinstance(content, list) and prefix not in content:
            return body.model_copy(update={"data": {**body.data, "content": [prefix, *content]}})
        return body

    def change(
        self,
        conversation: Conversation,
        op: Literal["revoke", "grant"],
        kinds: list[str],
        source: str,
    ) -> StandingPass:
        self.require_mutable_root(conversation)
        self.resolve(conversation)
        event: PassEvent = {"op": op, "kinds": kinds, "source": source, "at": int(time.time())}

        def apply(current: StandingPass | None) -> StandingPass:
            assert current is not None
            if op == "grant" and any(
                previous["op"] == "grant" and previous["source"] == source
                for previous in current["events"]
            ):
                raise OmnigentError(
                    "Standing pass approval has already been used", code=ErrorCode.FORBIDDEN
                )
            result = deepcopy(current)
            result["kinds"] = (
                list(dict.fromkeys([*current["kinds"], *kinds]))
                if op == "grant"
                else [kind for kind in current["kinds"] if kind not in kinds]
            )
            pass_strings(result["kinds"])
            result["events"].append(event)
            return result

        return mutate_standing_pass(self.store, conversation.id, apply)

    def require_mutable_root(self, conversation: Conversation) -> None:
        _require_mutable_root(self.store, conversation.id)

    async def steer(
        self,
        root_id: str,
        record: StandingPass,
        post: Callable[[str, str], Awaitable[bool | None]],
    ) -> dict[str, str]:
        event = record["events"][-1]
        text = (
            f"[System: standing pass changed: {event['op']} {json.dumps(event['kinds'])}; "
            f"current: {json.dumps(record['kinds'])}]"
        )
        outcomes: dict[str, str] = {}
        pending = [root_id]
        seen = {root_id}
        while pending:
            parent_id = pending.pop()
            ids = (
                await asyncio.to_thread(
                    self.store.list_child_conversation_ids_by_parent, [parent_id]
                )
            ).get(parent_id, [])
            for child in (await asyncio.to_thread(self.store.get_conversations, ids)).values():
                if child.id in seen:
                    continue
                seen.add(child.id)
                pending.append(child.id)
                if child.archived or child.live_status != "running":
                    outcomes[child.id] = "skipped_not_running"
                    continue
                try:
                    posted = await post(child.id, text)
                    outcomes[child.id] = "skipped_not_running" if posted is False else "steered"
                except Exception:
                    logger.exception("Standing pass notice failed for %s", child.id)
                    outcomes[child.id] = "failed"
        return outcomes
