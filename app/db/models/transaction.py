"""Transaction — immutable record of one money-moving attempt."""
import uuid
from decimal import Decimal

from sqlalchemy import Column, Enum, ForeignKey, Index, JSON, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.models._enums import TransactionStatus, TransactionType


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_tx_user_created", "user_id", "created_at"),
        Index("ix_tx_reference", "reference", unique=True),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reference = Column(String, nullable=False, unique=True)
    type   = Column(Enum(TransactionType, name="tx_type_enum"), nullable=False)
    status = Column(
        Enum(TransactionStatus, name="tx_status_enum"),
        nullable=False,
        default=TransactionStatus.pending,
    )
    amount    = Column(Numeric(14, 2), nullable=False)
    fee       = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    currency  = Column(String(3), nullable=False, default="NGN")

    # Arbitrary metadata. For wallet funding: {"paystack_reference": "..."}.
    # JSON().with_variant(JSONB(), 'postgresql') → JSONB on Postgres, TEXT on SQLite.
    meta = Column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False, default=dict)
