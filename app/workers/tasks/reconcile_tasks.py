"""Reconcile pending payments by polling Paystack verify.

Runs every 2 minutes via Celery beat. Catches transactions where the
webhook didn't arrive (network blip, Paystack outage). Races safely
against the webhook handler: before mutating a Payment row, both paths
re-fetch with SELECT FOR UPDATE and no-op if status is no longer pending.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.logger import log
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.session import SessionLocal
from app.integrations.paystack.factory import select_paystack_client
from app.services.transaction_service import TransactionService
from app.services.wallet_service import (
    InsufficientBalance,
    KycCapExceeded,
    WalletService,
)
from app.workers.celery_app import celery_app


# Keep in sync with the same-named set in webhooks.py. Only outbound tx
# types debit the user before the provider settles, so only these
# warrant a refund when the charge ultimately fails.
_REFUNDABLE_ON_FAILURE = {
    TransactionType.airtime,
    TransactionType.data,
    TransactionType.electricity,
    TransactionType.cable,
    TransactionType.flight,
}


@celery_app.task(name="app.workers.tasks.reconcile_tasks.reconcile_pending_payments")
def reconcile_pending_payments() -> dict:
    """Query payments in PENDING for >30s and verify with Paystack."""
    return asyncio.run(_reconcile())


async def _reconcile() -> dict:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
        pending = (
            db.query(Payment, Transaction)
            .join(Transaction, Transaction.id == Payment.transaction_id)
            .filter(
                Payment.status == PaymentStatus.pending,
                Payment.created_at < cutoff,
                Transaction.status.in_([
                    TransactionStatus.pending, TransactionStatus.processing
                ]),
            )
            .limit(50)
            .all()
        )
        if not pending:
            return {"checked": 0, "settled": 0}

        client = select_paystack_client()
        wallet_svc = WalletService(db=db)
        tx_svc = TransactionService(db=db)
        settled = 0
        deferred = 0
        for payment, tx in pending:
            try:
                v = await client.verify(reference=payment.provider_reference)
            except Exception as exc:
                # Provider / network error — retry on the next tick.
                log.warning(
                    "reconcile: verify failed for %s: %s",
                    payment.provider_reference, exc,
                )
                continue
            if v.status == "success":
                if v.authorization is not None:
                    payment.method    = v.authorization.channel
                    payment.last4     = v.authorization.last4
                    payment.bank_name = v.authorization.bank
                if _claim_payment(db, payment.id, PaymentStatus.success):
                    try:
                        wallet_svc.credit(user_id=tx.user_id, amount=tx.amount)
                    except (KycCapExceeded, InsufficientBalance) as exc:
                        # Domain exception on an otherwise-valid payment —
                        # defer. Leave Payment pending so the next tick can
                        # retry (ops may raise the user's cap meanwhile).
                        # Partial state is discarded by the db.rollback().
                        db.rollback()
                        deferred += 1
                        log.warning(
                            "reconcile: DEFERRED credit — ref=%s user=%s err=%s",
                            payment.provider_reference, tx.user_id, exc,
                        )
                        # Skip the transition + settled++; move to next row.
                        continue
                    tx_svc.transition(
                        tx, to_status=TransactionStatus.success,
                        reason="reconcile.verify.success",
                    )
                    settled += 1
            elif v.status == "failed":
                if _claim_payment(db, payment.id, PaymentStatus.failed):
                    tx_svc.transition(
                        tx, to_status=TransactionStatus.failed,
                        reason="reconcile.verify.failed",
                    )
                    # See webhooks.py: only refund outbound tx types.
                    if tx.type in _REFUNDABLE_ON_FAILURE:
                        refund = tx_svc.create_refund(
                            original_tx=tx,
                            amount=tx.amount,
                            reason="reconcile.verify.failed",
                        )
                        wallet_svc.credit(user_id=tx.user_id, amount=refund.amount)
                    settled += 1
            # abandoned → leave pending for next poll
        db.commit()
        return {"checked": len(pending), "settled": settled, "deferred": deferred}
    finally:
        db.close()


def _claim_payment(db, payment_id, target_status: PaymentStatus) -> bool:
    """Atomically flip Payment.status pending → target. Returns True if this call
    won the race. A no-op if the row was already mutated by the webhook handler
    (or a concurrent reconcile run)."""
    locked = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .with_for_update()
        .one()
    )
    if locked.status != PaymentStatus.pending:
        return False
    locked.status = target_status
    return True
