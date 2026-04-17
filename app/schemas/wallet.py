from decimal import Decimal

from pydantic import BaseModel, Field


class WalletResponse(BaseModel):
    balance: Decimal
    balance_cap: Decimal
    currency: str = "NGN"


class FundWalletRequest(BaseModel):
    amount: Decimal = Field(gt=Decimal("0"))


class FundWalletResponse(BaseModel):
    reference: str
    authorization_url: str
    amount: Decimal
    fee: Decimal
