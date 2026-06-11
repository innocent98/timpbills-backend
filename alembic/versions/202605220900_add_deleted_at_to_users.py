"""add deleted_at to users (Sprint 5c · Task 6.1)

Revision ID: 202605220900
Revises: 202605210900
Create Date: 2026-05-22 09:00:00

Soft-delete tombstone for /users/me DELETE. Set alongside
``is_active = false`` + ``tokens_revoked_at = now()`` when a user
self-deletes via DELETE /api/v1/users/me. The 30-day re-register
block in /auth/register reads this column to decide whether a phone
or email that already lives on a soft-deleted row may be re-used.

Why a dedicated column instead of reusing ``tokens_revoked_at``:
``tokens_revoked_at`` is also stamped by password-change and
phone-change flows. We must not block re-registration just because
those benign flows ran — only because the user actually deleted their
account. ``deleted_at`` is null in every benign case and non-null
only after a self-delete.

Hard delete (PII purge) is a Sprint 8 / compliance concern — this
migration only adds the soft-delete tombstone.

Nullable on purpose — existing users have no delete stamp.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202605220900"
down_revision: str | None = "202605210900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
