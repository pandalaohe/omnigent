"""Join the upstream workflow shares table with the fork schema."""

revision: str = "a76c20261126"
down_revision: tuple[str, str] = ("a75c20261126", "d3bda6dbade3")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Keep both lineages after their migrations have run."""


def downgrade() -> None:
    """Restore both revision markers without changing stored data."""
