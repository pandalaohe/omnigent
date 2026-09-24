"""Project-host-binding store — persists a project's per-host directories.

One global project identity, N per-host records each naming a local path.
This store owns the ``project_host_bindings`` table. Rows are keyed by
``(project_id, host_id, name)`` and carry a ``revision`` the store bumps on
every change; assignments pin the value they started against. At most one
binding per ``(project_id, host_id)`` is primary.

It also owns the ``project_host_entries`` table: one project directory per
host, keyed by ``(project_id, host_id)``, with no revision. An entry is where
a project's sessions open; bindings stay the repository source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import ProjectHostBinding, ProjectHostEntry
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
        path_verified_at: int | None = None,
    ) -> ProjectHostBinding:
        """
        Register a binding or revise it.

        Looks up the row by ``(project_id, host_id, name)``. A missing row
        is inserted at ``revision`` 1; an existing row whose fields differ
        is updated and bumped to ``revision + 1``; an identical row is
        returned unchanged (no bump).

        A verification timestamp refreshes ``path_verified_at`` (and
        ``updated_at``) without bumping ``revision`` when nothing else
        changed, so a periodic re-verify does not invalidate snapshots
        pinned against the binding's revision.

        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: Binding name; ``primary`` is the conventional value.
        :param repository_id: Which registered repository this directory
            holds.
        :param workspace: Absolute path as the host canonicalised it.
        :param is_primary: Whether this is the host's primary binding.
        :param enabled: Whether the binding is eligible at claim time.
        :param path_verified_at: Unix epoch seconds of a successful
            ``host.stat`` to stamp, or ``None`` to leave the stamp alone.
        :returns: The inserted or updated :class:`ProjectHostBinding`.
        :raises DuplicatePrimaryBindingError: If ``is_primary`` is true and
            another binding is already primary for this ``(project, host)``.
        :raises OmnigentError: ``INVALID_INPUT`` when ``repository_id``
            names no repository of ``project_id``.
        """
        ...

    @abstractmethod
    def record_verification(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        workspace: str,
        path_verified_at: int,
    ) -> ProjectHostBinding | None:
        """
        Stamp a verification without overwriting concurrent changes.

        Loads the row by id under the project lock; ``None`` when it is
        gone or its ``revision`` no longer equals ``expected_revision``.
        A moved canonical path is stored and bumps ``revision``; an
        unchanged path only refreshes ``path_verified_at``.

        :param binding_id: Opaque binding identifier.
        :param expected_revision: The ``revision`` the caller read before
            the host round trip; a mismatch means another writer moved
            first.
        :param workspace: Canonical path the host just returned.
        :param path_verified_at: Unix epoch seconds of the successful
            ``host.stat`` to stamp.
        :returns: The refreshed :class:`ProjectHostBinding`, or ``None``
            when the row is gone or changed under the caller.
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
    def get_by_name(
        self, *, project_id: str, host_id: str, name: str
    ) -> ProjectHostBinding | None:
        """
        Return one host's binding by name, or ``None`` if not found.

        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: The binding name.
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

    @abstractmethod
    def list_entries(self, project_id: str) -> list[ProjectHostEntry]:
        """
        List a project's entries ordered by ``host_id ASC``.

        :param project_id: The project whose entries to return.
        :returns: List of :class:`ProjectHostEntry` instances.
        """
        ...

    @abstractmethod
    def put_entry(self, project_id: str, host_id: str, workspace: str) -> ProjectHostEntry:
        """
        Register a host's entry path or move it.

        Looks up the row by ``(project_id, host_id)``. A missing row is
        inserted with ``updated_at`` NULL; an existing row whose workspace
        differs is updated and stamped with a fresh ``updated_at``; an
        identical row is returned unchanged.

        :param project_id: The project the entry belongs to.
        :param host_id: The host the directory lives on.
        :param workspace: Absolute path as the host canonicalised it.
        :returns: The inserted or updated :class:`ProjectHostEntry`.
        :raises OmnigentError: ``NOT_FOUND`` when the project does not exist.
        """
        ...

    @abstractmethod
    def delete_entry(self, project_id: str, host_id: str) -> bool:
        """
        Delete a host's entry. Idempotent.

        :param project_id: The project the entry belongs to.
        :param host_id: The host whose entry to remove.
        :returns: ``True`` if removed; ``False`` if not found.
        :raises OmnigentError: ``NOT_FOUND`` when the row exists but its
            project does not.
        """
        ...

    @abstractmethod
    def entry_at_or_under(self, host_id: str, workspace: str) -> bool:
        """
        Return whether any project has an entry at or inside ``workspace``.

        Tenant-scoped and project-agnostic: the guard that stops worktree
        cleanup from removing a directory that is, or holds, some project's
        entry. A nested entry (``/repo-worktrees/topic/subproject``) keeps
        the whole parent tree.

        :param host_id: The host holding the directory.
        :param workspace: The canonical directory path to match.
        :returns: ``True`` when an entry row names the pair or lies under it.
        """
        ...
