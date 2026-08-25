"""Wallet — one row per user, single source of balance truth.

Balance stored as NUMERIC(14,2) naira (not kobo). CHECK constraints enforce
non-negative balance + cap at the DB level. All mutations go through
WalletService which uses SELECT … FOR UPDATE for row-level locking.
"""
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Column, Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.models._enums import SpendLockReason


class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="wallet_balance_non_negative"),
        CheckConstraint("balance_cap >= 0", name="wallet_cap_non_negative"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )

    balance     = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    balance_cap = Column(Numeric(14, 2), nullable=False, default=Decimal("50000.00"))

    # Over-cap lock (DVA transfer path). When landed money would push the
    # wallet past the KYC cap we credit in full and set this, gating all
    # outbound spend until the next KYC tier upgrade clears it.
    spend_locked = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    spend_locked_reason = Column(
        Enum(SpendLockReason, name="spend_lock_reason_enum"), nullable=True
    )

    user = relationship("User", backref="wallet", uselist=False)
