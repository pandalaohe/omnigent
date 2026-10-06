"""Launch a saved library Agent (``ca_`` id) as a session.

A saved Agent row (``POST /v1/custom-agents``) is owner-scoped library
storage, not an ``agents`` row bound to a session, so the scheduled fire path
cannot create a conversation from its id the way it does for an ``ag_`` agent.
This module is the one server-side operation that turns one into a session:
copy the stored bundle into a fresh session-scoped ``ag_`` agent, stamp the
metadata the fire path stamps for a stored agent (title, terminal-first
presentation, permission labels, template id), apply the per-session
overrides, and return the created ids.

The scheduled create/update validation shares :func:`load_library_agent_bundle`
so the owner check, the 404 shape, and the parsed spec cannot drift from the
fire-time launch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_plugins import UI_MODE_LABEL_KEY, UI_MODE_TERMINAL_VALUE
from omnigent.member_snapshot import LIBRARY_AGENT_TEMPLATE_LABEL_KEY
from omnigent.sdk_permission_modes import (
    CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY,
    CODEX_SDK_APPROVAL_MODE_LABEL_KEY,
    CODEX_SDK_APPROVAL_MODES,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, local_single_user_enabled
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes._session_create_validation import (
    CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES,
)
from omnigent.server.schemas import CreatedSessionResponse, SessionCreateMetadata

# Saved library Agent ids are minted as ``ca_<uuid>`` by the custom-agents
# route; the web picker keys the same prefix. Nothing else starts with it.
_LIBRARY_AGENT_ID_PREFIX = "ca_"

# The label marking the saved Agent a session was created from, so the picker
# can associate a launched session with its library template. Value is the
# ``ca_`` id, matching the interactive New Chat launch and the web constant.
AGENT_TEMPLATE_LABEL_KEY = LIBRARY_AGENT_TEMPLATE_LABEL_KEY


def is_library_agent_id(agent_id: str) -> bool:
    """Return whether *agent_id* names a saved library Agent, not an agents row."""
    return agent_id.startswith(_LIBRARY_AGENT_ID_PREFIX)


def library_agent_owner_id(user_id: str | None) -> str:
    """Map a request/task owner to the custom-agents store key.

    Single-user servers persist ``None`` (no auth), while the store keys every
    row — including the local user's — by identity, resolving ``None`` to
    :data:`RESERVED_USER_LOCAL`. Mirrors the custom-agents route's owner
    resolution so a task and the picker agree on whose library a row is in.
    """
    return user_id or RESERVED_USER_LOCAL


async def load_library_agent_bundle(
    *,
    custom_agents_store: CustomAgentsStore,
    artifact_store: Any,
    owner: str | None,
    agent_id: str,
) -> tuple[Any, bytes]:
    """Load an owner's saved Agent bundle and its validated spec.

    :param custom_agents_store: Owner-scoped library storage.
    :param artifact_store: Store holding the row's bundle bytes.
    :param owner: Task/request owner; ``None`` maps to the local identity.
    :param agent_id: The ``ca_`` id to load.
    :returns: ``(spec, bundle_bytes)``.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown, deleted, or
        another-owner id (one 404 shape, so ids aren't enumerable), and for a
        row whose bundle is missing.
    """
    try:
        row = await asyncio.to_thread(
            custom_agents_store.get, library_agent_owner_id(owner), agent_id
        )
    except OmnigentError as exc:
        if exc.code != ErrorCode.NOT_FOUND:
            raise
        # Same message an unknown AgentStore agent produces: a deleted Agent, an
        # unknown id, and another owner's id must be indistinguishable.
        raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND) from exc
    try:
        bundle = await asyncio.to_thread(artifact_store.get, row["bundle_location"])
    except KeyError as exc:
        raise OmnigentError("Custom Agent bundle not found", code=ErrorCode.NOT_FOUND) from exc
    spec = await asyncio.to_thread(
        validate_agent_bundle,
        bundle,
        enforce_handler_allowlist=not local_single_user_enabled(),
    )
    return spec, bundle


def library_agent_lead_harness(spec: Any) -> str | None:
    """Resolve the bundle's lead harness.

    The root spec is the lead member, so its executor's harness is the harness
    a launched session runs; a ``ca_`` row has no agent name to resolve by.
    """
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.models.model_catalog import spec_harness

    raw = spec_harness(spec)
    return canonicalize_harness(raw) or raw


def library_agent_presentation_labels(*, spec: Any, host_bound: bool) -> dict[str, str]:
    """Terminal-first presentation labels for a saved-Agent launch.

    Mirrors the interactive create path, but resolves the wrapper from the
    bundle's lead harness (see :func:`library_agent_lead_harness`): a native
    wrapper's own labels, else the REPL-terminal label a runner will host for a
    non-native bundle bound to a host. An in-process (hostless) session has no
    runner to host a terminal, so it stays Chat-only.
    """
    from omnigent.harness_aliases import is_native_harness
    from omnigent.native.native_coding_agents import native_coding_agent_for_harness

    harness = library_agent_lead_harness(spec)
    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is not None:
        return dict(native_agent.presentation_labels)
    if not host_bound or is_native_harness(harness):
        return {}
    return {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE}


@dataclass(frozen=True)
class LibraryAgentLaunch:
    """Per-session values a saved-Agent launch applies.

    ``permission_mode`` is the raw requested mode; the launch derives the
    native launch args or SDK label fit for the bundle's OWN harness, so a mode
    that does not match that harness is dropped rather than breaking the
    launch (mirroring the fire path's fail-safe stance).
    """

    title: str | None
    host_id: str | None
    workspace: str | None
    model_override: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None


async def launch_library_agent(
    *,
    custom_agents_store: CustomAgentsStore,
    artifact_store: Any,
    conversation_store: Any,
    host_store: Any | None = None,
    owner: str | None,
    agent_id: str,
    launch: LibraryAgentLaunch,
    project_config: dict | None = None,
    master: dict | None = None,
) -> CreatedSessionResponse:
    """Create a session-scoped copy of an owner's saved Agent bundle.

    The caller must have resolved and validated the target ``host_id`` /
    ``workspace`` (the bundle-cwd workspace check included); this operation
    only persists the session.

    :param custom_agents_store: Owner-scoped library storage.
    :param artifact_store: Store holding the row's bundle bytes.
    :param conversation_store: Store owning the session+agent insert.
    :param host_store: Host registrations, used to resolve a joint bundle's
        member snapshot (liveness, readiness, catalog default). ``None``
        resolves no member labels.
    :param owner: Task/request owner; ``None`` maps to the local identity.
    :param agent_id: The owned ``ca_`` id to launch.
    :param launch: Per-session values (title, host/workspace, overrides).
    :param project_config: The task's project config, whose per-host set
        supplies unset member values. ``None`` skips the project layers.
    :param master: The owner's ``calling_defaults`` master table. ``None``
        skips the master layer.
    :returns: The created session and its session-scoped agent id.
    :raises OmnigentError: ``NOT_FOUND`` when the owner's Agent or its bundle
        is gone.
    """
    spec, bundle = await load_library_agent_bundle(
        custom_agents_store=custom_agents_store,
        artifact_store=artifact_store,
        owner=owner,
        agent_id=agent_id,
    )
    harness = library_agent_lead_harness(spec)
    permission_mode = launch.permission_mode
    launch_args: list[str] | None = None
    if (
        harness == "claude-native"
        and permission_mode is not None
        and permission_mode in CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES
    ):
        launch_args = ["--permission-mode", permission_mode]
    labels = library_agent_presentation_labels(spec=spec, host_bound=launch.host_id is not None)
    if (
        harness == "claude-sdk"
        and permission_mode is not None
        and permission_mode in CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES
    ):
        labels[CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY] = permission_mode
    elif (
        harness == "codex"
        and permission_mode is not None
        and permission_mode in CODEX_SDK_APPROVAL_MODES
    ):
        labels[CODEX_SDK_APPROVAL_MODE_LABEL_KEY] = permission_mode
    labels[AGENT_TEMPLATE_LABEL_KEY] = agent_id
    # A joint bundle (2+ members) freezes its member snapshot on the session
    # before the synchronous persistence thread, like the interactive
    # multipart create; a 1-member bundle resolves to no labels. The saved
    # Agent's member hosts decide each member's host.
    from omnigent.server.routes._sessions.helpers import (
        _member_hosts_from_library_agent,
        _member_snapshot_labels,
    )

    labels.update(
        await _member_snapshot_labels(
            spec,
            host_id=launch.host_id,
            host_store=host_store,
            member_hosts=await _member_hosts_from_library_agent(
                template_id=agent_id,
                owner=owner,
                custom_agents_store=custom_agents_store,
            ),
            project_config=project_config,
            master=master,
        )
    )

    metadata = SessionCreateMetadata(
        title=launch.title,
        labels=labels,
        reasoning_effort=launch.reasoning_effort,
        host_id=launch.host_id,
        workspace=launch.workspace,
        terminal_launch_args=launch_args,
    )
    # Lazy: the orchestration module imports nearly every route module, while
    # this module is imported at fire/route module scope.
    from omnigent.server.routes._sessions.orchestration import _create_session_from_bundle

    result: CreatedSessionResponse = await asyncio.to_thread(
        _create_session_from_bundle,
        conversation_store,
        artifact_store,
        metadata,
        bundle,
        None,
        spec,
        None,
        None,
        owner,
    )
    if launch.model_override is not None:
        # The bundle-create metadata carries no model override; apply the
        # caller's AFTER creation, exactly as the fire path does for a stored
        # agent (the bundle spec seeds the default when no effort is given).
        await asyncio.to_thread(
            conversation_store.update_conversation,
            result.session_id,
            model_override=launch.model_override,
        )
    return result
