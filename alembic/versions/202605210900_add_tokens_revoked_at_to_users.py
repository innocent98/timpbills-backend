"""add tokens_revoked_at to users (Sprint 5c · Task 4.2)

Revision ID: 202605210900
Revises: 202605201200
Create Date: 2026-05-21 09:00:00

Backstop column for the "log me out everywhere" flow that ships with
the password-change endpoint. On a successful password change we stamp
``users.tokens_revoked_at = now()`` and the auth gate rejects any
token whose ``iat`` claim is older than that stamp.

This complements — does not replace — the per-jti blocklist in
``revoked:jwt:{jti}`` Redis keys. The blocklist kills one specific
token; ``tokens_revoked_at`` kills every outstanding token issued
before a given instant, including ones we never saw (e.g. an old
mobile session whose access token has expired but whose refresh hasn't).

Nullable on purpose — existing users have no revocation stamp and
every token issued before the column existed is implicitly accepted.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202605210900"
down_revision: str | None = "202605201200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tokens_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "tokens_revoked_at")
