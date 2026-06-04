"""add notification_logs table

Revision ID: 202606041000
Revises: 202606040900
Create Date: 2026-06-04 10:00:00

Audit trail for every notification channel send (push/email/sms incl. OTP).
Written by NotificationService + the OTP send path (Task 13); read by the
admin dashboard (Task 14).
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202606041000"
down_revision = "202606040900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    channel = postgresql.ENUM(
        "push", "email", "sms", name="notification_channel_enum", create_type=True
    )
    status = postgresql.ENUM(
        "pending", "sent", "failed", name="notification_log_status_enum", create_type=True
    )
    channel.create(op.get_bind(), checkfirst=True)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "notification_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("channel", channel, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notification_logs_user_id", "notification_logs", ["user_id"])
    op.create_index("ix_notification_logs_event", "notification_logs", ["event"])
    op.create_index("ix_notification_logs_channel", "notification_logs", ["channel"])
    op.create_index("ix_notification_logs_status", "notification_logs", ["status"])
    op.create_index("ix_notification_logs_created_at", "notification_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notification_logs_created_at", table_name="notification_logs")
    op.drop_index("ix_notification_logs_status", table_name="notification_logs")
    op.drop_index("ix_notification_logs_channel", table_name="notification_logs")
    op.drop_index("ix_notification_logs_event", table_name="notification_logs")
    op.drop_index("ix_notification_logs_user_id", table_name="notification_logs")
    op.drop_table("notification_logs")
    postgresql.ENUM(name="notification_log_status_enum").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="notification_channel_enum").drop(op.get_bind(), checkfirst=True)
