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

from app.api.deps import (
    get_bill_service,
    get_db,
    get_paystack_provider,
    get_wallet_service,
)
from app.core.limiter import limiter
from app.core.logger import log
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.webhook_event import WebhookEvent
from app.integrations.paystack.base import PaymentProvider
from app.integrations.vtpass.client import translate_response as vtpass_translate
from app.integrations.vtpass.signature import (
    WebhookSecretNotConfigured,
    verify_vtpass_secret,
)
from app.services.bill_service import BillService, REFUNDABLE_ON_FAILURE
from app.services.notification_service import (
    NotificationEvent,
    build_wallet_funded_context,
)
from app.services.transaction_service import InvalidStateTransition, TransactionService
from app.services.wallet_service import KycCapExceeded, WalletService
from app.utils.responses import success
from app.workers.tasks.notification_tasks import dispatch_delay


# Lifted to app.services.bill_service (S3C-M10) — imported above.
# Local alias to keep the existing call sites tidy without a big rename.
_REFUNDABLE_ON_FAILURE = REFUNDABLE_ON_FAILURE


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
@limiter.limit("60/minute")
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
        # Unknown reference — still record the event for audit but
        # log.warning so incident triage has a concrete signal beyond
        # the webhook_events table (which nobody tails). Common
        # causes: misrouted webhooks (wrong env pointing at prod),
        # secret leaked and replayed. S3C-M3.
        log.warning(
            "paystack webhook: unknown reference %s event=%s",
            reference, event_id,
        )
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
            try:
                wallet_svc.credit(user_id=tx.user_id, amount=tx.amount)
            except KycCapExceeded as exc:
                # Don't leak partial state: roll back the claim and the
                # WebhookEvent insert so Paystack can retry (or so an
                # operator can raise the user's tier and try again).
                db.rollback()
                log.warning(
                    "webhook: credit would exceed KYC cap — user=%s ref=%s event=%s err=%s",
                    tx.user_id, reference, event_id, exc,
                )
                raise HTTPException(status_code=422, detail={
                    "code": "KYC_LIMIT_EXCEEDED",
                    "message": "Credit would exceed the user's KYC balance cap",
                })
            tx_svc.transition(
                tx,
                to_status=TransactionStatus.success,
                reason="paystack.webhook.charge.success",
                context={"paystack_event_id": event_id},
            )
            # Notify: only wallet-funding lands a balance change the
            # user cares about here. Outbound-tx charges don't come
            # through this endpoint with a success status.
            if tx.type == TransactionType.wallet_funding:
                # Re-fetch wallet balance so the email shows the right
                # number; commit above made the credit visible.
                from app.db.models.user import User
                from app.db.models.wallet import Wallet
                wallet = db.query(Wallet).filter(Wallet.user_id == tx.user_id).first()
                user = db.query(User).filter(User.id == tx.user_id).first()
                if user is not None:
                    dispatch_delay(
                        user_id=str(tx.user_id),
                        user_email=user.email,
                        event=NotificationEvent.wallet_funded,
                        context=build_wallet_funded_context(
                            amount=tx.amount,
                            balance=wallet.balance if wallet else tx.amount,
                            reference=tx.reference,
                            channel=(v.authorization.channel if v.authorization else None),
                        ),
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
                refund, was_created = tx_svc.create_refund(
                    original_tx=tx,
                    amount=tx.amount,
                    reason=f"paystack.webhook.{event_type}",
                )
                # Idempotency guard — if reconcile already refunded this
                # tx, was_created is False and we must NOT credit again.
                # See S3C-P1.
                if was_created:
                    try:
                        wallet_svc.credit(
                            user_id=tx.user_id, amount=refund.amount,
                        )
                    except KycCapExceeded as exc:
                        # The user's tier may have been lowered between
                        # the original debit and this refund attempt. If
                        # we let the exception bubble as 500, Paystack
                        # retries, the WebhookEvent dedupe swallows the
                        # retry, and the refund row stays orphaned
                        # (rolled back together with the credit) — user
                        # silently left without their money.
                        # Instead: roll back, return 422 so Paystack
                        # keeps retrying on its cadence until ops
                        # raises the user's cap. See S3C-P4a.
                        db.rollback()
                        log.warning(
                            "paystack webhook: refund credit would exceed "
                            "KYC cap — user=%s ref=%s event=%s err=%s",
                            tx.user_id, reference, event_id, exc,
                        )
                        raise HTTPException(status_code=422, detail={
                            "code": "KYC_LIMIT_EXCEEDED",
                            "message": (
                                "Refund would exceed the user's KYC balance "
                                "cap. Ops must raise the tier before the "
                                "refund can land; Paystack will retry."
                            ),
                        })

    we.processed = True
    db.commit()
    return success({"ok": True})


# ─── VTPass webhook ──────────────────────────────────────────────────────

# Tx states that are already final — a late webhook here is a no-op
# (typically the synchronous purchase path already handled this tx, or
# the reconcile worker got there first). We still record the event so
# ops can see it arrived.
_TX_FINAL_STATES = {
    TransactionStatus.success,
    TransactionStatus.failed,
    TransactionStatus.refund_pending,
    TransactionStatus.refunded,
    TransactionStatus.refund_failed,
}


@router.post("/vtpass", response_model=None)
@limiter.limit("60/minute")
async def vtpass_webhook(
    request: Request,
    x_vtpass_secret: str | None = Header(default=None, alias="X-VTPass-Secret"),
    db: Session = Depends(get_db),
    bill_svc: BillService = Depends(get_bill_service),
):
    # 1. Shared-secret auth. VTPass doesn't HMAC-sign bodies like Paystack;
    #    a configured secret header is our only auth surface. The helper
    #    raises WebhookSecretNotConfigured if VTPASS_WEBHOOK_SECRET is
    #    unset — we translate that to 500 rather than silently accept
    #    every forged request.
    try:
        if not verify_vtpass_secret(header_value=x_vtpass_secret):
            raise HTTPException(status_code=401, detail={
                "code": "INVALID_SECRET", "message": "Bad or missing X-VTPass-Secret header",
            })
    except WebhookSecretNotConfigured as exc:
        log.error("vtpass webhook: server misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail={
            "code": "WEBHOOK_NOT_CONFIGURED",
            "message": "Server-side VTPass webhook secret is not set",
        })

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Body is not valid JSON",
        })

    # 2. Identifiers. VTPass echoes our `request_id` back as `requestId`
    #    (camelCase) at the top level. Their own transaction id lives in
    #    `content.transactions.transactionId`. We use the latter for
    #    dedupe when available, falling back to request_id + code so
    #    events that arrive before VTPass assigns a transaction id still
    #    have a stable natural key.
    reference = payload.get("requestId") or payload.get("request_id")
    content = payload.get("content") or {}
    tx_block = (content.get("transactions") or {}) if isinstance(content, dict) else {}
    vtpass_event_id = (
        tx_block.get("transactionId")
        or tx_block.get("transaction_id")
        or f"{reference}:{payload.get('code', '')}"
    )
    event_type = payload.get("type") or "transaction-update"

    if not reference:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing requestId",
        })

    # 3. Atomic dedupe — identical to Paystack's S2C-4 pattern. Two
    #    concurrent retries of the same event race at the unique
    #    constraint; the loser rolls back and returns deduped.
    we = WebhookEvent(
        provider="vtpass",
        provider_event_id=str(vtpass_event_id),
        event_type=str(event_type),
        raw=payload,
        processed=False,
    )
    db.add(we)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return success({"ok": True, "deduped": True})

    # 4. Route webhook → Transaction via the Payment row BillService
    #    wrote on wallet-debit (provider="wallet", provider_reference=tx.reference).
    payment = (
        db.query(Payment)
        .filter(Payment.provider == "wallet")
        .filter(Payment.provider_reference == reference)
        .first()
    )
    if payment is None:
        # Unknown reference — still record the event so ops can audit
        # stray posts (wrong env, secret leaked, etc.), but don't error.
        # log.warning so incident triage has a concrete signal beyond
        # the webhook_events table. S3C-M3.
        log.warning(
            "vtpass webhook: unknown reference %s event=%s",
            reference, vtpass_event_id,
        )
        db.commit()
        return success({"ok": True, "note": "unknown_reference"})

    # 5. Lock the tx row. This is the race guard — both the reconcile
    #    worker and the webhook can arrive concurrently; the second
    #    caller waits here, sees a terminal status, and skips.
    tx = (
        db.query(Transaction)
        .filter(Transaction.id == payment.transaction_id)
        .with_for_update()
        .one()
    )
    if tx.status in _TX_FINAL_STATES:
        we.processed = True
        db.commit()
        return success({"ok": True, "already_processed": True})

    # 6. Translate VTPass envelope → normalized BillPurchaseResponse.
    #    Same shape as purchase + requery responses, so we reuse the
    #    client's parser.
    result = vtpass_translate(payload, request_id=reference, requested=tx.amount)

    # 7. Apply state changes. For delivered → success (refunds the
    #    shortfall on partial); for failed → failed + refund (gated by
    #    _REFUNDABLE_ON_FAILURE — bills are all IN the set); for pending
    #    → no-op, reconcile worker will finish.
    #
    #    Two defensive catches (S3C-H1):
    #    • InvalidStateTransition — the row-lock above makes this
    #      practically unreachable, but TransactionService commits
    #      mid-apply, temporarily releasing the lock. A sufficiently
    #      unlucky reconcile tick could still finalize the tx between
    #      transitions. 500 would make VTPass retry uselessly; return
    #      200 with already_processed=True so they stop.
    #    • KycCapExceeded on the refund credit — mirror of S3C-P4a:
    #      user's tier was lowered between the debit and the refund
    #      attempt. 422 + rollback so VTPass retries on their cadence.
    try:
        bill_svc.apply_provider_result(tx=tx, amount=tx.amount, result=result)
    except InvalidStateTransition as exc:
        log.info(
            "vtpass webhook: race lost to concurrent finalizer tx=%s event=%s: %s",
            reference, vtpass_event_id, exc,
        )
        we.processed = True
        db.commit()
        return success({"ok": True, "already_processed": True})
    except KycCapExceeded as exc:
        db.rollback()
        log.warning(
            "vtpass webhook: refund credit would exceed KYC cap — "
            "user=%s ref=%s event=%s err=%s",
            tx.user_id, reference, vtpass_event_id, exc,
        )
        raise HTTPException(status_code=422, detail={
            "code": "KYC_LIMIT_EXCEEDED",
            "message": (
                "Refund would exceed the user's KYC balance cap. "
                "Ops must raise the tier before the refund can land; "
                "VTPass will retry."
            ),
        })

    we.processed = True
    db.commit()
    return success({"ok": True})
