"""Payment — maps our Transaction to the provider's (Paystack) reference."""
import enum
import uuid

from sqlalchemy import Column, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    success = "success"
    failed  = "failed"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    provider = Column(String, nullable=False, default="paystack")
    # Paystack reference. Unique per provider.
    provider_reference = Column(String, nullable=False, unique=True, index=True)
    status = Column(
        Enum(PaymentStatus, name="payment_status_enum"),
        nullable=False,
        default=PaymentStatus.pending,
    )

    # Populated from Paystack verify.authorization on success.
    method    = Column(String, nullable=True)      # 'card' | 'bank_transfer' | 'ussd' | ...
    last4     = Column(String(4), nullable=True)   # only for card method
    bank_name = Column(String, nullable=True)      # populated for bank_transfer / ussd
