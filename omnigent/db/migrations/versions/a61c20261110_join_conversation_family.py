"""Join custom metadata with upstream conversation family membership."""

from collections.abc import Sequence

revision: str = "a61c20261110"
down_revision: tuple[str, str] = ("a60c20261110", "f1c7a9e2b406")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
