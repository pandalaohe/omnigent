"""Session member-snapshot labels shared by the server and the runner.

A joint agent's session freezes its 2+ members at create: the server writes
one ``omnigent.member.<role>`` label per member (the value is compact JSON),
and the runner reads them to lock a member's harness / model / effort and to
refuse work routed to a member that cannot run. Kept dependency-free so the
server routes and the runner agree on the shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

MEMBER_LABEL_PREFIX = "omnigent.member."
# Mirrors the conversation_labels columns (key ``String(128)``,
# value ``String(256)`` in db/db_models.py).
MEMBER_LABEL_KEY_MAX_CHARS = 128
MEMBER_LABEL_VALUE_MAX_CHARS = 256

# Reason codes stored in a member entry's ``unavailable`` field.
MEMBER_UNAVAILABLE_HOST_OFFLINE = "host_offline"
MEMBER_UNAVAILABLE_HARNESS_NOT_CONFIGURED = "harness_not_configured"
MEMBER_UNAVAILABLE_MODEL_MISSING = "model_missing"


def member_label_key(role: str) -> str:
    """Return the session label key holding *role*'s member snapshot."""
    return f"{MEMBER_LABEL_PREFIX}{role}"


def encode_member_entry(entry: Mapping[str, Any]) -> str:
    """Serialize one member entry to the label's compact JSON value."""
    return json.dumps(dict(entry), separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def parse_member_entry(value: str) -> dict[str, Any] | None:
    """Parse one label value back into an entry, or ``None`` when malformed."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def member_entries_from_labels(labels: Mapping[str, str] | None) -> dict[str, dict[str, Any]]:
    """Project ``{role: entry}`` for every well-formed member label."""
    entries: dict[str, dict[str, Any]] = {}
    for key, value in (labels or {}).items():
        if not key.startswith(MEMBER_LABEL_PREFIX):
            continue
        role = key[len(MEMBER_LABEL_PREFIX) :]
        entry = parse_member_entry(value)
        if role and entry is not None:
            entries[role] = entry
    return entries


def parse_role_mentions(text: str, roles: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(role, segment)`` for each ``@role`` / ``[role]`` mention.

    ``@role`` and ``[role]`` are equivalent, and only exact role names match —
    so a web ``[Attached: …]`` preamble (or any other bracketed text) never
    parses as a mention. A mention's segment is the text between it and the
    next mention, stripped; the text before the first mention belongs to no
    pair. Repeated mentions yield repeated pairs.
    """
    role_list = {role for role in roles if isinstance(role, str) and role}
    if not text or not role_list:
        return []
    ordered_roles = sorted(role_list, key=lambda role: (-len(role), role))
    alternation = "|".join(re.escape(role) for role in ordered_roles)
    pattern = re.compile(
        rf"(?:(?<![\w@-])@(?P<at>{alternation})(?![\w-]))|(?:\[(?P<square>{alternation})\])"
    )
    matches = list(pattern.finditer(text))
    pairs: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        role = match.group("at") or match.group("square")
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pairs.append((role, text[match.end() : segment_end].strip()))
    return pairs
