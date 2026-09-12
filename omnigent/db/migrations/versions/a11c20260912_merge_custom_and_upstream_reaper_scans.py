"""Join the custom lineage with upstream's host-reaper scan schema.

Revision ID: a11c20260912
Revises: a10c20260910, gg1b2c3d4e5f
Create Date: 2026-09-12 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a11c20260912"
down_revision: tuple[str, str] = ("a10c20260910", "gg1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
