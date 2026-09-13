"""Project-repository store — persists a project's registered repositories.

A project may span more than one repository because a coordination root with
a nested clone is two repositories, not two directories. This store owns the
``project_repositories`` table. Rows are keyed by ``(project_id, name)`` and
carry a ``revision`` the store bumps on every change; assignments pin the
value they were created against.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import ProjectRepository


class ProjectRepositoryStore(ABC):
    """
    Abstract base for project-repository persistence.

    Manages the lifecycle of registered repositories (upsert / get / list /
    delete). Reads and writes are scoped by the ambient workspace; rows carry
    no owner of their own — ownership is the project's.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project-repository store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def upsert(
        self,
        *,
        project_id: str,
        name: str,
        remote_url: str,
        default_branch: str,
        context_manifest_path: str = ".agents/project/manifest.json",
    ) -> ProjectRepository:
        """
        Register a repository or revise its registration.

        Looks up the row by ``(project_id, name)``. A missing row is
        inserted at ``revision`` 1; an existing row whose fields differ is
        updated and bumped to ``revision + 1``; an identical row is returned
        unchanged (no bump).

        :param project_id: The project to register the repository on.
        :param name: Stable identity used by assignments; unique per project.
        :param remote_url: The shared remote. Carries no credentials.
        :param default_branch: The repository's default branch name.
        :param context_manifest_path: Repo-relative path of the
            project-context manifest.
        :returns: The inserted or updated :class:`ProjectRepository`.
        """
        ...

    @abstractmethod
    def get(self, repository_id: str) -> ProjectRepository | None:
        """
        Return a registered repository by id, or ``None`` if not found.

        :param repository_id: Opaque repository identifier.
        :returns: The :class:`ProjectRepository` if found, else ``None``.
        """
        ...

    @abstractmethod
    def get_by_name(self, *, project_id: str, name: str) -> ProjectRepository | None:
        """
        Return a project's repository by name, or ``None`` if not found.

        :param project_id: The project the repository is registered on.
        :param name: The stable repository name.
        :returns: The :class:`ProjectRepository` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_by_project(self, project_id: str) -> list[ProjectRepository]:
        """
        List a project's registered repositories ordered by
        ``created_at ASC, id ASC``.

        :param project_id: The project whose repositories to return.
        :returns: List of :class:`ProjectRepository` instances.
        """
        ...

    @abstractmethod
    def delete(self, repository_id: str) -> bool:
        """
        Delete a registered repository. Idempotent.

        :param repository_id: Opaque repository identifier.
        :returns: ``True`` if removed; ``False`` if not found.
        """
        ...
