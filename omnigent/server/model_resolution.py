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
from omnigent.models.model_metadata import concrete_reported_model
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
LAUNCHED_MODEL_LABEL = "omnigent.launched_model"
_logger = logging.getLogger(__name__)


def resolution_labels(notes: Sequence[dict[str, str]]) -> dict[str, str]:
    return encode_json_labels(RESOLUTION_LABEL, list(notes)) if notes else {}


def _is_member_setting(note: Mapping[str, Any]) -> bool:
    setting = note.get("setting")
    return isinstance(setting, str) and setting.startswith("member:")


def _resolution_label_keys(labels: Mapping[str, str]) -> list[str]:
    return [
        key for key in labels if key == RESOLUTION_LABEL or key.startswith(f"{RESOLUTION_LABEL}.")
    ]


def clear_session_resolution_labels(
    labels: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """Drop session-level resolution notes while preserving member:* notes.

    :returns: ``(labels_to_set, keys_to_delete)`` to persist the cleaned notes.
    """
    if not labels:
        return {}, []
    existing_keys = _resolution_label_keys(labels)
    if not existing_keys:
        return {}, []
    existing_notes = notes_from_labels(labels)
    remaining = [note for note in existing_notes if _is_member_setting(note)]
    if len(remaining) == len(existing_notes) and existing_notes:
        return {}, []
    encoded = resolution_labels(remaining)
    to_delete = [key for key in existing_keys if key not in encoded]
    return encoded, to_delete


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
                f"{self.saved} is not in this host's model catalog; "
                "passing it to the host unchanged."
            )
        return f"{self.saved} resolved to {self.model} on this host."


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
        _resolve_harness_impl,
        conversation,
        agent_store=agents,
        agent_cache=agent_cache,
        preserve_acp_namespace=True,
    )
    saved = conversation.model_override or await asyncio.to_thread(
        _resolve_llm_model, conversation, agent_store=agents, agent_cache=agent_cache
    )
    session_resolved_model: str | None = None
    if saved and conversation.host_id:
        result = context.resolve(harness, saved, "session")
        session_resolved_model = result.model
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
    existing_notes = notes_from_labels(conversation.labels)
    new_session_notes = [
        note.payload() for note in context.notes if not note.setting.startswith("member:")
    ]
    new_member_notes = [
        note.payload()
        for current in contexts.values()
        for note in current.notes
        if note.setting.startswith("member:")
    ]
    member_notes = [note for note in existing_notes if _is_member_setting(note)]
    for note_payload in new_member_notes:
        if note_payload not in member_notes:
            member_notes.append(note_payload)
    if new_session_notes:
        session_notes = list(new_session_notes)
    elif session_resolved_model is not None:
        # Keep an existing session note only when this resume is still on that
        # same resolved model; drop notes belonging to an earlier model choice.
        session_notes = [
            note
            for note in existing_notes
            if not _is_member_setting(note)
            and isinstance(note.get("model"), str)
            and note["model"].strip() == session_resolved_model
        ]
    else:
        session_notes = [note for note in existing_notes if not _is_member_setting(note)]
    notes = [*session_notes, *member_notes]
    stale_keys: list[str] = []
    if notes != existing_notes or any(current.notes for current in contexts.values()):
        encoded_notes = resolution_labels(notes)
        labels.update(encoded_notes)
        stale_keys = [
            key for key in _resolution_label_keys(conversation.labels) if key not in encoded_notes
        ]
    if stale_keys:
        delete_label = getattr(conversations, "delete_label", None)
        for stale_key in stale_keys:
            if callable(delete_label):
                await asyncio.to_thread(delete_label, conversation.id, stale_key)
                conversation.labels.pop(stale_key, None)
            else:
                labels[stale_key] = ""
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


@dataclass(frozen=True)
class SessionModelDisplay:
    """Requested, launched, and running model ids for session and child info."""

    requested: str | None = None
    launched: str | None = None
    running: str | None = None


def session_model_display(
    labels: Mapping[str, str] | None,
    model_override: str | None,
    reported_model: str | None,
    *,
    harness: str | None = None,
) -> SessionModelDisplay:
    """Derive requested, launched, and running models for one session.

    Omits any field whose source is absent or unreliable for *harness*:
    ``launched`` is read only from ``omnigent.launched_model`` (stamped when
    a harness launch or confirmed live apply actually uses that model) and
    remains omitted for ``antigravity-native``; old sessions without
    ``reported_model`` or ``omnigent.launched_model`` omit those fields.
    """
    override = (
        model_override.strip()
        if isinstance(model_override, str) and model_override.strip()
        else None
    )
    running = concrete_reported_model(reported_model)
    requested = override
    if override is not None and labels:
        for note in reversed(notes_from_labels(labels)):
            if _is_member_setting(note):
                continue
            raw_saved = note.get("saved")
            raw_model = note.get("model")
            if (
                isinstance(raw_saved, str)
                and isinstance(raw_model, str)
                and raw_model.strip() == override
                and raw_saved.strip()
                and raw_saved.strip() != override
            ):
                requested = raw_saved.strip()
                break
    raw_launched = labels.get(LAUNCHED_MODEL_LABEL) if labels else None
    launched = (
        None
        if harness == "antigravity-native"
        else (
            raw_launched.strip()
            if isinstance(raw_launched, str) and raw_launched.strip()
            else None
        )
    )
    return SessionModelDisplay(requested=requested, launched=launched, running=running)
