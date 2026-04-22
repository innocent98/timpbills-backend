"""Push tokens — one row per (user, device) FCM registration.

Used by NotificationService to dispatch real FCM pushes. A single FCM token
identifies one physical device install; when a user logs out and another
logs in on the same device, the token is reassigned to the new user. The
row is removed outright when FCM reports the token as dead (unregistered,
invalid-registration) via ``delete_by_fcm_token``.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class PushToken(Base, TimestampMixin):
    __tablename__ = "push_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fcm_token = Column(String, nullable=False, unique=True, index=True)
    platform = Column(String, nullable=False)  # "ios" | "android"
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
