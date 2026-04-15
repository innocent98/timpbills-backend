"""add email_verified to users and new otp purposes

Revision ID: 202604151100
Revises: 202604141600
Create Date: 2026-04-15 11:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202604151100"
down_revision: str | None = "202604141600"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add email_verified column to users
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Add email column to otp_codes (nullable — phone OTPs don't have email)
    op.add_column(
        "otp_codes",
        sa.Column("email", sa.String(), nullable=True),
    )
    op.create_index("ix_otp_codes_email", "otp_codes", ["email"])

    # Make phone nullable in otp_codes (email OTPs don't have phone)
    op.alter_column("otp_codes", "phone", nullable=True)

    # Add new enum values for otp_purpose_enum
    # Postgres requires ALTER TYPE outside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE otp_purpose_enum ADD VALUE IF NOT EXISTS 'email_verification'"
        )
        op.execute(
            "ALTER TYPE otp_purpose_enum ADD VALUE IF NOT EXISTS 'phone_verification'"
        )


def downgrade() -> None:
    op.drop_index("ix_otp_codes_email", table_name="otp_codes")
    op.drop_column("otp_codes", "email")
    op.alter_column("otp_codes", "phone", nullable=False)
    op.drop_column("users", "email_verified")
    # Note: removing enum values in Postgres requires recreating the type — skipped.
