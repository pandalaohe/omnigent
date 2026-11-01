"""Join the custom runtime and automation migration branches."""

revision: str = "f3c914a721ab"
down_revision: tuple[str, str] = ("a50c20261027", "d0a1f879a01d")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Both predecessor branches already own their schema changes."""


def downgrade() -> None:
    """Restore the two predecessor revision stamps."""
