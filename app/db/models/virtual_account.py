"""Dedicated Virtual Account (Paystack DVA) — one row per user.

We persist only Paystack's durable identity token (customer_code) plus the
issued account details. The BVN and bank account supplied at setup are never
stored: they live only inside the provisioning request handler and the
outbound Paystack call. Resolution of inbound bank-transfer webhooks is by
`account_number` (unique); resolution of the identity/assign lifecycle
webhooks is by `paystack_customer_code`.
"""
import uuid

from sqlalchemy import Column, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.models._enums import VirtualAccountStatus


class VirtualAccount(Base, TimestampMixin):
    __tablename__ = "virtual_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    paystack_customer_code = Column(String, nullable=False)
    paystack_customer_id   = Column(String, nullable=True)
    dedicated_account_id   = Column(String, nullable=True)
    account_number = Column(String, nullable=True, unique=True, index=True)
    account_name   = Column(String, nullable=True)
    bank_name      = Column(String, nullable=True)
    bank_slug      = Column(String, nullable=True)
    currency = Column(String, nullable=False, default="NGN")
    status = Column(
        Enum(VirtualAccountStatus, name="virtual_account_status_enum"),
        nullable=False,
    )
    failure_reason = Column(String, nullable=True)
