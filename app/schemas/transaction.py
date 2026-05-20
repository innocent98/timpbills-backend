from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class TransactionView(BaseModel):
    reference: str
    type: str
    status: str
    amount: Decimal
    fee: Decimal
    currency: str
    created_at: datetime
    meta: dict = {}


class TransactionListResponse(BaseModel):
    items: list[TransactionView]
    total: int


class TransactionEventView(BaseModel):
    at: datetime
    from_status: str | None = None
    to_status: str
    reason: str | None = None
    context: dict = {}


class TransactionEventsResponse(BaseModel):
    items: list[TransactionEventView]
