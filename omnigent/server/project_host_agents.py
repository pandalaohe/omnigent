"""Validate a user's initial Agent for a new project-host connection."""

from __future__ import annotations

import asyncio

from fastapi import Request

from omnigent.calling_defaults import default_agent_for_host_or_legacy
from omnigent.db.db_models import InvalidUuidError
from omnigent.entities import Project
from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore


async def resolve_project_host_template(
    request: Request,
    project: Project,
    host_id: str,
    user_id: str | None,
    binding_store: ProjectHostBindingStore,
) -> tuple[str | None, str | None]:
    """Return a validated template or a notice; existing connections are skipped."""
    if default_agent_for_host_or_legacy(project.config, host_id):
        return None, None
    entries = await asyncio.to_thread(binding_store.list_entries, project.id)
    bindings = await asyncio.to_thread(
        binding_store.list_by_host, project_id=project.id, host_id=host_id
    )
    if any(entry.host_id == host_id for entry in entries) or bindings:
        return None, None
    state = request.app.state
    preferences = getattr(state, "user_preferences_store", None)
    if preferences is None:
        return None, None
    owner = user_id or RESERVED_USER_LOCAL
    envelope = await asyncio.to_thread(preferences.get, owner)
    templates = (envelope or {}).get("settings", {}).get("project_host_agents", {})
    template = templates.get(host_id) if isinstance(templates, dict) else None
    if not isinstance(template, str) or not template:
        return None, None

    from omnigent.server.routes._sessions.orchestration import _create_resolved_harness

    targets: list[tuple[str, str]] = []
    try:
        if template.startswith("ca_"):
            library = getattr(state, "custom_agents_store", None)
            saved = await asyncio.to_thread(library.get, owner, template) if library else None
            if saved:
                targets = [
                    (member.get("host_id") or host_id, member["harness"])
                    for member in saved.get("members") or []
                ]
                if not targets and saved.get("harness"):
                    targets = [(host_id, saved["harness"])]
        else:
            agents = getattr(state, "agent_store", None)
            agent = await asyncio.to_thread(agents.get, template) if agents else None
            if agent and (agent.kind != "user" or agent.created_by == owner):
                harness = _create_resolved_harness(
                    agent, None, getattr(state, "agent_cache", None)
                )
                if harness:
                    targets = [(host_id, harness)]
    except (InvalidUuidError, OmnigentError):
        targets = []
    hosts = getattr(state, "host_store", None)
    usable = bool(targets) and hosts is not None
    for target_host, harness in targets:
        host = await asyncio.to_thread(hosts.get_host, target_host) if hosts else None
        if (
            not host
            or host.user_id != owner
            or not host.configured_harnesses
            or host.configured_harnesses.get(
                harness,
                host.configured_harnesses.get("acp") if harness.startswith("acp:") else None,
            )
            is not True
        ):
            usable = False
    if usable:
        return template, None
    return (
        None,
        "New project agent unavailable on this host. "
        "Using the project's existing agent selection rules.",
    )
