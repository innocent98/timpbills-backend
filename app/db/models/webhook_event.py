"""Webhook events — raw body + dedupe on provider event id."""
import uuid

from sqlalchemy import Boolean, Column, JSON, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class WebhookEvent(Base, TimestampMixin):
    __tablename__ = "webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String, nullable=False)
    # Paystack's `data.id` or `data.reference` — unique per event.
    provider_event_id = Column(String, nullable=False, unique=True, index=True)
    event_type = Column(String, nullable=False)
    raw = Column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
    processed = Column(Boolean, nullable=False, default=False)
