"""Join custom metadata with upstream comment anchors."""

from collections.abc import Sequence

revision: str = "a63c20261111"
down_revision: tuple[str, str] = ("a62c20261110", "f9b3e6c2a401")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
