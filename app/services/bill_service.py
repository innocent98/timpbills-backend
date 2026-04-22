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

from redis.asyncio import Redis
from redis.exceptions import RedisError
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
    MeterValidation,
)
from app.services.notification_service import (
    NotificationEvent,
    build_bill_context,
)
from app.services.transaction_service import TransactionService
from app.services.wallet_service import InsufficientBalance, WalletService
from app.utils.references import new_transaction_reference


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
# with the tx in one of these would raise InvalidStateTransition. Used
# by BillService (sync purchase path), the vtpass webhook, and the
# reconcile worker — they all import from here (S3C-M10).
_TX_FINAL_STATES = {
    TransactionStatus.success,
    TransactionStatus.failed,
    TransactionStatus.refund_pending,
    TransactionStatus.refunded,
    TransactionStatus.refund_failed,
}


# Tx types where a failed upstream charge means the user was already
# debited, so a refund + wallet credit is owed. Wallet funding is
# intentionally OMITTED (S2C-1): a declined card never took money,
# there's nothing to refund. Single source of truth — webhooks.py
# and reconcile_tasks.py import from here (S3C-M10).
REFUNDABLE_ON_FAILURE = {
    TransactionType.airtime,
    TransactionType.data,
    TransactionType.electricity,
    TransactionType.cable,
    TransactionType.flight,
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
        redis: Redis,
    ) -> None:
        self._db = db
        self._tx = tx_svc
        self._wallet = wallet_svc
        self._provider = provider
        self._redis = redis

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

    # ── Electricity: meter validation (no tx row, no wallet debit) ──────

    async def validate_meter(
        self,
        *,
        user_id: UUID,
        service_id: str,
        meter_number: str,
        meter_type: str,
    ) -> MeterValidation:
        """Validate a DisCo meter number via the provider, with a 5-minute
        per-user Redis cache.

        Validation is NOT a transaction — no `Transaction` row is created,
        no wallet debit is issued. VTPass's merchant-verify endpoint still
        wants a `request_id`, so we mint a throwaway reference with a
        ``TMP-MV`` prefix so ops can tell validation refs from real tx
        refs in logs.

        Cache key includes the user_id so we never surface one user's
        lookup to another (defence-in-depth — the DisCo response has the
        customer's name + address, and while two different users could
        legitimately query the same meter, we'd rather hit VTPass twice
        than leak a cached identity across user contexts).

        Failure modes are re-raised unwrapped so the endpoint layer can
        map them to 400 (permanent) / 503 (temporary). Neither failure
        populates the cache — caching a transient failure would prolong
        the outage, and caching a permanent failure makes legitimate
        retries after the user fixes a typo pointlessly slow.
        """
        key = f"bill_validate:meter:{user_id}:{service_id}:{meter_number}"

        # 1. Cache lookup — fast-path the common "user submits then edits
        #    one character" pattern. Redis outages must NOT block validation
        #    (no money at stake here); degrade to "call VTPass every time".
        try:
            cached = await self._redis.get(key)
        except RedisError as exc:
            log.warning(
                "validate_meter: cache read failed, falling through to provider: %s",
                exc,
            )
            cached = None

        if cached is not None:
            return MeterValidation.model_validate_json(cached)

        # 2. Cache miss — mint a validation-only reference and hit VTPass.
        request_id = new_transaction_reference(
            user_id=str(user_id), prefix="TMP-MV",
        )
        # Any ProviderPermanentFailure / ProviderTemporaryFailure bubbles
        # up untouched; we intentionally do NOT catch-and-cache.
        validation = await self._provider.validate_meter(
            request_id=request_id,
            service_id=service_id,
            meter_number=meter_number,
            meter_type=meter_type,
        )

        # 3. Success → cache for 5 minutes. pydantic's JSON round-trip
        #    handles Decimal + enum serialization for us. Cache write
        #    failure is non-fatal — we've already produced the result,
        #    so just log and return.
        try:
            await self._redis.set(key, validation.model_dump_json(), ex=300)
        except RedisError as exc:
            log.warning(
                "validate_meter: cache write failed, continuing without caching: %s",
                exc,
            )
        return validation

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
                if result.delivered_amount_ngn > amount:
                    # Over-delivery. VTPass shouldn't do this in practice,
                    # but no schema bound prevents it, and no existing
                    # sanity check was in place. Log at ERROR so ops
                    # sees it — the user has been credited more airtime
                    # than they paid for, and we want a concrete trail
                    # to chase. S3C-M4. (We DO still honor it — failing
                    # the tx here would be worse UX.)
                    log.error(
                        "over-delivery from provider tx=%s requested=%s delivered=%s",
                        tx.reference, amount, result.delivered_amount_ngn,
                    )
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
        # Data-integrity violation: the Transaction's user_id FK can't
        # resolve. This should be unreachable (FK constraint); if we
        # hit it, something is seriously wrong (tests seeding directly
        # around ORM, race with user deletion, etc.). Promoting to
        # ERROR so ops sees it in alerting. S3C-M2.
        log.error(
            "notify: tx %s has no user row — FK violation? Skipping dispatch.",
            tx.reference,
        )
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
        log.error(
            "notify: tx %s has no user row — FK violation? Skipping dispatch.",
            tx.reference,
        )
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
