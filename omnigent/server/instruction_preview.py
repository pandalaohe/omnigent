"""Render the instruction text a route or session receives from Omnigent."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omnigent.harness_aliases import canonicalize_harness, native_terminal_name
from omnigent.runtime.instruction_layers import (
    compose_role,
    compose_route,
    compose_session_text,
    select_role_layers,
)
from omnigent.runtime.prompt import (
    InstructionPart,
    child_session_question_instruction,
    instruction_parts,
    native_startup_instruction_parts,
    session_instruction_parts,
)
from omnigent.spec import AgentSpec
from omnigent.stores.global_instructions_store import GlobalInstructionRevision


def _route_layers(
    revisions: Sequence[GlobalInstructionRevision], host_id: str | None, agent_key: str | None
) -> list[GlobalInstructionRevision]:
    keys = {("shared", "", "")}
    if agent_key:
        keys.add(("agent", agent_key, ""))
    if host_id:
        keys.add(("host", "", host_id))
    if host_id and agent_key:
        keys.add(("independent", agent_key, host_id))
    return [r for r in revisions if (r.scope, r.agent_key, r.host_id) in keys]


def _layer_part(revision: GlobalInstructionRevision, *, sent: bool) -> dict[str, Any]:
    return {
        "kind": f"{revision.scope}_block"
        if revision.scope in {"mother", "child"}
        else "user_layers",
        "text": revision.text,
        "chars": len(revision.text),
        "scope": revision.scope,
        "agent_key": revision.agent_key,
        "host_id": revision.host_id,
        "revision_id": revision.id,
        "sent": sent,
        "readonly": False,
    }


def _framework_part(part: InstructionPart) -> dict[str, Any]:
    return {
        "kind": part.kind,
        "text": part.text,
        "chars": len(part.text or ""),
        "sent": True,
        "readonly": True,
    }


def preview_instructions(
    revisions: Sequence[GlobalInstructionRevision],
    *,
    host_id: str | None,
    agent_key: str | None,
    spec: AgentSpec | None,
    name: str,
    harness: str | None,
    has_parent: bool,
    spawn: bool,
    peer: bool,
    quota_line: str = "",
) -> dict[str, Any]:
    """Render known delivery bytes and label every visible revision."""
    candidates = _route_layers(revisions, host_id, agent_key)
    effective = compose_route(candidates)
    role = "child" if has_parent else "mother" if spawn else None
    role_layers = select_role_layers(revisions, role, host_id, agent_key) if role else []
    role_text = compose_role(role_layers)
    sent_layers = list(effective.revisions) + role_layers
    global_text = compose_session_text(
        effective.text,
        has_parent=has_parent,
        spawn=spawn,
        mother_text=role_text if role == "mother" else "",
        child_text=role_text if role == "child" else "",
        quota_line=quota_line,
    )
    sent_user_parts = [_layer_part(r, sent=True) for r in sent_layers]
    if role == "mother" and role_layers and quota_line:
        sent_user_parts.append(_framework_part(InstructionPart("quota_line", quota_line)))
    replaced = [_layer_part(r, sent=False) for r in candidates if r not in sent_layers]
    if spec is None:
        return {
            "subject": {"name": name, "harness": harness},
            "parts": sent_user_parts + replaced,
            "text": global_text,
            "total_chars": len(global_text),
            "framework_unavailable": True,
        }
    session = session_instruction_parts(
        global_text or None,
        workspace=None,
        worktree=None,
        peer_messaging_enabled=peer,
        has_parent=has_parent,
    )
    terminal = native_terminal_name(harness)
    canonical = canonicalize_harness(harness)
    native = terminal is not None and terminal != "opencode"
    if native:
        delivered = native_startup_instruction_parts(spec, session)
    else:
        # SDK/OpenCode receives one runner-folded session block. The builder
        # strips only its outer whitespace; never strip each user layer.
        folded = "\n\n".join(p.text for p in session if p.text is not None)
        delivered = instruction_parts(
            spec,
            None,
            [],
            framework_instructions=[InstructionPart("user_layers", folded)] if folded else [],
            fallback=terminal != "opencode" and canonical not in {"hermes", "kimi"},
        )

    parts: list[dict[str, Any]] = []
    session_placeholders = [
        _framework_part(InstructionPart(kind, None)) for kind in ("worktree", "project_code")
    ]
    turn_placeholders = [
        _framework_part(InstructionPart(kind, None)) for kind in ("per_request", "skills_hint")
    ]
    inserted_turn_placeholders = False
    inserted_placeholders = False
    for part in delivered:
        if not inserted_turn_placeholders and part.kind != "author":
            parts.extend(turn_placeholders)
            inserted_turn_placeholders = True
        if part.kind in {"peer_grant", "child_rule"} and not inserted_placeholders:
            parts.extend(session_placeholders)
            inserted_placeholders = True
        if part.kind == "user_layers":
            if not inserted_placeholders:
                parts.extend(session_placeholders)
                inserted_placeholders = True
            # Expand the folded block into the same source sections and
            # normalize only the first and final section in the SDK family.
            expanded = sent_user_parts.copy()
            if not native:
                expanded.extend(_framework_part(p) for p in session if p.kind != "user_layers")
                if expanded:
                    expanded = [item.copy() for item in expanded]
                    sent = [item for item in expanded if item["sent"]]
                    if sent:
                        sent[0]["text"] = sent[0]["text"].lstrip()
                        sent[0]["chars"] = len(sent[0]["text"])
                        sent[-1]["text"] = sent[-1]["text"].rstrip()
                        sent[-1]["chars"] = len(sent[-1]["text"])
            parts.extend(expanded)
        else:
            parts.append(_framework_part(part))
    if not inserted_turn_placeholders:
        parts.extend(turn_placeholders)
    if not inserted_placeholders:
        parts.extend(session_placeholders)
    parts.append(_framework_part(InstructionPart("routing_note", None)))
    question = (
        child_session_question_instruction(peer)
        if native and has_parent and terminal in {"claude", "codex"}
        else None
    )
    if question:
        parts.append(_framework_part(InstructionPart("child_question", question)))
    parts.extend(replaced)
    composed = "\n\n".join(p["text"] for p in parts if p["sent"] and p["text"] is not None)
    return {
        "subject": {"name": name, "harness": harness},
        "parts": parts,
        "text": composed,
        "total_chars": len(composed),
        "framework_unavailable": False,
    }
