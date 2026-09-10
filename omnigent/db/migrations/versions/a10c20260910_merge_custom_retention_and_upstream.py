"""Join custom retention with the current upstream schema.

Revision ID: a10c20260910
Revises: ff1b2c3d4e5, ge1b2c3d4e5f
Create Date: 2026-09-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a10c20260910"
down_revision: tuple[str, str] = ("ff1b2c3d4e5", "ge1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
