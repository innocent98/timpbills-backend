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
from decimal import Decimal

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
from app.db.models._enums import (
    TransactionStatus,
    TransactionType,
    VirtualAccountStatus,
)
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.virtual_account import VirtualAccount
from app.db.models.webhook_event import WebhookEvent
from app.integrations.paystack.base import PaymentProvider
from app.integrations.vtpass.client import translate_response as vtpass_translate
from app.integrations.vtpass.signature import (
    WebhookSecretNotConfigured,
    verify_vtpass_secret,
)
from app.services.bill_service import REFUNDABLE_ON_FAILURE, BillService
from app.services.notification_service import (
    NotificationEvent,
    build_dva_context,
    build_wallet_funded_context,
)
from app.services.transaction_service import InvalidStateTransition, TransactionService
from app.services.wallet_service import KycCapExceeded, OverCapPolicy, WalletService
from app.utils.responses import success
from app.workers.tasks.notification_tasks import dispatch_delay

# Lifted to app.services.bill_service (S3C-M10) — imported above.
# Local alias to keep the existing call sites tidy without a big rename.
_REFUNDABLE_ON_FAILURE = REFUNDABLE_ON_FAILURE

# Paystack DVA identity/assign lifecycle events carry NO top-level ``data.id``
# — they are keyed only by ``customer`` / ``dedicated_account``. The generic
# ``data.id``-mandatory guard was written for charge events; without special
# handling it rejects these with 400, so the assigned account number never
# lands and the VA hangs in ``pending_assign`` forever. See the id synthesis
# in ``paystack_webhook`` below.
_DVA_LIFECYCLE_EVENTS = {
    "customeridentification.success",
    "customeridentification.failed",
    "dedicatedaccount.assign.success",
    "dedicatedaccount.assign.failed",
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
    data = payload.get("data", {}) or {}
    event_id = str(data.get("id", "")).strip()
    reference = data.get("reference")

    # DVA lifecycle events have no ``data.id`` (see _DVA_LIFECYCLE_EVENTS).
    # Synthesize a stable dedupe key from the natural identifiers so a Paystack
    # retry still collides at the WebhookEvent unique constraint, and — crucially
    # — so the event is NOT rejected by the id-mandatory guard below.
    if not event_id and event_type in _DVA_LIFECYCLE_EVENTS:
        cc = (
            data.get("customer_code")
            or (data.get("customer") or {}).get("customer_code")
            or ""
        )
        da_id = str((data.get("dedicated_account") or {}).get("id") or "")
        event_id = f"{event_type}:{cc}:{da_id}"

    if not event_id:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.id"
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

    # ── DVA identity + assign lifecycle (resolved by customer_code) ──────
    # These events carry no reference we minted, so they are handled BEFORE
    # the reference-mandatory MALFORMED check below.
    if event_type in _DVA_LIFECYCLE_EVENTS:
        customer_code = (
            data.get("customer_code")
            or (data.get("customer") or {}).get("customer_code")
        )
        va = (
            db.query(VirtualAccount)
            .filter(VirtualAccount.paystack_customer_code == customer_code)
            .first()
        )
        if va is None:
            log.warning(
                "paystack webhook: DVA event for unknown customer_code=%s event=%s",
                customer_code, event_id,
            )
            we.processed = True
            db.commit()
            return success({"ok": True, "note": "unknown_customer"})

        notify_event: NotificationEvent | None = None
        notify_ctx: dict | None = None

        if event_type == "customeridentification.success":
            va.status = VirtualAccountStatus.pending_assign
        elif event_type == "customeridentification.failed":
            va.status = VirtualAccountStatus.failed
            va.failure_reason = data.get("reason") or "Identity verification failed"
            notify_event = NotificationEvent.dva_failed
            notify_ctx = build_dva_context(status="failed", reason=va.failure_reason)
        elif event_type == "dedicatedaccount.assign.success":
            acct = data.get("dedicated_account") or {}
            bank = acct.get("bank") or {}
            va.account_number = acct.get("account_number")
            va.account_name = acct.get("account_name")
            va.bank_name = bank.get("name")
            va.bank_slug = bank.get("slug")
            va.dedicated_account_id = str(acct.get("id") or "") or None
            va.status = VirtualAccountStatus.active
            va.failure_reason = None
            notify_event = NotificationEvent.dva_ready
            notify_ctx = build_dva_context(
                status="active",
                account_number=va.account_number,
                bank_name=va.bank_name,
            )
        else:  # dedicatedaccount.assign.failed
            va.status = VirtualAccountStatus.failed
            va.failure_reason = data.get("reason") or "Account assignment failed"
            notify_event = NotificationEvent.dva_failed
            notify_ctx = build_dva_context(status="failed", reason=va.failure_reason)

        we.processed = True
        db.commit()

        if notify_event is not None:
            from app.db.models.user import User  # local import, mirrors existing style
            user = db.query(User).filter(User.id == va.user_id).first()
            if user is not None:
                dispatch_delay(
                    user_id=str(va.user_id),
                    user_email=user.email,
                    event=notify_event,
                    context=notify_ctx,
                )
        return success({"ok": True})

    # ── DVA inbound transfer funding (resolved by receiver account) ──────
    # A dedicated_nuban charge.success carries Paystack's own reference, not
    # one we minted, so it is handled BEFORE the reference-mandatory check.
    # There is no pre-existing Payment row for a DVA inflow; the WebhookEvent
    # unique insert (flushed above) is the sole idempotency guard — a replayed
    # data.id short-circuits at the dedupe block and never re-credits.
    authorization = data.get("authorization") or {}
    if event_type == "charge.success" and (
        data.get("channel") == "dedicated_nuban"
        or authorization.get("channel") == "dedicated_nuban"
    ):
        acct = authorization.get("receiver_bank_account_number")
        va = (
            db.query(VirtualAccount)
            .filter(VirtualAccount.account_number == acct)
            .first()
        )
        if va is None:
            log.warning(
                "paystack webhook: dedicated_nuban transfer to unknown account=%s event=%s",
                acct, event_id,
            )
            we.processed = True
            db.commit()
            return success({"status": "unknown_account"})

        # kobo -> naira, GROSS (Timpbills absorbs the DVA fee).
        amount = Decimal(data.get("amount", 0)) / Decimal(100)

        tx_svc = TransactionService(db=db)
        tx = tx_svc.create(
            user_id=va.user_id,
            type=TransactionType.wallet_funding,
            amount=amount,
            meta={
                "funding_channel": "dedicated_nuban",
                "paystack_event_id": event_id,
                "sender_name": authorization.get("sender_name"),
                "sender_bank": authorization.get("sender_bank"),
                "sender_account_masked": authorization.get("sender_bank_account_number"),
                "paystack_fee": data.get("fees"),
            },
        )
        # LOCK policy: landed money is never rejected. Over-cap credits in full
        # and locks outbound spend until the next KYC upgrade covers it.
        # idempotency_key=tx.reference records a uniquely-constrained marker in
        # the same commit as the balance change, so the reconciliation sweep
        # (reconcile_dva_funding) can safely re-drive a tx left pending by a
        # crash without ever double-crediting. See must-fix #2.
        new_balance = wallet_svc.credit(
            user_id=va.user_id, amount=amount, over_cap=OverCapPolicy.LOCK,
            idempotency_key=tx.reference,
        )
        tx_svc.transition(
            tx,
            to_status=TransactionStatus.success,
            reason="paystack.webhook.dedicated_nuban",
            context={"paystack_event_id": event_id},
        )

        we.processed = True
        db.commit()

        from app.db.models.user import User
        user = db.query(User).filter(User.id == va.user_id).first()
        if user is not None:
            dispatch_delay(
                user_id=str(va.user_id),
                user_email=user.email,
                event=NotificationEvent.wallet_funded,
                context=build_wallet_funded_context(
                    amount=amount,
                    balance=new_balance,
                    reference=tx.reference,
                    channel="transfer",
                ),
            )
        return success({"ok": True})

    if not reference:
        raise HTTPException(status_code=400, detail={
            "code": "MALFORMED_WEBHOOK", "message": "Missing data.reference"
        })

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
            # Record Paystack's real fee for this funding (kobo) so finance
            # can report Timpbills' absorbed card cost. The user is not
            # charged this fee (card-fee absorption 2026-09-15); we credit
            # tx.amount in full. Mirrors the DVA path's context.
            tx_svc.transition(
                tx,
                to_status=TransactionStatus.success,
                reason="paystack.webhook.charge.success",
                context={"paystack_event_id": event_id, "paystack_fee": data.get("fees")},
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
