"""refund type and payment method

Revision ID: 202604180900
Revises: 202604171200
Create Date: 2026-04-18 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '202604180900'
down_revision = '202604171200'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add 'refund' to transaction_type enum (Postgres only; SQLite tests ignore).
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TYPE tx_type_enum ADD VALUE IF NOT EXISTS 'refund'")

    # Add payment method columns
    op.add_column('payments', sa.Column('method', sa.String(), nullable=True))
    op.add_column('payments', sa.Column('last4', sa.String(length=4), nullable=True))
    op.add_column('payments', sa.Column('bank_name', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('payments', 'bank_name')
    op.drop_column('payments', 'last4')
    op.drop_column('payments', 'method')
    # Postgres cannot DROP an enum value cleanly; leaving 'refund' in the type is harmless.
