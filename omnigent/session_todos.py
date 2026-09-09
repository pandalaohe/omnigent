"""Validation for native-harness session plans forwarded to the Web UI."""

from __future__ import annotations

import json
from typing import Any

_VALID_STATUSES = {"pending", "in_progress", "completed"}
_MAX_TODOS = 100
_MAX_TEXT_LENGTH = 4096
_MAX_SERIALIZED_BYTES = 256 * 1024


def validate_session_todos(value: object) -> list[dict[str, Any]]:
    """Return only complete todo items from a forwarded list.

    The event handler rejects a non-list at the request boundary. Stored JSON
    is treated as untrusted and falls back to an empty list through this same
    validator.
    """
    if not isinstance(value, list):
        raise ValueError("session todos must be a list")
    if len(value) > _MAX_TODOS:
        raise ValueError(f"session todos cannot exceed {_MAX_TODOS} items")

    normalized: list[dict[str, Any]] = []
    for todo in value:
        if (
            not isinstance(todo, dict)
            or not isinstance(todo.get("content"), str)
            or not isinstance(todo.get("status"), str)
            or todo.get("status") not in _VALID_STATUSES
            or not isinstance(todo.get("activeForm"), str)
        ):
            continue
        content = todo["content"]
        active_form = todo["activeForm"]
        if len(content) > _MAX_TEXT_LENGTH or len(active_form) > _MAX_TEXT_LENGTH:
            raise ValueError(
                f"session todo text fields cannot exceed {_MAX_TEXT_LENGTH} characters"
            )
        normalized.append(
            {
                "content": content,
                "status": todo["status"],
                "activeForm": active_form,
            }
        )

    serialized_size = len(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if serialized_size > _MAX_SERIALIZED_BYTES:
        raise ValueError(f"session todos cannot exceed {_MAX_SERIALIZED_BYTES} serialized bytes")
    return normalized


__all__ = ["validate_session_todos"]
