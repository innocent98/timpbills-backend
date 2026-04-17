from typing import Protocol

from app.integrations.paystack.schemas import InitResponse, VerifyResponse


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
