from typing import Protocol

from app.integrations.paystack.schemas import (
    AssignDedicatedAccountResponse,
    BankListItem,
    CreateCustomerResponse,
    DedicatedAccountDetails,
    DvaProvider,
    InitResponse,
    VerifyResponse,
)


class PaymentProvider(Protocol):
    async def initialize(
        self,
        *,
        amount_kobo: int,
        email: str,
        reference: str,
        callback_url: str | None = None,
        metadata: dict | None = None,
    ) -> InitResponse: ...

    async def verify(self, *, reference: str) -> VerifyResponse: ...

    def verify_signature(self, *, raw_body: bytes, signature: str) -> bool: ...

    # ── Dedicated Virtual Accounts ───────────────────────────────────────
    async def create_customer(
        self, *, email: str, first_name: str, last_name: str, phone: str
    ) -> CreateCustomerResponse: ...

    async def assign_dedicated_account(
        self,
        *,
        email: str,
        first_name: str,
        middle_name: str,
        last_name: str,
        phone: str,
        preferred_bank: str,
        country: str,
        account_number: str,
        bvn: str,
        bank_code: str,
    ) -> AssignDedicatedAccountResponse: ...

    async def fetch_dedicated_account(
        self, *, account_id: str
    ) -> DedicatedAccountDetails: ...

    async def requery_dedicated_account(
        self, *, account_number: str, provider_slug: str
    ) -> AssignDedicatedAccountResponse: ...

    async def list_dva_providers(self) -> list[DvaProvider]: ...

    async def list_banks(self, *, country: str = "nigeria") -> list[BankListItem]: ...
