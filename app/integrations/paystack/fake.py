"""In-memory fake Paystack for tests — deterministic reference handling."""
from dataclasses import dataclass, field
from decimal import Decimal

from app.integrations.paystack.schemas import InitResponse, VerifyResponse


@dataclass
class FakePaystackClient:
    # Map our reference → "success" | "failed" | "abandoned"
    _outcomes: dict[str, str] = field(default_factory=dict)
    initialized: list[tuple[str, int]] = field(default_factory=list)

    # Test hooks
    def will_succeed(self, reference: str) -> None:
        self._outcomes[reference] = "success"

    def will_fail(self, reference: str) -> None:
        self._outcomes[reference] = "failed"

    # Protocol
    async def initialize(
        self, *, amount_kobo: int, email: str, reference: str,
        callback_url: str | None = None, metadata: dict | None = None,
    ) -> InitResponse:
        self.initialized.append((reference, amount_kobo))
        return InitResponse(
            authorization_url=f"https://checkout.paystack.com/fake/{reference}",
            access_code=f"access_{reference}",
            reference=reference,
        )

    async def verify(self, *, reference: str) -> VerifyResponse:
        outcome = self._outcomes.get(reference, "abandoned")
        # Unknown amount in fake — retrieve from initialized
        amount_kobo = next(
            (k for r, k in self.initialized if r == reference), 0
        )
        return VerifyResponse(
            reference=reference,
            status=outcome,
            amount=Decimal(amount_kobo) / Decimal(100),
            paid_at="2026-04-17T12:00:00Z",
        )

    def verify_signature(self, *, raw_body: bytes, signature: str) -> bool:
        # Fake always accepts the literal "FAKE_SIG" signature in tests.
        return signature == "FAKE_SIG"
