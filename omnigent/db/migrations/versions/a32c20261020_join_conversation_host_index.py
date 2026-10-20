"""Join the local migration chain with the conversation host index."""

from collections.abc import Sequence

revision: str = "a32c20261020"
down_revision: str | Sequence[str] | None = ("a31c20261019", "nf12ab34cd56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
