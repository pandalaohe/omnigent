"""Shared catalog evidence for saved model reference resolution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from omnigent.calling_defaults import CallingResolution, _catalog_model_ids
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.member_snapshot import (
    MEMBER_LABEL_PREFIX,
    MEMBER_LABEL_VALUE_MAX_CHARS,
    MEMBER_UNAVAILABLE_MODEL_MISSING,
    encode_member_entry,
    parse_member_entry,
)
from omnigent.models.model_reference import (
    ALIASES_NAMESPACE,
    ModelResolution,
    ProviderGroups,
    alias_pairs,
    provider_groups,
    resolve_saved_model,
)
from omnigent.server.json_labels import decode_json_labels, encode_json_labels

RESOLUTION_LABEL = "omnigent.model_resolution"
_logger = logging.getLogger(__name__)


def resolution_labels(notes: Sequence[dict[str, str]]) -> dict[str, str]:
    return encode_json_labels(RESOLUTION_LABEL, list(notes)) if notes else {}


def usable_catalog(record: Mapping[str, Any] | None) -> frozenset[str] | None:
    """An absent, empty or failed catalog cannot establish a model's absence."""
    if not record or record.get("error"):
        return None
    rows = record.get("models")
    if not isinstance(rows, list) or not rows:
        return None
    ids = frozenset(_catalog_model_ids([row for row in rows if isinstance(row, Mapping)]))
    return ids or None


def read_provider_groups(store: Any, owner: str) -> ProviderGroups:
    if store is None:
        return {}
    try:
        envelope = store.get(owner)
        value = (envelope or {}).get("settings", {}).get(ALIASES_NAMESPACE, {"pairs": []})
        return provider_groups(alias_pairs(value))
    except Exception:
        _logger.warning("Could not read model provider aliases for %s", owner, exc_info=True)
        return {}


def resolve_catalog_model(
    saved: str,
    catalog: frozenset[str] | None,
    groups: ProviderGroups,
    harness: str | None,
    *,
    allow_substitution: bool = False,
) -> ModelResolution:
    del harness
    return resolve_saved_model(
        saved,
        catalog,
        groups,
        allow_substitution=allow_substitution,
    )


@dataclass(frozen=True)
class ResolutionNote:
    setting: str
    saved: str
    model: str
    outcome: str
    host: str

    def payload(self) -> dict[str, str]:
        return asdict(self)

    def warning(self) -> str:
        if self.outcome == "missing":
            return (
                f"{self.saved} is not in this host's model catalog; trying it as unknown. "
                "Review Settings › Model references."
            )
        return (
            f"{self.saved} resolved to {self.model} on this host. "
            "Review Settings › Model references."
        )


@dataclass
class ResolutionContext:
    host: str
    records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    groups: ProviderGroups = field(default_factory=dict)
    notes: list[ResolutionNote] = field(default_factory=list)

    def resolve(self, harness: str | None, saved: str, setting: str) -> ModelResolution:
        result = resolve_catalog_model(
            saved, usable_catalog(self.records.get(harness or "")), self.groups, harness
        )
        if result.model != saved or result.outcome == "missing":
            note = ResolutionNote(setting, saved, result.model, result.outcome, self.host)
            if note not in self.notes:
                self.notes.append(note)
        return result

    def labels(self) -> dict[str, str]:
        return resolution_labels([note.payload() for note in self.notes])

    def apply_default(self, resolution: CallingResolution, saved: str | None) -> CallingResolution:
        if resolution.model is None and saved:
            evidence = self.resolve(resolution.harness, saved, "agent_default")
            resolution = replace(
                resolution,
                model=evidence.model if evidence.model != saved else None,
                model_resolution=evidence,
            )
        return replace(resolution, notes=tuple(note.payload() for note in self.notes))

    def warnings(self) -> list[str]:
        return [
            note.warning() for note in self.notes if note.outcome in {"substituted", "missing"}
        ]


async def resolution_context(
    *, host: str | None, owner: str, preferences: Any = None, catalogs: Any = None
) -> ResolutionContext:
    groups = await asyncio.to_thread(read_provider_groups, preferences, owner)
    records = await asyncio.to_thread(catalogs.list, [host]) if host and catalogs else []
    return ResolutionContext(host or "", {row["harness"]: row for row in records}, groups)


async def resolve_session_model(
    conversation: Any,
    *,
    conversations: Any,
    permissions: Any = None,
    agents: Any = None,
    agent_cache: Any = None,
    preferences: Any = None,
    catalogs: Any = None,
) -> Any:
    """Resolve effective model ids at the shared create/resume boundary."""
    from omnigent.server.routes._sessions.helpers import _resolve_harness_impl, _resolve_llm_model
    from omnigent.server.session_collab import collab_owner_for

    if not conversation.agent_id or (
        not conversation.host_id
        and not any(key.startswith(MEMBER_LABEL_PREFIX) for key in conversation.labels)
    ):
        return conversation
    owner = await asyncio.to_thread(collab_owner_for, conversation, conversations, permissions)
    context = await resolution_context(
        host=conversation.host_id, owner=owner, preferences=preferences, catalogs=catalogs
    )
    harness = await asyncio.to_thread(
        _resolve_harness_impl, conversation, agent_store=agents, agent_cache=agent_cache
    )
    saved = conversation.model_override or await asyncio.to_thread(
        _resolve_llm_model, conversation, agent_store=agents, agent_cache=agent_cache
    )
    if saved and conversation.host_id:
        result = context.resolve(harness, saved, "session")
        if result.model != saved:
            await asyncio.to_thread(
                conversations.update_conversation, conversation.id, model_override=result.model
            )
            conversation.model_override = result.model

    contexts = {conversation.host_id: context}
    labels = {}
    for key, raw in conversation.labels.items():
        if not key.startswith(MEMBER_LABEL_PREFIX):
            continue
        entry = parse_member_entry(raw)
        if (
            entry is None
            or not isinstance(entry.get("model"), str)
            or not isinstance(entry.get("harness"), str)
        ):
            continue
        host = entry.get("host") or conversation.host_id
        if not isinstance(host, str) or not host:
            continue
        if host not in contexts:
            contexts[host] = await resolution_context(
                host=host, owner=owner, preferences=preferences, catalogs=catalogs
            )
        evidence = contexts[host].resolve(
            entry["harness"], entry["model"], f"member:{key[len(MEMBER_LABEL_PREFIX) :]}"
        )
        changed = evidence.model != entry["model"]
        entry["model"] = evidence.model
        if entry.get("unavailable") == MEMBER_UNAVAILABLE_MODEL_MISSING:
            entry.pop("unavailable")
            changed = True
        if changed:
            encoded = encode_member_entry(entry)
            if len(encoded) > MEMBER_LABEL_VALUE_MAX_CHARS:
                raise OmnigentError(
                    "Resolved member model exceeds the saved size; shorten the provider name",
                    code=ErrorCode.INVALID_INPUT,
                )
            labels[key] = encoded
    notes = notes_from_labels(conversation.labels)
    for current in contexts.values():
        for note in current.notes:
            if note.payload() not in notes:
                notes.append(note.payload())
    if any(current.notes for current in contexts.values()):
        labels.update(resolution_labels(notes))
    if labels:
        await asyncio.to_thread(conversations.set_labels, conversation.id, labels)
        conversation.labels.update(labels)
    return conversation


async def resolve_app_session_model(app: Any, conversation: Any) -> Any:
    state = app.state
    return await resolve_session_model(
        conversation,
        conversations=state.conversation_store,
        permissions=getattr(state, "permission_store", None),
        agents=getattr(state, "agent_store", None),
        agent_cache=getattr(state, "agent_cache", None),
        preferences=getattr(state, "user_preferences_store", None),
        catalogs=getattr(state, "host_model_catalog_cache_store", None),
    )


def notes_from_labels(labels: Mapping[str, str]) -> list[dict[str, str]]:
    notes = decode_json_labels(labels, RESOLUTION_LABEL)
    return [note for note in notes if isinstance(note, dict)] if isinstance(notes, list) else []
