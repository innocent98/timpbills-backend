"""Paystack webhook receiver.

1. Verify HMAC-SHA512 signature using raw request body.
2. Dedupe on provider_event_id in webhook_events table.
3. For charge.success: mark Payment.success, credit wallet,
   transition Transaction to success.
4. For charge.failed: mark Payment.failed, transition Transaction to failed.
   For OUTBOUND tx types (airtime/data/etc.) a refund is also issued;
   for wallet_funding we must NOT refund — the user was never debited.
"""
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_paystack_provider, get_wallet_service
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.webhook_event import WebhookEvent
from app.integrations.paystack.base import PaymentProvider
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService
from app.utils.responses import success


# Transaction types where a failed Paystack charge means we already debited the
# user's wallet (or equivalent) — so we issue a refund on failure. Wallet
# funding is excluded because the charge going through was what would have
# credited the wallet in the first place; a declined card never took money
# from the user, so there is nothing to refund.
_REFUNDABLE_ON_FAILURE = {
    TransactionType.airtime,
    TransactionType.data,
    TransactionType.electricity,
    TransactionType.cable,
    TransactionType.flight,
}


router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _claim_payment(db: Session, payment_id, target_status: PaymentStatus) -> bool:
    """Atomically flip Payment.status pending → target; no-op if already moved."""
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


@router.post("/paystack", response_model=None)
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str | None = Header(default=None, alias="x-paystack-signature"),
    db: Session = Depends(get_db),
    paystack: PaymentProvider = Depends(get_paystack_provider),
    wallet_svc: WalletService = Depends(get_wallet_service),
):
    raw_body = await request.body()
    if not paystack.verify_signature(raw_body=raw_body, signature=x_paystack_signature or ""):
        raise HTTPException(status_code=401, detail={
            "code": "INVALID_SIGNATURE", "message": "Bad signature"
        })

    payload = json.loads(raw_body or b"{}")
    event_type = payload.get("event", "")
    event_id = str(payload.get("data", {}).get("id", ""))
    reference = payload.get("data", {}).get("reference")

    if not event_id or not reference:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.id or data.reference"
        })

    # Dedupe atomically — attempt the insert and let the unique constraint on
    # provider_event_id be the arbiter. Two concurrent retries of the same
    # event both reach here; the DB serialises them and the loser rolls back
    # and returns deduped. This replaces an earlier query-then-insert pattern
    # that had a race window where both requests could pass the existence
    # check before either committed.
    we = WebhookEvent(
        provider="paystack",
        provider_event_id=event_id,
        event_type=event_type,
        raw=payload,
        processed=False,
    )
    db.add(we)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return success({"ok": True, "deduped": True})

    # Lookup payment → transaction
    payment = (
        db.query(Payment).filter(Payment.provider_reference == reference).first()
    )
    if payment is None:
        # Unknown reference — still record the event for audit.
        db.commit()
        return success({"ok": True, "note": "unknown_reference"})

    tx = (
        db.query(Transaction).filter(Transaction.id == payment.transaction_id).first()
    )
    tx_svc = TransactionService(db=db)

    if event_type == "charge.success":
        # Guard: verify with Paystack to confirm (defensive against fake signatures)
        v = await paystack.verify(reference=reference)
        if v.status != "success":
            raise HTTPException(status_code=400, detail={
                "code": "PAYSTACK_VERIFY_MISMATCH",
                "message": f"Webhook says success but verify says {v.status}",
            })
        # Populate Payment with authorization details (method, last4, bank).
        if v.authorization is not None:
            payment.method    = v.authorization.channel
            payment.last4     = v.authorization.last4
            payment.bank_name = v.authorization.bank
        # Race guard: reconcile worker may have claimed this payment already.
        if _claim_payment(db, payment.id, PaymentStatus.success):
            wallet_svc.credit(user_id=tx.user_id, amount=tx.amount)
            tx_svc.transition(
                tx,
                to_status=TransactionStatus.success,
                reason="paystack.webhook.charge.success",
                context={"paystack_event_id": event_id},
            )
    elif event_type in ("charge.failed", "transfer.failed"):
        if _claim_payment(db, payment.id, PaymentStatus.failed):
            tx_svc.transition(
                tx,
                to_status=TransactionStatus.failed,
                reason=f"paystack.webhook.{event_type}",
                context={"paystack_event_id": event_id},
            )
            # Only issue a refund when the original tx actually debited the
            # user (outbound services). A declined wallet-funding charge
            # never collected money, so there is nothing to refund.
            if tx.type in _REFUNDABLE_ON_FAILURE:
                refund = tx_svc.create_refund(
                    original_tx=tx,
                    amount=tx.amount,
                    reason=f"paystack.webhook.{event_type}",
                )
                wallet_svc.credit(user_id=tx.user_id, amount=refund.amount)

    we.processed = True
    db.commit()
    return success({"ok": True})
