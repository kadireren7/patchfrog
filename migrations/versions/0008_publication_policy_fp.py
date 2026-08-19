"""Fold publication_policy_fingerprint into review_publications canonical identity

Revision ID: 0008_publication_policy_fp
Revises: 0007_review_publishing
Create Date: 2026-08-20

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_publication_policy_fp"
down_revision: str | None = "0007_review_publishing"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_publications",
        sa.Column("publication_policy_fingerprint", sa.String(length=64), nullable=False),
    )

    op.drop_index("uq_review_publications_published_identity", table_name="review_publications")
    op.create_index(
        "uq_review_publications_published_identity", "review_publications",
        ["review_run_id", "mode", "publication_policy_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_publications_published_identity", table_name="review_publications")
    op.create_index(
        "uq_review_publications_published_identity", "review_publications",
        ["review_run_id", "mode"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )

    op.drop_column("review_publications", "publication_policy_fingerprint")
