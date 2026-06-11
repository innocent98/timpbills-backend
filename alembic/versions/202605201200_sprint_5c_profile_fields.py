"""sprint 5c profile fields and notification preferences

Revision ID: 202605201200
Revises: 202605191100
Create Date: 2026-05-21 08:43:08.285797

Sprint 5c task 1.1. Adds the four profile-extension columns to ``users``
(``date_of_birth``, ``gender``, ``address``, ``avatar_url``) and creates the
new ``notification_preferences`` table — a 1:1 child of ``users`` keyed by a
unique ``user_id`` FK with ``ON DELETE CASCADE``.

Defaults reflect spec §3.2: transaction alerts, referral updates, and email
notifications opt-in by default; marketing promotions opt-out by default.
``id`` uses ``gen_random_uuid()`` from ``pgcrypto`` — the existing migrations
rely on the same extension being installed (see ``202605191000``).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202605201200"
down_revision: str | None = "202605191100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. New profile columns on users (all nullable — optional fields) ──
    op.add_column("users", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("gender", sa.String(length=20), nullable=True))
    op.add_column("users", sa.Column("address", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=512), nullable=True))

    # ── 2. notification_preferences table ─────────────────────────────────
    op.create_table(
        "notification_preferences",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "transaction_alerts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "referral_updates",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "promotions",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "email_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_notification_preferences_user_id",
        "notification_preferences",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_preferences_user_id",
        table_name="notification_preferences",
    )
    op.drop_table("notification_preferences")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "address")
    op.drop_column("users", "gender")
    op.drop_column("users", "date_of_birth")
