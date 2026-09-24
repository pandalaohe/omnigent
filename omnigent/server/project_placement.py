"""Resolve a project's directory and default host from its configuration."""

from __future__ import annotations

import asyncio
import ntpath
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from omnigent.entities import Project, ProjectHostBinding, ProjectHostEntry
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes._workspace_validation import (
    _is_subpath_of,
    _is_windows_absolute_path,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore


@dataclass(frozen=True)
class HostRoot:
    """A project's directory on one host.

    :param host_id: Host carrying this root.
    :param workspace: Directory path on that host.
    :param source: Entry, binding or project config that supplied the path.
    :param checkout: Repository a worktree is created from on that host
        (R-CHECKOUT), or ``None`` when none is registered.
    """

    host_id: str
    workspace: str
    source: Literal["entry", "binding", "config"]
    checkout: str | None = None


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


def _entry_on_host(entries: Iterable[ProjectHostEntry], host_id: str) -> ProjectHostEntry | None:
    """Return the host's entry, or ``None`` when it has none.

    :param entries: The project's per-host entries.
    :param host_id: Target host.
    :returns: The matching entry, if any.
    """
    for entry in entries:
        if entry.host_id == host_id:
            return entry
    return None


def checkout_on_host(
    bindings: list[ProjectHostBinding],
    entries: list[ProjectHostEntry],
    host_id: str,
) -> str | None:
    """Return the repository a worktree is created from on one host.

    R-CHECKOUT, ungated by the flag and the collaboration switch: a registered
    primary enabled binding is the repository source whatever those say; with
    no such binding the entry itself is the repository (single-repository
    projects); otherwise none.

    :param bindings: The project's per-host bindings.
    :param entries: The project's per-host entries.
    :param host_id: Target host.
    :returns: The checkout path, or ``None`` when none is registered.
    """
    for binding in bindings:
        if binding.host_id == host_id and binding.is_primary and binding.enabled:
            return binding.workspace
    entry = _entry_on_host(entries, host_id)
    return entry.workspace if entry is not None else None


def root_on_host(
    project: Project,
    bindings: list[ProjectHostBinding],
    host_id: str,
    *,
    gates_on: bool,
    entries: list[ProjectHostEntry] | None = None,
) -> HostRoot | None:
    """Return the project's root on one host, if configured.

    R-ROOT: once a project has an entry on any host, entries are its only
    roots — that host's entry when it exists, otherwise no root on that host
    (deleting an entry means "no directory on that host", never a silent
    fallback to a binding or the config). A project without entries keeps the
    legacy binding → config resolution.

    :param project: Project whose root is resolved.
    :param bindings: Its per-host directory bindings.
    :param host_id: Target host.
    :param gates_on: Whether bindings may supply roots.
    :param entries: Its per-host entries; empty for a project without entries.
    :returns: The preferred root, or ``None``.
    """
    if host_id == "__sandbox__":
        return None
    if entries:
        entry = _entry_on_host(entries, host_id)
        if entry is None:
            return None
        return HostRoot(
            host_id,
            entry.workspace,
            "entry",
            checkout_on_host(bindings, entries, host_id),
        )
    if gates_on:
        for binding in bindings:
            if binding.host_id == host_id and binding.is_primary and binding.enabled:
                return HostRoot(
                    host_id,
                    binding.workspace,
                    "binding",
                    checkout_on_host(bindings, entries or [], host_id),
                )
    config = project.config
    workspace = config.get("workspace")
    if config.get("host_id") == host_id and isinstance(workspace, str) and workspace:
        return HostRoot(
            host_id,
            workspace,
            "config",
            checkout_on_host(bindings, entries or [], host_id),
        )
    return None


def host_roots(
    project: Project,
    bindings: list[ProjectHostBinding],
    *,
    gates_on: bool,
    entries: list[ProjectHostEntry] | None = None,
) -> list[HostRoot]:
    """Return one preferred root for each configured host.

    With entries, one root per entry host (sorted) — the config host is not a
    root of its own once the project has entries. Without, the legacy
    binding/config hosts unchanged.

    :param project: Project whose roots are resolved.
    :param bindings: Its per-host directory bindings.
    :param gates_on: Whether bindings may supply roots.
    :param entries: Its per-host entries; empty for a project without entries.
    :returns: Preferred roots ordered by host id.
    """
    if entries:
        host_ids = {entry.host_id for entry in entries if entry.host_id != "__sandbox__"}
        return [
            root
            for host_id in sorted(host_ids)
            if (
                root := root_on_host(
                    project, bindings, host_id, gates_on=gates_on, entries=entries
                )
            )
            is not None
        ]
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


def _same_canonical_path(first: str, second: str) -> bool:
    """Return whether two host-canonical paths name the same directory.

    ``_is_subpath_of`` treats equal paths as contained; R-PLACE needs them
    distinguished ("strictly inside"). Windows paths compare case-insensitively
    with separators normalised, so ``D:\\P`` and ``d:\\p\\`` are the same
    directory.

    :param first: A host-canonical path.
    :param second: Another host-canonical path.
    :returns: ``True`` when both name the same directory.
    """
    if _is_windows_absolute_path(first) or _is_windows_absolute_path(second):
        return ntpath.normcase(ntpath.normpath(first)) == ntpath.normcase(ntpath.normpath(second))
    return first.rstrip("/") == second.rstrip("/")


def place_session(
    entry: str | None,
    target: str,
    *,
    git_used: bool,
    entry_within_agent_boundary: bool,
) -> tuple[str, str | None]:
    """Decide a session's launch directory and recorded worktree (R-PLACE 4–5).

    When the project has an entry on the target host, the target is strictly
    inside it, and the entry passes the same agent-boundary check the target
    passed, the session launches at the entry and records the target as its
    worktree — the entry's grants may not reach the target otherwise. Every
    other case launches at the target, recording it as the worktree only when
    git worktree creation or binding ran.

    :param entry: The project's entry on the target host, or ``None``.
    :param target: The validated directory the session would launch in.
    :param git_used: Whether a git worktree was created or bound for this
        session.
    :param entry_within_agent_boundary: Whether the entry passes the agent's
        workspace validation.
    :returns: ``(workspace, worktree)`` to persist.
    """
    if (
        entry is not None
        and entry_within_agent_boundary
        and not _same_canonical_path(target, entry)
        and _is_subpath_of(target, entry)
    ):
        return entry, target
    return target, target if git_used else None


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


async def load_entries(
    binding_store: ProjectHostBindingStore | None, project_id: str
) -> list[ProjectHostEntry]:
    """Load the project's per-host entries off-thread, or return an empty list.

    :param binding_store: Store, if configured.
    :param project_id: Project to query.
    :returns: Its entries, or an empty list without a store.
    """
    if binding_store is None:
        return []
    return await asyncio.to_thread(binding_store.list_entries, project_id)


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
