"""Idempotency keys — dedupes money-endpoint POSTs per user."""
import uuid

from sqlalchemy import JSON, Column, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class IdempotencyKey(Base, TimestampMixin):
    __tablename__ = "idempotency_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    key = Column(String, nullable=False, unique=True, index=True)
    request_hash     = Column(String, nullable=False)
    response_status  = Column(Integer, nullable=False)
    response_body    = Column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
