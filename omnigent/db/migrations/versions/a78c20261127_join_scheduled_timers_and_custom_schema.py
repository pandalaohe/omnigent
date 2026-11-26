"""Join scheduled timers and saved workflow scheduling with the fork schema."""

revision: str = "a78c20261127"
down_revision: tuple[str, str] = ("a77c20261126", "47d3e8fc3e25")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Keep both lineages after their migrations have run."""


def downgrade() -> None:
    """Restore both revision markers without changing stored data."""
