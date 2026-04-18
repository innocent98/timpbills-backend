from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class InitResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str


class PaystackAuthorization(BaseModel):
    channel: Optional[str] = None       # 'card' | 'bank_transfer' | 'ussd' | ...
    last4: Optional[str] = None
    bank: Optional[str] = None


class VerifyResponse(BaseModel):
    reference: str
    status: str  # "success" | "failed" | "abandoned"
    amount: Decimal  # Naira
    paid_at: str | None = None
    authorization: Optional[PaystackAuthorization] = None
