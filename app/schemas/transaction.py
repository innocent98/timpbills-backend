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
