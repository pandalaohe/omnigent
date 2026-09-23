"""Resolve a project's directory and default host from its configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from omnigent.entities import Project, ProjectHostBinding
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore


@dataclass(frozen=True)
class HostRoot:
    """A project's directory on one host.

    :param host_id: Host carrying this root.
    :param workspace: Directory path on that host.
    :param source: Binding or project config that supplied the path.
    """

    host_id: str
    workspace: str
    source: Literal["binding", "config"]


@dataclass(frozen=True)
class DefaultHost:
    """The preferred host and the rule that selected it.

    :param host_id: Preferred host, or ``None``.
    :param reason: Selection reason.
    """

    host_id: str | None
    reason: Literal["config", "single_root", "ambiguous", "none"]


def bindings_apply(project: Project, feature_flags: FeatureFlags | None) -> bool:
    """Return whether binding roots are enabled for this project.

    :param project: Project whose collaboration switch is checked.
    :param feature_flags: Deployment feature snapshot, if available.
    :returns: Whether both the project switch and assignment flag are on.
    """
    return bool(
        feature_flags is not None
        and feature_flags.enabled(Feature.PROJECT_ASSIGNMENTS)
        and project.collaboration_enabled
    )


def root_on_host(
    project: Project,
    bindings: list[ProjectHostBinding],
    host_id: str,
    *,
    gates_on: bool,
) -> HostRoot | None:
    """Return the project's root on one host, if configured.

    :param project: Project whose root is resolved.
    :param bindings: Its per-host directory bindings.
    :param host_id: Target host.
    :param gates_on: Whether bindings may supply roots.
    :returns: The preferred root, or ``None``.
    """
    if host_id == "__sandbox__":
        return None
    if gates_on:
        for binding in bindings:
            if binding.host_id == host_id and binding.is_primary and binding.enabled:
                return HostRoot(host_id, binding.workspace, "binding")
    config = project.config
    workspace = config.get("workspace")
    if config.get("host_id") == host_id and isinstance(workspace, str) and workspace:
        return HostRoot(host_id, workspace, "config")
    return None


def host_roots(
    project: Project, bindings: list[ProjectHostBinding], *, gates_on: bool
) -> list[HostRoot]:
    """Return one preferred root for each configured host.

    :param project: Project whose roots are resolved.
    :param bindings: Its per-host directory bindings.
    :param gates_on: Whether bindings may supply roots.
    :returns: Preferred roots ordered by host id.
    """
    host_ids = (
        {binding.host_id for binding in bindings if binding.host_id != "__sandbox__"}
        if gates_on
        else set()
    )
    config_host_id = project.config.get("host_id")
    if isinstance(config_host_id, str) and config_host_id and config_host_id != "__sandbox__":
        host_ids.add(config_host_id)
    return [
        root
        for host_id in sorted(host_ids)
        if (root := root_on_host(project, bindings, host_id, gates_on=gates_on)) is not None
    ]


def default_host(
    project: Project,
    roots: list[HostRoot],
    *,
    eligible_host_ids: frozenset[str] | None = None,
) -> DefaultHost:
    """Choose the config host or a single eligible rooted host.

    :param project: Project whose default host is resolved.
    :param roots: Preferred roots from :func:`host_roots`.
    :param eligible_host_ids: Caller-owned, existing hosts; ``None`` disables filtering.
    :returns: Host choice and its reason.
    """
    config_host_id = project.config.get("host_id")
    if config_host_id == "__sandbox__":
        return DefaultHost(None, "none")
    if isinstance(config_host_id, str) and config_host_id:
        return DefaultHost(config_host_id, "config")
    rooted = [
        root.host_id
        for root in roots
        if eligible_host_ids is None or root.host_id in eligible_host_ids
    ]
    if len(rooted) == 1:
        return DefaultHost(rooted[0], "single_root")
    return DefaultHost(None, "ambiguous" if rooted else "none")


async def load_bindings(
    binding_store: ProjectHostBindingStore | None, project_id: str
) -> list[ProjectHostBinding]:
    """Load the project's bindings off-thread, or return an empty list.

    :param binding_store: Store, if configured.
    :param project_id: Project to query.
    :returns: Its bindings, or an empty list without a store.
    """
    if binding_store is None:
        return []
    return await asyncio.to_thread(binding_store.list_by_project, project_id)


async def load_eligible_host_ids(
    host_store: HostStore | None, user_id: str | None, host_ids: Iterable[str]
) -> frozenset[str] | None:
    """Return existing hosts owned by the caller, or no filter without a store.

    :param host_store: Host store, if configured.
    :param user_id: Caller, or ``None`` when auth is disabled.
    :param host_ids: Candidate hosts to check.
    :returns: Eligible host ids, or ``None`` without a store.
    """
    if host_store is None:
        return None
    hosts = await asyncio.gather(
        *(asyncio.to_thread(host_store.get_host, host_id) for host_id in set(host_ids))
    )
    return frozenset(
        host.host_id
        for host in hosts
        if host is not None
        and getattr(host, "deleted_at", None) is None
        and (user_id is None or host.user_id == user_id)
    )
