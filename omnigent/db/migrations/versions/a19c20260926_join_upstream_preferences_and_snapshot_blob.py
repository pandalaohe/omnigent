"""Join the custom lineage with upstream's preferences and snapshot-blob schema.

Revision ID: a19c20260926
Revises: a18c20260925, ll1a2b3c4d5e
Create Date: 2026-09-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a19c20260926"
down_revision: tuple[str, str] = ("a18c20260925", "ll1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
