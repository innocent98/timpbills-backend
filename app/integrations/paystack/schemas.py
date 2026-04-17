from decimal import Decimal

from pydantic import BaseModel


class InitResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str


class VerifyResponse(BaseModel):
    reference: str
    status: str  # "success" | "failed" | "abandoned"
    amount: Decimal  # Naira
    paid_at: str | None = None
