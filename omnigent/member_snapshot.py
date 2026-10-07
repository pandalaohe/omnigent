"""Session member-snapshot labels shared by the server and the runner.

A joint agent's session freezes its 2+ members at create: the server writes
one ``omnigent.member.<role>`` label per member (the value is compact JSON),
and the runner reads them to lock a member's harness / model / effort and to
refuse work routed to a member that cannot run. One session lock label under
the same prefix records whether the members lock. Kept dependency-free so the
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

# Session label recording the saved library Agent (``ca_`` id) a session was
# created from. Only such a session locks members to the frozen snapshot.
LIBRARY_AGENT_TEMPLATE_LABEL_KEY = "omnigent:agent-template-id"

# Session label fixing at create whether the members lock ("true"/"false").
# Under the member prefix, so reserved-label refusal, continuation copy and
# agent-switch drop cover it; ":" is no role and the value is no JSON object.
MEMBER_LOCK_LABEL_KEY = f"{MEMBER_LABEL_PREFIX}:locked"

# In-memory entry field carrying the lock, projected from the session lock
# label; entries written before that label existed may carry it themselves.
MEMBER_LOCKED_FIELD = "locked"

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
    """Project ``{role: entry}`` for every well-formed member label.

    An entry's own ``locked`` field wins; otherwise it is fixed from the
    session lock label, so a pre-lock-label session still resolves its lock.
    """
    session_lock = session_member_lock(labels)
    entries: dict[str, dict[str, Any]] = {}
    for key, value in (labels or {}).items():
        if not key.startswith(MEMBER_LABEL_PREFIX):
            continue
        role = key[len(MEMBER_LABEL_PREFIX) :]
        entry = parse_member_entry(value)
        if role and entry is not None:
            entry.setdefault(MEMBER_LOCKED_FIELD, session_lock)
            entries[role] = entry
    return entries


def session_member_lock(labels: Mapping[str, str] | None) -> bool:
    """Return whether *labels* freeze the members at create.

    The session lock label wins; without it — a session created before the
    label existed — the library-Agent template label decides.
    """
    if labels and MEMBER_LOCK_LABEL_KEY in labels:
        return labels[MEMBER_LOCK_LABEL_KEY] == "true"
    return launched_from_library_agent(labels)


def launched_from_library_agent(labels: Mapping[str, str] | None) -> bool:
    """Return whether *labels* mark a session launched from a saved library agent.

    Evaluated on a session's create-time labels: only a session started from a
    user's saved library joint agent (the template label carries its ``ca_``
    id) locks members to the frozen snapshot; built-in and uploaded joint
    agents keep per-dispatch choice and parent-model inheritance.
    """
    template_id = (labels or {}).get(LIBRARY_AGENT_TEMPLATE_LABEL_KEY)
    return isinstance(template_id, str) and template_id.startswith("ca_")


def member_lock_applies(entry: Mapping[str, Any]) -> bool:
    """Return whether the member *entry* was frozen by a library-agent launch."""
    return entry.get(MEMBER_LOCKED_FIELD) is True


def frozen_member_lock_label(labels: Mapping[str, str] | None) -> dict[str, str]:
    """Return the session lock label to fix before a template-label write.

    Save as Agent stamps a library id onto a running session; fixing the
    pre-save lock first keeps a non-library session's members unlocked.
    """
    if not labels or MEMBER_LOCK_LABEL_KEY in labels:
        return {}
    if not any(key.startswith(MEMBER_LABEL_PREFIX) for key in labels):
        return {}
    return {MEMBER_LOCK_LABEL_KEY: "true" if launched_from_library_agent(labels) else "false"}


# The web composer's attachment preamble. Its text can carry ``@`` inside a
# file path (``[Attached: /tmp/@executor.txt]``), so mentions are matched
# against the text with every attachment span removed.
_ATTACHED_SPAN_RE = re.compile(r"\[Attached:[^\]]*\]")


def parse_role_mentions(text: str, roles: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(role, segment)`` for each ``@role`` / ``[role]`` mention.

    ``@role`` and ``[role]`` are equivalent, and only exact role names match.
    ``[Attached: …]`` spans are excluded before matching, so a path inside one
    can never surface as a mention. A mention's segment is the text between it
    and the next mention, stripped; the text before the first mention belongs
    to no pair. Repeated mentions yield repeated pairs.
    """
    role_list = {role for role in roles if isinstance(role, str) and role}
    if not text or not role_list:
        return []
    scrubbed = _ATTACHED_SPAN_RE.sub(" ", text)
    ordered_roles = sorted(role_list, key=lambda role: (-len(role), role))
    alternation = "|".join(re.escape(role) for role in ordered_roles)
    pattern = re.compile(
        rf"(?:(?<![\w@-])@(?P<at>{alternation})(?![\w-]))|(?:\[(?P<square>{alternation})\])"
    )
    matches = list(pattern.finditer(scrubbed))
    pairs: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        role = match.group("at") or match.group("square")
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(scrubbed)
        pairs.append((role, scrubbed[match.end() : segment_end].strip()))
    return pairs
