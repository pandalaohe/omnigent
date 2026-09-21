"""Join the custom lineage with upstream's inference-snapshot schema.

Revision ID: a14c20260921
Revises: a13c20260920, hi1b2c3d4e5f
Create Date: 2026-09-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a14c20260921"
down_revision: tuple[str, str] = ("a13c20260920", "hi1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
