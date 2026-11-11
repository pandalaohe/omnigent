"""Join custom metadata with upstream sidebar live state."""

from collections.abc import Sequence

revision: str = "a60c20261110"
down_revision: tuple[str, str] = ("a59c20261110", "c7cd88f24c1c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
