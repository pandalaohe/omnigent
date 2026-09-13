"""Project-host-binding store — persists a project's per-host directories.

One global project identity, N per-host records each naming a local path.
This store owns the ``project_host_bindings`` table. Rows are keyed by
``(project_id, host_id, name)`` and carry a ``revision`` the store bumps on
every change; assignments pin the value they started against. At most one
binding per ``(project_id, host_id)`` is primary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import ProjectHostBinding
from omnigent.errors import ErrorCode, OmnigentError


class DuplicatePrimaryBindingError(OmnigentError):
    """A second primary binding was requested for one ``(project, host)``.

    The existing primary is left untouched — the store never silently
    clears it. Subclasses :class:`OmnigentError` with ``ALREADY_EXISTS``
    so the route layer maps it to a 409 without special-casing.
    """

    def __init__(self, project_id: str, host_id: str) -> None:
        """
        Initialize the duplicate-primary error.

        :param project_id: The project the binding was requested on.
        :param host_id: The host that already has a primary binding.
        """
        super().__init__(
            f"project {project_id} already has a primary binding on host {host_id}",
            code=ErrorCode.ALREADY_EXISTS,
        )


class ProjectHostBindingStore(ABC):
    """
    Abstract base for project-host-binding persistence.

    Manages the lifecycle of per-host directory bindings (upsert / get /
    list / delete). Reads and writes are scoped by the ambient workspace;
    rows carry no owner of their own — ownership is the project's.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project-host-binding store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def upsert(
        self,
        *,
        project_id: str,
        host_id: str,
        name: str,
        repository_id: str,
        workspace: str,
        is_primary: bool = False,
        enabled: bool = True,
    ) -> ProjectHostBinding:
        """
        Register a binding or revise it.

        Looks up the row by ``(project_id, host_id, name)``. A missing row
        is inserted at ``revision`` 1; an existing row whose fields differ
        is updated and bumped to ``revision + 1``; an identical row is
        returned unchanged (no bump).

        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: Binding name; ``primary`` is the conventional value.
        :param repository_id: Which registered repository this directory
            holds.
        :param workspace: Absolute path as the host canonicalised it.
        :param is_primary: Whether this is the host's primary binding.
        :param enabled: Whether the binding is eligible at claim time.
        :returns: The inserted or updated :class:`ProjectHostBinding`.
        :raises DuplicatePrimaryBindingError: If ``is_primary`` is true and
            another binding is already primary for this ``(project, host)``.
        """
        ...

    @abstractmethod
    def get(self, binding_id: str) -> ProjectHostBinding | None:
        """
        Return a binding by id, or ``None`` if not found.

        :param binding_id: Opaque binding identifier.
        :returns: The :class:`ProjectHostBinding` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list_by_project(self, project_id: str) -> list[ProjectHostBinding]:
        """
        List a project's bindings ordered by ``created_at ASC, id ASC``.

        :param project_id: The project whose bindings to return.
        :returns: List of :class:`ProjectHostBinding` instances.
        """
        ...

    @abstractmethod
    def list_by_host(self, *, project_id: str, host_id: str) -> list[ProjectHostBinding]:
        """
        List one host's bindings on a project ordered by
        ``created_at ASC, id ASC``.

        :param project_id: The project whose bindings to return.
        :param host_id: The host whose bindings to return.
        :returns: List of :class:`ProjectHostBinding` instances.
        """
        ...

    @abstractmethod
    def delete(self, binding_id: str) -> bool:
        """
        Delete a binding. Idempotent.

        :param binding_id: Opaque binding identifier.
        :returns: ``True`` if removed; ``False`` if not found.
        """
        ...
