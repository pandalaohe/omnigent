"""Project-host-entry entity — persisted in the ``project_host_entries`` table.

A :class:`ProjectHostEntry` is one project's own directory on one host: the
directory its sessions open in. Entries are a project setting of their own,
separate from the registered repositories and host bindings — an entry works
with collaboration off. This module holds the plain dataclass the store
converts ORM rows into.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectHostEntry:
    """
    One project's directory on one host.

    :param project_id: The project this entry belongs to.
    :param host_id: The host this entry lives on.
    :param workspace: Absolute path as the host canonicalised it, never as
        typed.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        the row has never been updated.
    :param workspace_id: Tenant partition key that owns this row.
    """

    project_id: str
    host_id: str
    workspace: str
    created_at: int
    updated_at: int | None = None
    workspace_id: int = 0
