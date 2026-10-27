"""Atomic mutations of per-session check-in overrides and follow-ups."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import replace
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.child_checkin import (
    CHECKIN_LABEL_PREFIX,
    FOLLOWUP_NEXT_KEY,
    FOLLOWUP_PREFIX,
    INSTRUCTIONS_PREFIX,
    POLICY_KEY,
    FollowUp,
    decode_followup,
    encode_followup,
    next_followup_due,
)
from omnigent.db.utils import strip_nul_bytes
from omnigent.stores.conversation_store import ConversationStore, LabelGroupChange

_logger = logging.getLogger(__name__)


def session_checkin_policy(labels: dict[str, str]) -> dict[str, int | str]:
    try:
        policy = json.loads(labels.get(POLICY_KEY, "{}"))
    except ValueError:
        policy = {}
    if not isinstance(policy, dict):
        return {}
    return {
        key: value
        for key, value in policy.items()
        if (key == "g" and type(value) is int and 0 <= value <= 86400)
        or (key == "c" and value in ("changed", "summary"))
    }


def session_checkin_instructions(labels: dict[str, str]) -> str | None:
    chunks = {key: value for key, value in labels.items() if key.startswith(INSTRUCTIONS_PREFIX)}
    if not chunks:
        return None
    keys = [f"{INSTRUCTIONS_PREFIX}{index}" for index in range(len(chunks))]
    if (
        len(chunks) > 4
        or set(keys) != chunks.keys()
        or any(len(value) > 250 for value in chunks.values())
    ):
        _logger.warning("Invalid session check-in instruction chunks")
        return None
    text = strip_nul_bytes("".join(chunks[key] for key in keys))
    if text and not text.strip():
        _logger.warning("Invalid blank session check-in instructions")
        return None
    return text


def checkin_followups(labels: dict[str, str]) -> dict[str, FollowUp]:
    items: dict[str, FollowUp] = {}
    for key, raw in labels.items():
        if not key.startswith(FOLLOWUP_PREFIX):
            continue
        item = decode_followup(raw)
        if item is None:
            _logger.warning("Invalid check-in follow-up label %s", key)
        else:
            items[key] = replace(item, id=key[len(FOLLOWUP_PREFIX) :])
    return items


class ChildCheckinPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    min_gap_seconds: Annotated[int, Field(ge=0, le=86400)] | None = None
    coverage: Literal["changed", "summary"] | None = None
    instructions: str | None = None
    reset: bool = False
    reset_instructions: bool = False

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Use reset or reset_instructions instead of null")
        if self.reset and {"min_gap_seconds", "coverage"} & self.model_fields_set:
            raise ValueError("reset cannot be combined with policy values")
        if self.reset_instructions and "instructions" in self.model_fields_set:
            raise ValueError("reset_instructions cannot be combined with instructions")
        if self.instructions is not None:
            self.instructions = strip_nul_bytes(self.instructions)
            if len(self.instructions) > 1000 or (
                self.instructions and not self.instructions.strip()
            ):
                raise ValueError(
                    "instructions must be empty or nonblank text within 1000 characters"
                )
        return self


class ChildFollowupAdd(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str
    after_minutes: Annotated[int, Field(ge=15, le=10080)]


def _indexed_change(
    current: dict[str, str], upserts: dict[str, str], deletes: set[str]
) -> LabelGroupChange:
    remaining = {key: value for key, value in current.items() if key not in deletes} | upserts
    due = next_followup_due(remaining)
    if due is None:
        deletes.add(FOLLOWUP_NEXT_KEY)
    else:
        upserts[FOLLOWUP_NEXT_KEY] = str(due)
    return LabelGroupChange(upserts, deletes)


def patch_checkin_labels(
    store: ConversationStore, session_id: str, patch: ChildCheckinPatch
) -> None:
    def change(current: dict[str, str]) -> LabelGroupChange:
        upserts: dict[str, str] = {}
        deletes: set[str] = set()
        if patch.reset:
            deletes.add(POLICY_KEY)
        elif {"min_gap_seconds", "coverage"} & patch.model_fields_set:
            policy = session_checkin_policy(current)
            if patch.min_gap_seconds is not None:
                policy["g"] = patch.min_gap_seconds
            if patch.coverage is not None:
                policy["c"] = patch.coverage
            upserts[POLICY_KEY] = json.dumps(policy, separators=(",", ":"))
        if patch.reset_instructions or patch.instructions is not None:
            deletes.update(key for key in current if key.startswith(INSTRUCTIONS_PREFIX))
            if patch.instructions is not None:
                chunks = [
                    patch.instructions[index : index + 250]
                    for index in range(0, len(patch.instructions), 250)
                ] or [""]
                upserts.update(
                    {f"{INSTRUCTIONS_PREFIX}{index}": text for index, text in enumerate(chunks)}
                )
        return _indexed_change(current, upserts, deletes)

    store.mutate_label_group(session_id, CHECKIN_LABEL_PREFIX, change)


def add_checkin_followup(
    store: ConversationStore, session_id: str, request: ChildFollowupAdd, now: int
) -> FollowUp:
    item = FollowUp("", now + request.after_minutes * 60, request.text)
    raw = encode_followup(item)
    fid = ""

    def change(current: dict[str, str]) -> LabelGroupChange:
        nonlocal fid
        if sum(key.startswith(FOLLOWUP_PREFIX) for key in current) >= 20:
            raise ValueError("This session has 20 or more follow-ups; remove one before adding")
        fid = secrets.token_hex(4)
        while f"{FOLLOWUP_PREFIX}{fid}" in current:
            fid = secrets.token_hex(4)
        return _indexed_change(current, {f"{FOLLOWUP_PREFIX}{fid}": raw}, set())

    store.mutate_label_group(session_id, CHECKIN_LABEL_PREFIX, change)
    return FollowUp(fid, item.due, strip_nul_bytes(item.text))


def remove_checkin_followup(store: ConversationStore, session_id: str, fid: str) -> None:
    def change(current: dict[str, str]) -> LabelGroupChange:
        key = f"{FOLLOWUP_PREFIX}{fid}"
        if key not in current:
            raise KeyError(fid)
        return _indexed_change(current, {}, {key})

    store.mutate_label_group(session_id, CHECKIN_LABEL_PREFIX, change)


def refresh_checkin_followups(store: ConversationStore, session_id: str) -> dict[str, str]:
    return store.mutate_label_group(
        session_id, CHECKIN_LABEL_PREFIX, lambda current: _indexed_change(current, {}, set())
    )


def settle_checkin_followups(
    store: ConversationStore,
    session_id: str,
    delivered: dict[str, str],
    outcome: Literal["accepted", "uncertain"],
) -> None:
    def change(current: dict[str, str]) -> LabelGroupChange:
        upserts: dict[str, str] = {}
        deletes: set[str] = set()
        for key, raw in delivered.items():
            if current.get(key) != raw:
                continue
            if outcome == "accepted":
                deletes.add(key)
            elif (item := decode_followup(raw)) is not None:
                upserts[key] = encode_followup(replace(item, uncertain=True))
        return _indexed_change(current, upserts, deletes)

    store.mutate_label_group(session_id, CHECKIN_LABEL_PREFIX, change)
