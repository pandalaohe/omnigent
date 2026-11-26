"""Join the upstream memory table with the fork schema."""

revision: str = "a75c20261126"
down_revision: tuple[str, str] = ("a74c20261126", "ffeec66eadb5")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Keep both lineages after their migrations have run."""


def downgrade() -> None:
    """Restore both revision markers without changing stored data."""
