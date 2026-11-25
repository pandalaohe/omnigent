"""Join the workflow and journal schema with the fork's usage schema."""

revision: str = "a74c20261126"
down_revision: tuple[str, str] = ("a73c20261126", "f7c2d8a4b901")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Restore both revision markers without changing stored data."""
