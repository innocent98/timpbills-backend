"""add wallet_credit_keys table (DVA credit idempotency)

Revision ID: 202607221300
Revises: 202607221200
Create Date: 2026-07-22 13:00:00

Backs WalletService.credit(idempotency_key=...) — the no-double-credit guard
for the DVA inbound-funding reconciliation sweep (backend must-fix #2). One
row per applied wallet credit; the unique `key` (funding tx reference) is
written in the same transaction as the balance change, so a repeat credit
with the same key collides and no-ops. Forward-only per PRD 1.7.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202607221300"
down_revision = "202607221200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_credit_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0.00",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_wallet_credit_keys_key", "wallet_credit_keys", ["key"], unique=True
    )
    op.create_index(
        "ix_wallet_credit_keys_user_id", "wallet_credit_keys", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_credit_keys_user_id", table_name="wallet_credit_keys")
    op.drop_index("ix_wallet_credit_keys_key", table_name="wallet_credit_keys")
    op.drop_table("wallet_credit_keys")
