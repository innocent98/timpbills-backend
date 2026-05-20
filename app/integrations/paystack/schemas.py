from decimal import Decimal

from pydantic import BaseModel


class InitResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str


class PaystackAuthorization(BaseModel):
    channel: str | None = None       # 'card' | 'bank_transfer' | 'ussd' | ...
    last4: str | None = None
    bank: str | None = None


class VerifyResponse(BaseModel):
    reference: str
    status: str  # "success" | "failed" | "abandoned"
    amount: Decimal  # Naira
    paid_at: str | None = None
    authorization: PaystackAuthorization | None = None
