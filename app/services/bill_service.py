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
from app.services.notification_service import (
    NotificationEvent,
    build_bill_context,
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


# Tx states that are already final — hitting any state-changing path
# with the tx in one of these would raise InvalidStateTransition. Kept
# here (not imported from webhooks.py) so BillService can row-lock +
# skip before calling transition()/create_refund(). Kept in lock-step
# with webhooks.vtpass_webhook and reconcile_tasks._reconcile_bills.
_TX_FINAL_STATES = {
    TransactionStatus.success,
    TransactionStatus.failed,
    TransactionStatus.refund_pending,
    TransactionStatus.refunded,
    TransactionStatus.refund_failed,
}


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
        permanent_err: ProviderPermanentFailure | None = None
        result: BillPurchaseResponse | None = None
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
            # deliver. We'll transition + refund after the row-lock step
            # below so a concurrent webhook doesn't trip InvalidStateTransition.
            log.warning(
                "bill provider permanent failure tx=%s err=%s",
                tx.reference, exc,
            )
            permanent_err = exc

        # 5. Re-fetch the tx with a row-lock. While we were awaiting the
        #    provider, the VTPass webhook or reconcile worker may have
        #    finalized this tx — applying state changes blind would race
        #    with them and either double-refund or raise InvalidStateTransition.
        #    Guard: if the tx is already terminal, the concurrent finalizer
        #    owns the outcome; we return their state. This is S3C-P2.
        locked_tx = (
            self._db.query(Transaction)
            .filter(Transaction.id == tx.id)
            .with_for_update()
            .one()
        )
        if locked_tx.status in _TX_FINAL_STATES:
            log.info(
                "bill sync: tx %s finalized concurrently (status=%s); "
                "skipping apply",
                locked_tx.reference, locked_tx.status.value,
            )
            placeholder = (
                result
                if result is not None
                else _placeholder_failed(
                    locked_tx.reference, amount,
                    description=str(permanent_err or "concurrent finalization"),
                )
            )
            return BillResult(tx=locked_tx, response=placeholder)

        # 6. Apply. Permanent failure has its own path (refund + transition);
        #    everything else goes through apply_provider_result.
        if permanent_err is not None:
            self._tx.transition(
                locked_tx, to_status=TransactionStatus.failed,
                reason=f"provider_permanent_failure: {permanent_err}",
            )
            self._refund_and_credit(
                locked_tx, amount=amount,
                reason=f"provider_permanent_failure: {permanent_err}",
            )
            _notify_bill_failure_refund(
                db=self._db, tx=locked_tx, amount=amount, reason=str(permanent_err),
            )
            return BillResult(
                tx=locked_tx,
                response=_placeholder_failed(
                    locked_tx.reference, amount, description=str(permanent_err),
                ),
            )

        assert result is not None  # narrowed by the except branches above
        return self.apply_provider_result(tx=locked_tx, amount=amount, result=result)

    def apply_provider_result(
        self, *, tx: Transaction, amount: Decimal, result: BillPurchaseResponse
    ) -> BillResult:
        """Apply a provider result (either from the synchronous purchase
        path or from the /webhooks/vtpass handler) to the tx state.

        Assumes the caller has already verified the tx is not yet in a
        terminal state — calling this on an already-final tx raises
        InvalidStateTransition via TransactionService.transition."""
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
            _notify_bill_success(db=self._db, tx=tx, amount=amount, result=result)
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
        _notify_bill_failure_refund(
            db=self._db, tx=tx, amount=amount, reason=result.description,
        )
        return BillResult(tx=tx, response=result)

    def _refund_and_credit(
        self, tx: Transaction, *, amount: Decimal, reason: str
    ) -> None:
        """Create a refund tx and credit the wallet with that amount.

        Safe against repeated invocation: ``create_refund`` is idempotent
        by ``original_tx.reference`` and returns ``(refund, was_created)``.
        We credit only when the refund row is freshly minted, so two
        concurrent callers with the same reason (e.g. sync-purchase and
        webhook racing on the same failed tx) can't double-credit — an
        earlier heuristic that inferred "freshness" from the event list
        was fragile; see S3C-P1 commit."""
        refund, was_created = self._tx.create_refund(
            original_tx=tx, amount=amount, reason=reason,
        )
        if was_created:
            self._wallet.credit(user_id=tx.user_id, amount=refund.amount)


# ── Placeholder response builders for transient / permanent failure paths

def _notify_bill_success(
    *, db: Session, tx: Transaction, amount: Decimal, result: BillPurchaseResponse
) -> None:
    """Fire-and-forget email + push for a successful bill delivery.
    Inside a helper so the BillService happy-path reads clean, and so
    tests can patch this one symbol instead of the whole Celery task."""
    from app.db.models.user import User
    from app.workers.tasks.notification_tasks import dispatch_delay

    user = db.query(User).filter(User.id == tx.user_id).first()
    if user is None:
        log.warning("notify: tx %s has no user row — skipping", tx.reference)
        return

    partial = result.delivered_amount_ngn < amount
    meta = tx.meta or {}
    ctx = build_bill_context(
        tx_type=tx.type.value,
        amount=amount,
        destination=str(meta.get("phone") or meta.get("destination") or ""),
        reference=tx.reference,
        when=tx.created_at.isoformat() if tx.created_at else "",
        partial=partial,
        delivered_amount=result.delivered_amount_ngn if partial else None,
        shortfall=(amount - result.delivered_amount_ngn) if partial else None,
    )
    dispatch_delay(
        user_id=str(tx.user_id), user_email=user.email,
        event=NotificationEvent.bill_success, context=ctx,
    )


def _notify_bill_failure_refund(
    *, db: Session, tx: Transaction, amount: Decimal, reason: str = ""
) -> None:
    from app.db.models.user import User
    from app.workers.tasks.notification_tasks import dispatch_delay

    user = db.query(User).filter(User.id == tx.user_id).first()
    if user is None:
        log.warning("notify: tx %s has no user row — skipping", tx.reference)
        return

    meta = tx.meta or {}
    ctx = build_bill_context(
        tx_type=tx.type.value,
        amount=amount,
        destination=str(meta.get("phone") or meta.get("destination") or ""),
        reference=tx.reference,
        when=tx.created_at.isoformat() if tx.created_at else "",
        partial=False,
    )
    # The failure template shows a reason line when present.
    ctx["reason"] = reason
    dispatch_delay(
        user_id=str(tx.user_id), user_email=user.email,
        event=NotificationEvent.bill_failure_refund, context=ctx,
    )


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
