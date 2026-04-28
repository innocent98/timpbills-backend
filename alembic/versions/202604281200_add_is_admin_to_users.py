"""add_is_admin_to_users

Revision ID: 202604281200
Revises: 202604211200
Create Date: 2026-04-28 12:00:00.000000

Sprint 5 BE-52 — admin manual-refund endpoint needs an authorization
signal. We add a single `is_admin` boolean rather than a separate
`admin_users` table because:

  * v1 admin surface is one endpoint — a join table would be over-eng
    for the current load.
  * Sprint 8 may grow this into a richer admin model; if so, migrating
    a boolean to a join table is a routine schema change. The reverse
    (collapsing a join table back into a boolean) is harder.

Server-default `false` so existing rows backfill safely without an
explicit UPDATE in the upgrade.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '202604281200'
down_revision = '202604211200'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'is_admin',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'is_admin')
