"""add admin_users table; drop users.is_admin

Revision ID: 202606040900
Revises: 202605260900
Create Date: 2026-06-04 09:00:00

Admin auth moves off the single-bit users.is_admin flag onto a dedicated
admin_users table (own password hash, role column ready for v2 RBAC).
require_admin is rewritten to an opaque-session cookie path, so nothing
reads users.is_admin after this migration — drop it.

Runbook: immediately after upgrade, create the first admin via
`python scripts/create_admin.py`.

Downgrade: re-adds users.is_admin (default false) and drops admin_users.
Any admin rows are lost on downgrade — re-seed via the CLI.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202606040900"
down_revision = "202605260900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the ENUM explicitly (checkfirst=True makes it idempotent), then
    # reference it in the table column with create_type=False so create_table
    # does NOT re-emit CREATE TYPE (which would raise DuplicateObject).
    postgresql.ENUM(
        "superadmin", "support", name="admin_role_enum", create_type=True
    ).create(op.get_bind(), checkfirst=True)
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "superadmin", "support",
                name="admin_role_enum", create_type=False,
            ),
            nullable=False, server_default="superadmin",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)
    op.drop_column("users", "is_admin")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
    postgresql.ENUM(name="admin_role_enum").drop(op.get_bind(), checkfirst=True)
