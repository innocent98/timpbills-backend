from pydantic import BaseModel, Field


class ProvisionVirtualAccountRequest(BaseModel):
    bvn: str = Field(pattern=r"^\d{11}$")
    account_number: str = Field(pattern=r"^\d{10}$")
    bank_code: str = Field(min_length=3, max_length=6)
    preferred_bank: str | None = None


class VirtualAccountResponse(BaseModel):
    status: str
    account_number: str | None = None
    account_name: str | None = None
    bank_name: str | None = None
    failure_reason: str | None = None


class BankListItemResponse(BaseModel):
    name: str
    slug: str
    code: str
