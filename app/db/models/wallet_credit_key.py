"""Wallet credit idempotency keys — the no-double-credit arbiter.

One row per successfully-applied wallet credit that opts into idempotency
(currently the DVA inbound-funding path). The unique ``key`` (the funding
transaction's reference) is inserted in the SAME database transaction as the
balance mutation, so a repeat ``WalletService.credit(idempotency_key=...)``
with the same key hits the unique constraint and becomes a safe no-op.

This is what lets the DVA reconciliation sweep re-run a stuck ``pending``
transaction without risking a second credit: sub-case (A) — never credited —
inserts the marker and credits once; sub-case (B) — already credited by the
live webhook before it crashed — collides on the marker and skips the credit.
"""
import uuid
from decimal import Decimal

from sqlalchemy import Column, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class WalletCreditKey(Base, TimestampMixin):
    __tablename__ = "wallet_credit_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The funding tx reference. Unique — this is the whole point of the table.
    key = Column(String, nullable=False, unique=True, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
