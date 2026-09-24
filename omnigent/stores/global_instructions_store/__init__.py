"""Global instructions store — the server-wide instruction text.

One append-only revision per save; the newest revision in a workspace is
the live value. Session initialization reads it and appends it to every
Omnigent-started session's instructions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

# Hard ceiling for one revision's text. The route rejects longer saves
# outright (never truncates); the store itself stays cap-agnostic.
GLOBAL_INSTRUCTIONS_MAX_CHARS: Final[int] = 8000


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
    def current(self) -> GlobalInstructionRevision | None:
        """
        Return the newest revision, or ``None`` when nothing was saved.

        :returns: The live revision, or ``None``.
        """
        ...

    @abstractmethod
    def save(self, text: str, *, created_by: str | None) -> GlobalInstructionRevision:
        """
        Append a revision and return it.

        :param text: The full replacement text; empty is a valid save.
        :param created_by: User ID of the saving admin, or ``None`` in
            single-user mode.
        :returns: The newly created revision.
        """
        ...

    @abstractmethod
    def list_revisions(self, *, limit: int = 50) -> list[GlobalInstructionRevision]:
        """
        List revisions newest first.

        :param limit: Maximum revisions to return.
        :returns: Revisions ordered by ``created_at`` then ``id``,
            descending.
        """
        ...
