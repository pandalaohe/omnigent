"""Join custom usage accounting with upstream automation and inbox migrations."""

from collections.abc import Sequence

revision: str = "a73c20261126"
down_revision: tuple[str, str] = ("a72c20261117", "b4e2d7a91c35")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
