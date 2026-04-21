"""BillService — wallet-first orchestration for airtime, data, and
(in Sprint 4) electricity + cable.

Invariants this service owns:

 1. **Wallet is the only payment source at this layer.** If the user
    didn't have enough balance when the request reached here, we raise
    `InsufficientBalance` and let the endpoint return 402. Top-up-then-
    pay is mobile-side orchestration; by the time we're called, the
    wallet is expected to cover the bill.

 2. **No money disappears.** Every debit is mirrored by exactly one of:
      • a successful bill delivery (happy path), or
      • a refund transaction + wallet credit of the same amount (failure
        path), or
      • a pending state that the reconcile worker resolves to one of
        the above within ~2 minutes.
    Partial delivery refunds the *difference*.

 3. **Refunds reuse Sprint 2's `create_refund`.** Same idempotency-by-
    original-reference guarantee, same `TransactionEvent` audit trail.

 4. **State machine is strict.** Only transitions declared in
    `TransactionService._ALLOWED` are attempted. A bill goes
    pending → processing → {success | failed} in the synchronous path
    and stays at processing when VTPass was transient/pending (reconcile
    worker finishes).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logger import log
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.integrations.vtpass.base import (
    BillProvider,
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    BillPurchaseResponse,
    DataPlanList,
)
from app.services.transaction_service import TransactionService
from app.services.wallet_service import InsufficientBalance, WalletService


# ── Result payload returned to the endpoint layer ────────────────────────

@dataclass(frozen=True, slots=True)
class BillResult:
    """What the endpoint hands back to mobile. The caller uses
    `tx.reference` to drive the status page polling; `response` is the
    normalized VTPass payload (for partial-delivery banners etc.)."""
    tx: Transaction
    response: BillPurchaseResponse


# ── Errors ───────────────────────────────────────────────────────────────

class DataPlanNotFound(Exception):
    """Client asked for a variation_code that VTPass doesn't know."""


# ── Service ──────────────────────────────────────────────────────────────


class BillService:
    def __init__(
        self,
        *,
        db: Session,
        tx_svc: TransactionService,
        wallet_svc: WalletService,
        provider: BillProvider,
    ) -> None:
        self._db = db
        self._tx = tx_svc
        self._wallet = wallet_svc
        self._provider = provider

    # ── Public API ──────────────────────────────────────────────────────

    async def purchase_airtime(
        self,
        *,
        user_id: UUID,
        network: str,
        phone: str,
        amount_ngn: Decimal,
    ) -> BillResult:
        service_id = network.lower()
        meta = {
            "network":    network.upper(),
            "phone":      phone,
            "service_id": service_id,
        }
        return await self._execute_bill(
            user_id=user_id,
            tx_type=TransactionType.airtime,
            amount=amount_ngn,
            meta=meta,
            provider_fn=lambda req_id: self._provider.purchase_airtime(
                request_id=req_id,
                service_id=service_id,
                phone=phone,
                amount_ngn=amount_ngn,
            ),
        )

    async def purchase_data(
        self,
        *,
        user_id: UUID,
        network: str,
        phone: str,
        variation_code: str,
    ) -> BillResult:
        service_id = f"{network.lower()}-data"
        # Resolve price server-side — never trust a client-sent amount.
        plans = await self._provider.list_data_plans(service_id=service_id)
        match = next(
            (v for v in plans.variations if v.variation_code == variation_code),
            None,
        )
        if match is None:
            raise DataPlanNotFound(
                f"Unknown data plan {variation_code!r} for {service_id}"
            )
        meta = {
            "network":        network.upper(),
            "phone":          phone,
            "service_id":     service_id,
            "variation_code": variation_code,
            "plan_name":      match.name,
        }
        return await self._execute_bill(
            user_id=user_id,
            tx_type=TransactionType.data,
            amount=match.price_ngn,
            meta=meta,
            provider_fn=lambda req_id: self._provider.purchase_data(
                request_id=req_id,
                service_id=service_id,
                phone=phone,
                variation_code=variation_code,
            ),
        )

    async def list_data_plans(self, *, network: str) -> DataPlanList:
        """Passthrough for the `GET /bills/data/plans` endpoint. Live
        fetch per product decision — no Redis cache."""
        return await self._provider.list_data_plans(
            service_id=f"{network.lower()}-data"
        )

    # ── Orchestration internals ─────────────────────────────────────────

    async def _execute_bill(
        self,
        *,
        user_id: UUID,
        tx_type: TransactionType,
        amount: Decimal,
        meta: dict,
        provider_fn: Callable[[str], Awaitable[BillPurchaseResponse]],
    ) -> BillResult:
        # 1. Create the tx (pending). Commits immediately.
        tx = self._tx.create(
            user_id=user_id, type=tx_type, amount=amount, meta=meta
        )

        # 2. Debit wallet. Atomic + row-locked. Raises InsufficientBalance.
        try:
            self._wallet.debit(user_id=user_id, amount=amount)
        except InsufficientBalance:
            # Mark the tx failed with an audit reason so ops can see why
            # we never called the provider.
            self._tx.transition(
                tx, to_status=TransactionStatus.failed,
                reason="insufficient_balance_before_provider",
            )
            raise

        # 3. Record the wallet-debit Payment row (mirror of Sprint 2's
        #    Paystack Payment rows — single source of truth for "where did
        #    money move?"). provider="wallet", provider_reference=tx.reference.
        self._db.add(Payment(
            transaction_id=tx.id,
            provider="wallet",
            provider_reference=tx.reference,
            status=PaymentStatus.success,
        ))
        self._tx.transition(
            tx, to_status=TransactionStatus.processing,
            reason="wallet_debit",
        )

        # 4. Call the provider.
        try:
            result = await provider_fn(tx.reference)
        except ProviderTemporaryFailure as exc:
            # Network / 5xx / timeout. We don't know whether VTPass received
            # the request — leave tx in processing; the reconcile worker
            # will requery. The wallet debit stays put so the user can't
            # double-spend. If reconcile's requery later reports "not
            # found", Sprint 3 B12 refunds.
            log.warning(
                "bill provider transient failure tx=%s err=%s",
                tx.reference, exc,
            )
            return BillResult(
                tx=tx,
                response=_placeholder_pending(tx.reference, amount),
            )
        except ProviderPermanentFailure as exc:
            # 4xx / bad request / unknown variation — provider will never
            # deliver. Mark failed, then refund + credit back to wallet.
            log.warning(
                "bill provider permanent failure tx=%s err=%s",
                tx.reference, exc,
            )
            self._tx.transition(
                tx, to_status=TransactionStatus.failed,
                reason=f"provider_permanent_failure: {exc}",
            )
            self._refund_and_credit(
                tx, amount=amount,
                reason=f"provider_permanent_failure: {exc}",
            )
            return BillResult(
                tx=tx,
                response=_placeholder_failed(tx.reference, amount, description=str(exc)),
            )

        # 5. Translate the normalized response into state changes.
        return self._apply_provider_result(tx=tx, amount=amount, result=result)

    def _apply_provider_result(
        self, *, tx: Transaction, amount: Decimal, result: BillPurchaseResponse
    ) -> BillResult:
        if result.status == BillDeliveryStatus.delivered:
            if result.delivered_amount_ngn < amount:
                # Partial — refund the difference and annotate the tx.
                shortfall = amount - result.delivered_amount_ngn
                self._refund_and_credit(
                    tx, amount=shortfall,
                    reason="partial_delivery_shortfall",
                )
                tx.meta = {
                    **(tx.meta or {}),
                    "partial_delivery":     True,
                    "delivered_amount_ngn": str(result.delivered_amount_ngn),
                    "shortfall_ngn":        str(shortfall),
                    "vtpass_transaction_id": result.transaction_id,
                }
            else:
                tx.meta = {
                    **(tx.meta or {}),
                    "vtpass_transaction_id": result.transaction_id,
                }
            self._tx.transition(
                tx, to_status=TransactionStatus.success,
                reason="provider_delivered",
                context={"code": result.code},
            )
            return BillResult(tx=tx, response=result)

        if result.status == BillDeliveryStatus.pending:
            # Provider accepted but upstream telco hasn't confirmed.
            # Reconcile worker owns the final transition.
            return BillResult(tx=tx, response=result)

        # FAILED
        self._refund_and_credit(
            tx, amount=amount,
            reason=f"provider_failed_code_{result.code}",
        )
        self._tx.transition(
            tx, to_status=TransactionStatus.failed,
            reason=f"provider_failed_code_{result.code}",
            context={"description": result.description},
        )
        return BillResult(tx=tx, response=result)

    def _refund_and_credit(
        self, tx: Transaction, *, amount: Decimal, reason: str
    ) -> None:
        """Create a refund tx and credit the wallet with that amount. The
        refund row is idempotent by `original_tx.reference` (see Sprint 2
        `create_refund`), so this is safe to call twice without
        double-crediting — the second call returns the existing row and
        skips the credit."""
        refund = self._tx.create_refund(
            original_tx=tx, amount=amount, reason=reason,
        )
        # `create_refund` is idempotent-by-original-reference; if it
        # returned the pre-existing refund row we must NOT credit again.
        # Detect this by matching reason on the event audit (freshly
        # created refund has one event with our reason; a replay has a
        # prior event with a different reason).
        existing_events = self._tx.events_for(refund)
        has_our_reason = any(
            e.reason and e.reason == reason for e in existing_events
        )
        if has_our_reason and len(existing_events) == 1:
            # This was a fresh create — apply the credit.
            self._wallet.credit(user_id=tx.user_id, amount=refund.amount)


# ── Placeholder response builders for transient / permanent failure paths

def _placeholder_pending(reference: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=reference,
        transaction_id="",
        status=BillDeliveryStatus.pending,
        code="TIMP_TRANSIENT",
        requested_amount_ngn=amount,
        delivered_amount_ngn=Decimal("0.00"),
        description="Provider transient failure — awaiting reconcile",
        raw={"timp_placeholder": "transient"},
    )


def _placeholder_failed(
    reference: str, amount: Decimal, *, description: str = ""
) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=reference,
        transaction_id="",
        status=BillDeliveryStatus.failed,
        code="TIMP_PERMANENT",
        requested_amount_ngn=amount,
        delivered_amount_ngn=Decimal("0.00"),
        description=description or "Provider rejected the request",
        raw={"timp_placeholder": "permanent"},
    )
