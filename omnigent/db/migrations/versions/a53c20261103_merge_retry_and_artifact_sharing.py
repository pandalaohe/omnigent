"""Join child retry receipts and existing artifact sharing preferences."""

from collections.abc import Sequence

revision: str = "a53c20261103"
down_revision: tuple[str, str] = ("a51c20261029", "a52c20261030")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
