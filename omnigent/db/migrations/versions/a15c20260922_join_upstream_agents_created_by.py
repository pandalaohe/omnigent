"""Join the custom lineage with upstream's agents.created_by schema.

Revision ID: a15c20260922
Revises: a14c20260921, ii1a2b3c4d5e
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a15c20260922"
down_revision: tuple[str, str] = ("a14c20260921", "ii1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Retain both lineages after their migrations have run."""


def downgrade() -> None:
    """Split the heads without changing either branch's data."""
