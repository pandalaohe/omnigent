"""Join custom metadata with upstream scheduled source sessions."""

from collections.abc import Sequence

revision: str = "a62c20261110"
down_revision: tuple[str, str] = ("a61c20261110", "1ad1db53f99d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
