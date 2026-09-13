"""Project-repository entity — persisted in the ``project_repositories`` table.

A :class:`ProjectRepository` is one registered repository of a collaboration
project. A project may span more than one repository because a coordination
root with a nested clone is two repositories, not two directories. This
module holds the plain dataclass the store converts ORM rows into.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProjectRepository:
    """
    A registered repository of a collaboration project.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param project_id: The project this repository is registered on.
    :param name: Stable identity used by assignments, unique per project.
    :param remote_url: The shared remote. Carries no credentials.
    :param default_branch: The repository's default branch name.
    :param context_manifest_path: Repo-relative path of the project-context
        manifest, e.g. ``".agents/project/manifest.json"``.
    :param revision: Bumped on any change; assignments pin the value they
        were created against.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        the row has never been updated.
    :param workspace_id: Tenant partition key that owns this row.
    """

    id: str
    project_id: str
    name: str
    remote_url: str
    default_branch: str
    context_manifest_path: str
    revision: int
    created_at: int
    updated_at: int | None = None
    workspace_id: int = 0
