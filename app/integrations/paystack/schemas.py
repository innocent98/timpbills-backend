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


class CreateCustomerResponse(BaseModel):
    customer_code: str
    customer_id: str | None = None


class AssignDedicatedAccountResponse(BaseModel):
    status: bool
    message: str


class DedicatedAccountDetails(BaseModel):
    account_number: str | None = None
    account_name: str | None = None
    bank_name: str | None = None
    bank_slug: str | None = None
    dedicated_account_id: str | None = None
    status: str | None = None


class DvaProvider(BaseModel):
    provider_slug: str
    bank_name: str


class BankListItem(BaseModel):
    name: str
    slug: str
    code: str
