"""add virtual_accounts table + wallet spend-lock columns

Revision ID: 202607221200
Revises: 202607101300
Create Date: 2026-07-22 12:00:00

Creates virtual_accounts (one DVA per user, unique account_number for inbound
webhook resolution) and adds spend_locked / spend_locked_reason to wallets for
the over-cap transfer lock. Forward-only per PRD 1.7.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202607221200"
down_revision = "202607101300"
branch_labels = None
depends_on = None

_VA_STATUS_VALUES = (
    "pending_identity", "pending_assign", "active", "failed", "deactivated",
)
_SPEND_REASON_VALUES = ("over_cap",)

# Two instantiations per enum: a `create_type=True` copy used only for the
# explicit CREATE TYPE below, and a `create_type=False` copy used as the
# column type so `op.create_table`/`op.add_column` don't also try to emit
# CREATE TYPE (which would collide — see 202604171200 for the same pattern).
# Must use `postgresql.ENUM` (not the generic `sa.Enum`) — `create_type` is a
# postgresql-dialect-only kwarg that a generic `sa.Enum` silently drops.
_VA_STATUS_CREATE = postgresql.ENUM(
    *_VA_STATUS_VALUES, name="virtual_account_status_enum", create_type=True
)
_VA_STATUS_COL = postgresql.ENUM(
    *_VA_STATUS_VALUES, name="virtual_account_status_enum", create_type=False
)
_SPEND_REASON_CREATE = postgresql.ENUM(
    *_SPEND_REASON_VALUES, name="spend_lock_reason_enum", create_type=True
)
_SPEND_REASON_COL = postgresql.ENUM(
    *_SPEND_REASON_VALUES, name="spend_lock_reason_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    _VA_STATUS_CREATE.create(bind, checkfirst=True)
    _SPEND_REASON_CREATE.create(bind, checkfirst=True)

    op.create_table(
        "virtual_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("paystack_customer_code", sa.String(), nullable=False),
        sa.Column("paystack_customer_id", sa.String(), nullable=True),
        sa.Column("dedicated_account_id", sa.String(), nullable=True),
        sa.Column("account_number", sa.String(), nullable=True),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column("bank_name", sa.String(), nullable=True),
        sa.Column("bank_slug", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False, server_default="NGN"),
        sa.Column(
            "status",
            _VA_STATUS_COL,
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_virtual_accounts_user_id", "virtual_accounts", ["user_id"]
    )
    op.create_index(
        "ix_virtual_accounts_user_id", "virtual_accounts", ["user_id"]
    )
    op.create_index(
        "ix_virtual_accounts_account_number",
        "virtual_accounts",
        ["account_number"],
        unique=True,
    )

    op.add_column(
        "wallets",
        sa.Column(
            "spend_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "wallets",
        sa.Column("spend_locked_reason", _SPEND_REASON_COL, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wallets", "spend_locked_reason")
    op.drop_column("wallets", "spend_locked")
    op.drop_index("ix_virtual_accounts_account_number", table_name="virtual_accounts")
    op.drop_index("ix_virtual_accounts_user_id", table_name="virtual_accounts")
    op.drop_constraint(
        "uq_virtual_accounts_user_id", "virtual_accounts", type_="unique"
    )
    op.drop_table("virtual_accounts")

    bind = op.get_bind()
    _SPEND_REASON_CREATE.drop(bind, checkfirst=True)
    _VA_STATUS_CREATE.drop(bind, checkfirst=True)
