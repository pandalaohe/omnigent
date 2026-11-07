"""Persist one model-bound context reading for native and SDK usage ingress."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, TypedDict

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.server.routes._sessions.common import (
    _LAST_AUTO_COMPACT_TOKEN_LIMIT_LABEL_KEY,
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)
from omnigent.stores.conversation_store import ConversationStore, LabelGroupChange

_MODEL_LABEL = "omnigent.last_context_model"
_LIMIT_LABELS: dict[
    Literal["context_model", "context_window", "auto_compact_token_limit"], str
] = {
    "context_model": _MODEL_LABEL,
    "context_window": _LAST_CONTEXT_WINDOW_LABEL_KEY,
    "auto_compact_token_limit": _LAST_AUTO_COMPACT_TOKEN_LIMIT_LABEL_KEY,
}


class ContextReading(TypedDict):
    context_model: str | None
    context_window: int | None
    auto_compact_token_limit: int | None


def _model(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= LABEL_VALUE_MAX_LEN
        else None
    )


def _positive(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _label_limit(value: str | None) -> int | None:
    try:
        return _positive(int(value)) if value else None
    except ValueError:
        return None


def context_reading_from_labels(labels: Mapping[str, str]) -> ContextReading | None:
    """Decode the same limits tuple for a snapshot and a committed usage event."""
    if not any(key in labels for key in _LIMIT_LABELS.values()):
        return None
    return {
        "context_model": _model(labels.get(_MODEL_LABEL)),
        "context_window": _label_limit(labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY)),
        "auto_compact_token_limit": _label_limit(
            labels.get(_LAST_AUTO_COMPACT_TOKEN_LIMIT_LABEL_KEY)
        ),
    }


def persist_context_reading(
    store: ConversationStore,
    session_id: str,
    patch: Mapping[str, object],
    *,
    observer: Callable[[dict[str, str], LabelGroupChange], LabelGroupChange] | None = None,
) -> dict[str, object]:
    """Apply a usage patch atomically and return its canonical context event fields.

    Missing limits inherit only from the same identified sampling model. Billing
    model metadata and token-only posts never establish or relabel limits.
    """
    has_limits = "context_window" in patch or "auto_compact_token_limit" in patch
    tokens = patch.get("context_tokens")
    has_tokens = type(tokens) is int and tokens >= 0
    if not has_limits and not has_tokens:
        return {}

    def change(labels: dict[str, str]) -> LabelGroupChange:
        upserts = {_LAST_CONTEXT_TOKENS_LABEL_KEY: str(tokens)} if has_tokens else {}
        deletes: set[str] = set()
        if has_limits:
            previous = context_reading_from_labels(labels)
            model = _model(patch.get("context_model"))
            same_model = (
                model is not None and previous is not None and previous["context_model"] == model
            )
            for field, label in _LIMIT_LABELS.items():
                if field == "context_model":
                    upserts[label] = model or ""
                    continue
                value = (
                    _positive(patch[field])
                    if field in patch
                    else previous[field]
                    if same_model and previous is not None
                    else None
                )
                if value is None:
                    deletes.add(label)
                else:
                    upserts[label] = str(value)
        update = LabelGroupChange(upserts=upserts, deletes=deletes)
        return observer(labels, update) if observer is not None else update

    labels = store.mutate_label_group(
        session_id, "omnigent." if observer is not None else "omnigent.last_", change
    )
    result: dict[str, object] = {"context_tokens": tokens} if has_tokens else {}
    if has_limits:
        reading = context_reading_from_labels(labels)
        if reading is not None:
            result.update(reading)
    return result
