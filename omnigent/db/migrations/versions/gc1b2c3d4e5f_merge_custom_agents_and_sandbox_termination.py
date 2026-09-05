"""Merge custom Agent and managed sandbox migration lineages.

Revision ID: gc1b2c3d4e5f
Revises: fd1b2c3d4e5, gb1b2c3d4e5f
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "gc1b2c3d4e5f"
down_revision: tuple[str, str] = ("fd1b2c3d4e5", "gb1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two already-applied schema branches."""


def downgrade() -> None:
    """Split back to the two parent revisions."""
