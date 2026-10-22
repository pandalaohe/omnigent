"""Append-only instruction revisions, scoped by workspace and layer."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class InstructionRevisionConflict(ValueError):
    """The instruction layer changed since the caller read it."""


@dataclass(frozen=True)
class InstructionLayerKey:
    """Identify a shared, product, host, independent or session-role layer."""

    scope: str = "shared"
    agent_key: str = ""
    host_id: str = ""

    def __post_init__(self) -> None:
        expected = {
            "shared": (False, False),
            "agent": (True, False),
            "host": (False, True),
            "independent": (True, True),
            "mother": (False, False),
            "child": (False, False),
        }
        if self.scope not in expected or expected[self.scope] != (
            bool(self.agent_key.strip()),
            bool(self.host_id.strip()),
        ):
            raise ValueError("Invalid instruction layer scope or keys")
        if self.agent_key != self.agent_key.strip() or self.host_id != self.host_id.strip():
            raise ValueError("Instruction layer keys must not contain surrounding whitespace")
        if len(self.agent_key) > 64 or len(self.host_id) > 64:
            raise ValueError("Instruction layer keys must be at most 64 characters")


@dataclass(frozen=True)
class GlobalInstructionRevision:
    """One saved revision of the global instructions text.

    :param id: Opaque revision identifier, e.g. ``"a1b2c3..."``.
    :param text: The instruction text; empty means "off".
    :param created_at: Unix epoch seconds at save time.
    :param created_by: User ID of the saving admin, or ``None`` in
        single-user mode.
    """

    id: str
    text: str
    created_at: int
    created_by: str | None
    scope: str = "shared"
    agent_key: str = ""
    host_id: str = ""
    created_us: int = 0


class GlobalInstructionsStore(ABC):
    """Abstract base for global instructions persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the global instructions store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def current(
        self, *, scope: str = "shared", agent_key: str = "", host_id: str = ""
    ) -> GlobalInstructionRevision | None:
        """
        Return the newest revision, or ``None`` when nothing was saved.

        :returns: The live revision, or ``None``.
        """
        ...

    @abstractmethod
    def save(
        self,
        text: str,
        *,
        created_by: str | None,
        scope: str = "shared",
        agent_key: str = "",
        host_id: str = "",
        expected_revision_id: str | None = None,
        check_revision: bool = False,
    ) -> GlobalInstructionRevision:
        """
        Append a revision and return it.

        :param text: The full replacement text; empty is a valid save.
        :param created_by: User ID of the saving admin, or ``None`` in
            single-user mode.
        :param expected_revision_id: Observed revision; ``None`` means unsaved.
        :param check_revision: Refuse a stale observed revision atomically.
        :returns: The newly created revision.
        """
        ...

    @abstractmethod
    def list_revisions(
        self,
        *,
        limit: int = 50,
        scope: str = "shared",
        agent_key: str = "",
        host_id: str = "",
    ) -> list[GlobalInstructionRevision]:
        """
        List revisions newest first.

        :param limit: Maximum revisions to return.
        :returns: Revisions ordered by ``created_at`` then ``id``,
            descending.
        """
        ...

    @abstractmethod
    def list_layers(self) -> list[GlobalInstructionRevision]:
        """Return the latest revision of every layer, including disabled layers."""
        ...

    @abstractmethod
    def latest_layers(
        self, host_id: str | None, agent_key: str | None
    ) -> list[GlobalInstructionRevision]:
        """Read the independent setting, or shared → agent → host for a route."""
        ...
