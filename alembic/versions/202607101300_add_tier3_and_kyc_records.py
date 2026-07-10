"""add tier_3 kyc level + kyc_records table

Revision ID: 202607101300
Revises: 202606041000
Create Date: 2026-07-10 13:00:00

Adds the 4th KYC tier ("tier_3") to kyc_level_enum and creates kyc_records,
the audit trail the KYC verification service (A6) writes one row per
provider call to. No raw PII columns — masked_id is last-2-digits only.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202607101300"
down_revision = "202606041000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres cannot add an enum value inside the same transaction block
    # that might go on to use it, and older Postgres can't ALTER TYPE ...
    # ADD VALUE inside a transaction block at all. Commit the migration's
    # open transaction first, then run the ALTER TYPE outside of it.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("ALTER TYPE kyc_level_enum ADD VALUE IF NOT EXISTS 'tier_3'")

    op.create_table(
        "kyc_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verification_type", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("liveness_passed", sa.Boolean(), nullable=True),
        sa.Column("face_match", sa.Boolean(), nullable=True),
        sa.Column("face_match_confidence", sa.Integer(), nullable=True),
        sa.Column("tier_before", sa.Integer(), nullable=False),
        sa.Column("tier_after", sa.Integer(), nullable=True),
        sa.Column("masked_id", sa.String(length=8), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kyc_records_user_id", "kyc_records", ["user_id"])
    op.create_unique_constraint(
        "uq_kyc_records_provider_reference", "kyc_records", ["provider_reference"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_kyc_records_provider_reference", "kyc_records", type_="unique")
    op.drop_index("ix_kyc_records_user_id", table_name="kyc_records")
    op.drop_table("kyc_records")
    # Postgres cannot DROP a value from an enum type cleanly (would require
    # rebuilding the type and every column/index using it) — tier_3 stays
    # in kyc_level_enum on downgrade. Matches the precedent in
    # 202604180900_refund_type_and_payment_method.py for the same limitation.
