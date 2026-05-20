"""Audit trail — one row per state transition on a Transaction."""
import uuid

from sqlalchemy import JSON, Column, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.models._enums import TransactionStatus


class TransactionEvent(Base, TimestampMixin):
    __tablename__ = "transaction_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status = Column(Enum(TransactionStatus, name="tx_status_enum"), nullable=True)
    to_status   = Column(Enum(TransactionStatus, name="tx_status_enum"), nullable=False)
    reason      = Column(String, nullable=True)
    context     = Column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False, default=dict)
