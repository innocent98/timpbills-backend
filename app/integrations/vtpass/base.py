"""BillProvider Protocol — the single interface BillService and the
reconcile worker depend on. Every method returns typed schemas from
`schemas.py`; provider-specific error shapes are translated to
`ProviderTemporaryFailure` / `ProviderPermanentFailure` at the client
boundary."""
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.integrations.vtpass.schemas import (
    BillPurchaseResponse,
    DataPlanList,
)


@runtime_checkable
class BillProvider(Protocol):
    """Abstract bill-provider surface. Sprint 3 covers airtime + data;
    Sprint 4 extends the same Protocol with `validate_meter`,
    `purchase_electricity`, `purchase_cable` on the existing VTPass
    implementation.

    All methods are async. `request_id` is our internal transaction
    reference (`TMP-YYMMDD-…`) — VTPass uses it as its idempotency key,
    and our reconcile worker passes it to `requery` to check status.
    """

    # ── Airtime ─────────────────────────────────────────────────────────

    async def purchase_airtime(
        self,
        *,
        request_id: str,
        service_id: str,
        phone: str,
        amount_ngn: Decimal,
    ) -> BillPurchaseResponse: ...

    # ── Data ────────────────────────────────────────────────────────────

    async def list_data_plans(self, *, service_id: str) -> DataPlanList: ...

    async def purchase_data(
        self,
        *,
        request_id: str,
        service_id: str,
        phone: str,
        variation_code: str,
    ) -> BillPurchaseResponse: ...

    # ── Status requery (used by the reconcile worker) ───────────────────

    async def requery(self, *, request_id: str) -> BillPurchaseResponse: ...


# ── Domain exceptions — raised by the real client, caught by BillService ──

class ProviderTemporaryFailure(Exception):
    """Transient error (network, 5xx, timeout) — BillService should leave
    the transaction PENDING and let the reconcile worker requery."""


class ProviderPermanentFailure(Exception):
    """Non-retryable error (400 / 422 / invalid service / bad amount) —
    BillService should mark the transaction FAILED and refund any debit
    already taken from the wallet."""
