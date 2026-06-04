import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class NotificationChannel(str, enum.Enum):
    push = "push"
    email = "email"
    sms = "sms"


class NotificationLogStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class NotificationLog(TimestampMixin, Base):
    __tablename__ = "notification_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event = Column(String, nullable=False, index=True)
    channel = Column(
        Enum(NotificationChannel, name="notification_channel_enum"),
        nullable=False,
        index=True,
    )
    status = Column(
        Enum(NotificationLogStatus, name="notification_log_status_enum"),
        nullable=False,
        index=True,
    )
    provider = Column(String, nullable=False)
    provider_reference = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    # created_at carries an operational index (ix_notification_logs_created_at)
    # declared in the migration for admin dashboard time-range queries. It lives
    # on the shared TimestampMixin column, so the index can't be expressed here
    # without affecting every model — the migration is the authoritative DDL.
