"""Reconcile pending payments by polling Paystack verify.

Runs every 2 minutes via Celery beat. Catches transactions where the
webhook didn't arrive (network blip, Paystack outage).
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.logger import log
from app.db.models._enums import TransactionStatus
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.session import SessionLocal
from app.integrations.paystack.client import PaystackClient
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.workers.celery_app import celery_app


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

        client = PaystackClient()
        wallet_svc = WalletService(db=db)
        tx_svc = TransactionService(db=db)
        settled = 0
        for payment, tx in pending:
            try:
                v = await client.verify(reference=payment.provider_reference)
            except Exception as exc:
                log.warning("reconcile: verify failed for %s: %s", payment.provider_reference, exc)
                continue
            if v.status == "success":
                payment.status = PaymentStatus.success
                wallet_svc.credit(user_id=tx.user_id, amount=tx.amount)
                tx_svc.transition(
                    tx, to_status=TransactionStatus.success,
                    reason="reconcile.verify.success",
                )
                settled += 1
            elif v.status == "failed":
                payment.status = PaymentStatus.failed
                tx_svc.transition(
                    tx, to_status=TransactionStatus.failed,
                    reason="reconcile.verify.failed",
                )
                settled += 1
            # abandoned → leave pending for next poll
        db.commit()
        return {"checked": len(pending), "settled": settled}
    finally:
        db.close()
