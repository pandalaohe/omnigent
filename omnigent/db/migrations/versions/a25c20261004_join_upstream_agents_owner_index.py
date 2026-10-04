"""Join the custom lineage with upstream's agents owner index.

Revision ID: a25c20261004
Revises: a24c20260930, mm1a2b3c4d5e
Create Date: 2026-10-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a25c20261004"
down_revision: tuple[str, str] = ("a24c20260930", "mm1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
