"""add users.anonymized_at (account-deletion PII purge marker)

Revision ID: 202607251200
Revises: 202607221300
Create Date: 2026-07-25 12:00:00
"""
import sqlalchemy as sa

from alembic import op

revision = "202607251200"
down_revision = "202607221300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "anonymized_at")
