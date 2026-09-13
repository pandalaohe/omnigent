"""Project-host-binding entity — persisted in the ``project_host_bindings`` table.

A :class:`ProjectHostBinding` is one host's local directory for a registered
repository: one global project identity, N per-host records each naming a
local path. Binding granularity is host → directory, never agent →
directory. This module holds the plain dataclass the store converts ORM rows
into.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProjectHostBinding:
    """
    One host's local directory for a registered repository.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param project_id: The project this binding belongs to.
    :param host_id: The bound host.
    :param name: Binding name; ``primary`` is the conventional value. Unique
        per (project, host).
    :param is_primary: At most one true per (project_id, host_id).
    :param repository_id: Which registered repository this directory holds.
    :param workspace: Absolute path as the host canonicalised it, never as
        typed.
    :param enabled: Disabled bindings are skipped at claim time.
    :param revision: Bumped on any change; assignments pin the value they
        started against.
    :param path_verified_at: Unix epoch seconds of the last successful
        ``host.stat``, or ``None`` if never verified.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        the row has never been updated.
    :param workspace_id: Tenant partition key that owns this row.
    """

    id: str
    project_id: str
    host_id: str
    name: str
    repository_id: str
    workspace: str
    revision: int
    created_at: int
    is_primary: bool = False
    enabled: bool = True
    path_verified_at: int | None = None
    updated_at: int | None = None
    workspace_id: int = 0
