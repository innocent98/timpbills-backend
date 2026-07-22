"""In-memory fake Paystack for tests — deterministic reference handling."""
from dataclasses import dataclass, field
from decimal import Decimal

from app.integrations.paystack.schemas import (
    AssignDedicatedAccountResponse,
    BankListItem,
    CreateCustomerResponse,
    DedicatedAccountDetails,
    DvaProvider,
    InitResponse,
    PaystackAuthorization,
    VerifyResponse,
)


@dataclass
class FakePaystackClient:
    # Map our reference → "success" | "failed" | "abandoned"
    _outcomes: dict[str, str] = field(default_factory=dict)
    initialized: list[tuple[str, int]] = field(default_factory=list)
    customers: list[str] = field(default_factory=list)
    assigned: list[tuple[str, str, str, str]] = field(default_factory=list)

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
            authorization=PaystackAuthorization(
                channel="card",
                last4="4081",
                bank=None,
            ),
        )

    def verify_signature(self, *, raw_body: bytes, signature: str) -> bool:
        # Fake always accepts the literal "FAKE_SIG" signature in tests.
        return signature == "FAKE_SIG"

    # ── Dedicated Virtual Accounts ───────────────────────────────────────
    async def create_customer(
        self, *, email: str, first_name: str, last_name: str, phone: str
    ) -> CreateCustomerResponse:
        # Deterministic by email so a repeat provision maps to one customer
        # (mirrors Paystack's idempotent POST /customer by email).
        self.customers.append(email)
        digest = f"{abs(hash(email)) % 10**10:010d}"
        return CreateCustomerResponse(
            customer_code=f"CUS_fake_{digest}", customer_id=digest
        )

    async def assign_dedicated_account(
        self, *, email: str, first_name: str, middle_name: str, last_name: str,
        phone: str, preferred_bank: str, country: str, account_number: str,
        bvn: str, bank_code: str,
    ) -> AssignDedicatedAccountResponse:
        self.assigned.append((email, account_number, bvn, bank_code))
        return AssignDedicatedAccountResponse(
            status=True, message="Assign dedicated account in progress"
        )

    async def fetch_dedicated_account(
        self, *, account_id: str
    ) -> DedicatedAccountDetails:
        return DedicatedAccountDetails(
            account_number="9988776655", account_name="ADA OBI",
            bank_name="Test Bank", bank_slug="test-bank",
            dedicated_account_id=account_id, status="active",
        )

    async def requery_dedicated_account(
        self, *, account_number: str, provider_slug: str
    ) -> AssignDedicatedAccountResponse:
        return AssignDedicatedAccountResponse(status=True, message="requery queued")

    async def list_dva_providers(self) -> list[DvaProvider]:
        return [DvaProvider(provider_slug="test-bank", bank_name="Test Bank")]

    async def list_banks(self, *, country: str = "nigeria") -> list[BankListItem]:
        return [
            BankListItem(name="Wema Bank", slug="wema-bank", code="035"),
            BankListItem(name="Test Bank", slug="test-bank", code="000"),
            BankListItem(name="Kuda MFB", slug="kuda-bank", code="50211"),
        ]


def customer_identification_event(
    *, customer_code: str, success: bool = True, reason: str | None = None,
    event_id: str = "evt_ci",
) -> dict:
    event = "customeridentification.success" if success else "customeridentification.failed"
    data = {"id": event_id, "customer_code": customer_code, "email": "ada@x.co"}
    if not success:
        data["reason"] = reason or "Account resolution failed"
    return {"event": event, "data": data}


def dedicated_account_assign_event(
    *, customer_code: str, success: bool = True, account_number: str | None = None,
    account_name: str | None = None, bank_name: str | None = None,
    bank_slug: str | None = None, reason: str | None = None, event_id: str = "evt_da",
) -> dict:
    event = "dedicatedaccount.assign.success" if success else "dedicatedaccount.assign.failed"
    data: dict = {"id": event_id, "customer": {"customer_code": customer_code}}
    if success:
        data["dedicated_account"] = {
            "id": "dva_1",
            "account_number": account_number,
            "account_name": account_name,
            "bank": {"name": bank_name, "slug": bank_slug},
        }
    else:
        data["reason"] = reason or "Could not assign account"
    return {"event": event, "data": data}


def dva_charge_event(
    *, account_number: str, amount_kobo: int, sender_name: str = "JOHN DOE",
    sender_bank: str = "Kuda MFB", sender_account: str = "1234567890",
    fees: int = 1500, reference: str = "dva-ref", event_id: str = "evt_dva",
) -> dict:
    return {
        "event": "charge.success",
        "data": {
            "id": event_id,
            "reference": reference,
            "amount": amount_kobo,
            "channel": "dedicated_nuban",
            "fees": fees,
            "authorization": {
                "channel": "dedicated_nuban",
                "receiver_bank_account_number": account_number,
                "sender_name": sender_name,
                "sender_bank": sender_bank,
                "sender_bank_account_number": sender_account,
            },
        },
    }
