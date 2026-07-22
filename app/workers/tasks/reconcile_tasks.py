"""Reconcile pending payments by polling Paystack verify.

Runs every 2 minutes via Celery beat. Catches transactions where the
webhook didn't arrive (network blip, Paystack outage). Races safely
against the webhook handler: before mutating a Payment row, both paths
re-fetch with SELECT FOR UPDATE and no-op if status is no longer pending.

Sprint 3 B12: a companion `reconcile_pending_bills` task uses VTPass
requery() to finalize bill txs where the vtpass webhook went missing.
Same 2-minute cadence, same S2C-8 domain-exception taxonomy (defer on
KycCapExceeded / InsufficientBalance, log-and-continue on transient
provider errors, refund on permanent failure via apply_provider_result).
"""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import ColumnElement, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import log
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.session import SessionLocal
from app.integrations.paystack.factory import select_paystack_client
from app.integrations.vtpass.base import (
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.factory import select_vtpass_client
from app.services.bill_service import REFUNDABLE_ON_FAILURE, BillService
from app.services.transaction_service import TransactionService
from app.services.wallet_service import (
    InsufficientBalance,
    KycCapExceeded,
    OverCapPolicy,
    WalletService,
)
from app.workers.celery_app import celery_app

# Tx states that are already final — the webhook handler beat us to it,
# or ops manually resolved. Skip them rather than InvalidStateTransition.
_TX_FINAL_STATES = {
    TransactionStatus.success,
    TransactionStatus.failed,
    TransactionStatus.refund_pending,
    TransactionStatus.refunded,
    TransactionStatus.refund_failed,
}


# Lifted to app.services.bill_service (S3C-M10) — imported above.
# Local alias keeps existing call sites terse without a rename.
_REFUNDABLE_ON_FAILURE = REFUNDABLE_ON_FAILURE


@celery_app.task(name="app.workers.tasks.reconcile_tasks.reconcile_pending_payments")
def reconcile_pending_payments() -> dict:
    """Query payments in PENDING for >30s and verify with Paystack."""
    return asyncio.run(_reconcile())


async def _reconcile() -> dict:
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=30)
        # Anything still pending older than this is an abandoned checkout the
        # user never completed: stop polling Paystack and close it out.
        abandon_cutoff = now - timedelta(hours=settings.PAYMENT_ABANDON_AFTER_HOURS)
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
            return {"checked": 0, "settled": 0, "deferred": 0, "abandoned": 0}

        client = select_paystack_client()
        wallet_svc = WalletService(db=db)
        tx_svc = TransactionService(db=db)
        settled = 0
        deferred = 0
        abandoned = 0
        for payment, tx in pending:
            # Over-age abandon sweep: a still-pending payment past the abandon
            # horizon is a checkout the user never finished. Close it out
            # terminally WITHOUT calling Paystack verify — the whole point is
            # to stop the unbounded re-poll. Same _claim_payment race guard +
            # tx_svc.transition pattern as the verify-driven branches below.
            # created_at is a DateTime(timezone=True) column; normalise to
            # UTC-aware defensively since some drivers (e.g. SQLite) hand back
            # naive datetimes, which can't be compared to abandon_cutoff.
            created_at = payment.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at < abandon_cutoff:
                if _claim_payment(db, payment.id, PaymentStatus.failed):
                    tx_svc.transition(
                        tx, to_status=TransactionStatus.failed,
                        reason="reconcile.abandoned",
                    )
                    # Refund only outbound tx types, reusing the same
                    # idempotency guard as the verify.failed branch so a
                    # webhook that already refunded isn't double-credited.
                    if tx.type in _REFUNDABLE_ON_FAILURE:
                        refund, was_created = tx_svc.create_refund(
                            original_tx=tx,
                            amount=tx.amount,
                            reason="reconcile.abandoned",
                        )
                        if was_created:
                            wallet_svc.credit(
                                user_id=tx.user_id, amount=refund.amount,
                            )
                    abandoned += 1
                continue
            try:
                v = await client.verify(reference=payment.provider_reference)
            except (httpx.HTTPError, TimeoutError) as exc:
                # Transient network issues — retry on the next tick.
                # (S3C-H2) Previously this was bare `except Exception`,
                # which swallowed programming errors (AttributeError
                # from a bad Paystack SDK upgrade, KeyError from a
                # response-shape change, 401 from a rotated key) as if
                # they were transient blips. Those deserve to propagate
                # so Sentry + Celery surface them.
                log.warning(
                    "reconcile: verify transient failure for %s: %s",
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
                        refund, was_created = tx_svc.create_refund(
                            original_tx=tx,
                            amount=tx.amount,
                            reason="reconcile.verify.failed",
                        )
                        # Idempotency guard — if the webhook already
                        # refunded this tx, was_created is False and we
                        # must NOT credit again. See S3C-P1.
                        if was_created:
                            wallet_svc.credit(
                                user_id=tx.user_id, amount=refund.amount,
                            )
                    settled += 1
            # verify=="abandoned" but still within the abandon horizon →
            # leave pending; the over-age sweep above will close it out once
            # it crosses PAYMENT_ABANDON_AFTER_HOURS.
        db.commit()
        return {
            "checked": len(pending), "settled": settled,
            "deferred": deferred, "abandoned": abandoned,
        }
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


# ─── Bill reconciliation (Sprint 3 B12) ──────────────────────────────────

# Bill types the reconciler handles. Same membership as REFUNDABLE_ON_FAILURE
# by design; kept as a tuple here for use in `.in_(...)` filters.
_BILL_TX_TYPES = tuple(REFUNDABLE_ON_FAILURE)

# How many consecutive permanent-requery failures before we stop
# retrying and transition the tx to failed with `needs_ops_review`.
# Five = ~10 minutes of retries at the 2-min cadence; enough for a
# transient backend config blip to self-heal, short enough that a
# truly broken tx doesn't loop forever. See S3C-P3.
_MAX_REQUERY_PERMANENT_ATTEMPTS = 5


@celery_app.task(name="app.workers.tasks.reconcile_tasks.reconcile_pending_bills")
def reconcile_pending_bills() -> dict:
    """Query bill txs in pending/processing >30s, requery VTPass, apply."""
    return asyncio.run(_reconcile_bills())


async def _reconcile_bills() -> dict:
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=30)
        pending_bills = (
            db.query(Transaction)
            .filter(
                Transaction.type.in_(_BILL_TX_TYPES),
                Transaction.status.in_([
                    TransactionStatus.pending, TransactionStatus.processing,
                ]),
                Transaction.created_at < cutoff,
            )
            .limit(50)
            .all()
        )
        if not pending_bills:
            return {
                "checked": 0, "settled": 0, "deferred": 0,
                "skipped": 0, "escalated": 0,
            }

        provider = select_vtpass_client()
        tx_svc = TransactionService(db=db)
        wallet_svc = WalletService(db=db)
        # Reconcile worker only calls `apply_provider_result` on bill_svc —
        # the redis client is unused here, but BillService requires one
        # since B4's validate_meter path shares the instance. Pull the
        # same singleton the API layer uses so we don't fan out Redis
        # connections unnecessarily.
        from app.api.deps import get_redis
        bill_svc = BillService(
            db=db, tx_svc=tx_svc, wallet_svc=wallet_svc, provider=provider,
            redis=get_redis(),
        )

        settled = 0
        deferred = 0
        skipped = 0
        escalated = 0

        for tx in pending_bills:
            # 1. Ask VTPass what happened to this request_id.
            try:
                result = await provider.requery(request_id=tx.reference)
            except ProviderTemporaryFailure as exc:
                # Network / 5xx / timeout — retry on the next tick.
                log.warning(
                    "reconcile_bills: transient for %s: %s", tx.reference, exc,
                )
                continue
            except ProviderPermanentFailure as exc:
                # VTPass rejected the requery itself (4xx). We don't know the
                # actual bill outcome and cannot auto-refund — a permanent
                # requery rejection != a permanent bill failure. But we
                # also can't spin on this row forever: track attempts in
                # tx.meta; after N consecutive attempts, transition to
                # failed with reason=needs_ops_review so ops sees a
                # concrete handle in the tx list instead of having to
                # grep logs. See S3C-P3.
                attempts = int((tx.meta or {}).get("requery_permanent_attempts", 0)) + 1
                tx.meta = {**(tx.meta or {}), "requery_permanent_attempts": attempts}
                db.flush()
                log.error(
                    "reconcile_bills: permanent requery error for %s "
                    "(attempt %d/%d): %s",
                    tx.reference, attempts, _MAX_REQUERY_PERMANENT_ATTEMPTS, exc,
                )
                if attempts >= _MAX_REQUERY_PERMANENT_ATTEMPTS:
                    # Escalate: transition to failed with a distinct
                    # reason so the tx drops out of the pending/processing
                    # sweep. We do NOT refund — the bill may have
                    # actually delivered upstream; ops decides.
                    locked = (
                        db.query(Transaction)
                        .filter(Transaction.id == tx.id)
                        .with_for_update()
                        .one()
                    )
                    if locked.status not in _TX_FINAL_STATES:
                        tx_svc.transition(
                            locked,
                            to_status=TransactionStatus.failed,
                            reason="needs_ops_review",
                            context={
                                "last_requery_error": str(exc),
                                "attempts": attempts,
                            },
                        )
                        escalated += 1
                continue
            except Exception as exc:
                # Never let a single bad row kill the batch.
                log.warning(
                    "reconcile_bills: unexpected error for %s: %s",
                    tx.reference, exc,
                )
                continue

            # 2. Lock the tx row. If the vtpass webhook already landed,
            #    the tx will be in a terminal state here — skip.
            locked = (
                db.query(Transaction)
                .filter(Transaction.id == tx.id)
                .with_for_update()
                .one()
            )
            if locked.status in _TX_FINAL_STATES:
                skipped += 1
                continue

            # 3. Apply via the shared BillService path. Domain exceptions
            #    (S2C-8 taxonomy): defer rather than leave partial state.
            try:
                bill_svc.apply_provider_result(
                    tx=locked, amount=locked.amount, result=result,
                )
                settled += 1
            except (KycCapExceeded, InsufficientBalance) as exc:
                db.rollback()
                deferred += 1
                log.warning(
                    "reconcile_bills: DEFERRED — ref=%s user=%s err=%s",
                    locked.reference, locked.user_id, exc,
                )
                continue

        db.commit()
        return {
            "checked":   len(pending_bills),
            "settled":   settled,
            "deferred":  deferred,
            "skipped":   skipped,
            "escalated": escalated,
        }
    finally:
        db.close()


# ─── DVA inbound-funding reconciliation (backend must-fix #2) ─────────────
#
# The DVA funding webhook (webhooks.py, dedicated_nuban charge.success) is NOT
# atomic: it commits the WebhookEvent dedup row + a pending wallet_funding tx
# BEFORE crediting the wallet. A crash between those steps leaves the tx stuck
# `pending` with the money uncredited; Paystack's redelivery then dedups on
# provider_event_id and drops the credit forever. This sweep recovers those.
#
# NO DOUBLE-CREDIT is the invariant. Two crash sub-cases both leave the tx
# `pending`, and the sweep can't tell them apart by status alone:
#   (A) crash before the credit         -> wallet NOT credited -> MUST credit.
#   (B) crash after the credit, before  -> wallet ALREADY credited, tx still
#       tx->success                        pending -> MUST NOT credit again.
# We therefore make the credit itself idempotent: WalletService.credit is
# called with idempotency_key=tx.reference, which records a uniquely-
# constrained wallet_credit_keys marker in the SAME commit as the balance
# change. The live webhook path passes the same key, so in sub-case (B) the
# marker already exists, the sweep's credit collides and no-ops, and we only
# transition the tx to success.

# Grace window: a tx younger than this may still be in-flight on the live
# webhook path, so we leave it alone — the sweep must never race live credits.
# The live credit+transition completes in milliseconds; 5 minutes is
# comfortably beyond any realistic latency while still recovering promptly.
_DVA_RECONCILE_GRACE_SECONDS = 300


def _dva_channel_filter(db: Session) -> "ColumnElement[bool]":
    """SQL-level predicate for `meta.funding_channel == "dedicated_nuban"`.

    `Transaction.meta` is `JSON().with_variant(JSONB(), "postgresql")` — plain
    TEXT-backed JSON on SQLite, native JSONB on Postgres. There's no single
    SQLAlchemy expression that compiles correctly on both (the generic JSON
    comparator doesn't expose `.astext`, and `.op("->>")` isn't valid SQLite
    syntax on older SQLite builds), so branch on the bound dialect, same
    pattern as the ON-CONFLICT/IntegrityError dialect split in
    PushTokensService — one portable behavior, two dialect-specific
    implementations.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return cast(Transaction.meta, JSONB)["funding_channel"].astext == "dedicated_nuban"
    # SQLite (test suite) and any other dialect: meta is stored as TEXT,
    # extract via the json1 extension's json_extract().
    return func.json_extract(Transaction.meta, "$.funding_channel") == "dedicated_nuban"


@celery_app.task(name="app.workers.tasks.reconcile_tasks.reconcile_dva_funding")
def reconcile_dva_funding() -> dict:
    """Recover DVA inbound-funding credits dropped by a mid-webhook crash.

    Finds wallet_funding txns stuck `pending` with
    meta.funding_channel == "dedicated_nuban" older than the grace window,
    then credits (idempotently) + transitions each to success. Synchronous —
    no external calls, pure DB — so no asyncio wrapper (unlike the Paystack /
    VTPass reconcilers which await a provider client)."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=_DVA_RECONCILE_GRACE_SECONDS)
        # funding_channel match MUST happen in SQL, before .limit(50) — not in
        # Python after it. Orphaned card-funding pending wallet_funding txns
        # (meta has no funding_channel) can accumulate; if the query pulled
        # 50 rows and only then filtered to dedicated_nuban, a backlog of 50+
        # orphans would fill the whole window and shadow a genuinely stuck
        # DVA credit below it — starving recovery indefinitely. See the
        # Important review fix covered by
        # test_orphaned_non_dva_backlog_does_not_starve_genuine_recovery.
        stuck = (
            db.query(Transaction)
            .filter(
                Transaction.type == TransactionType.wallet_funding,
                Transaction.status == TransactionStatus.pending,
                Transaction.created_at < cutoff,
                _dva_channel_filter(db),
            )
            .limit(50)
            .all()
        )
        if not stuck:
            return {"checked": 0, "recovered": 0, "skipped": 0}

        wallet_svc = WalletService(db=db)
        tx_svc = TransactionService(db=db)
        recovered = 0
        skipped = 0

        for tx in stuck:
            # Re-lock the tx. If the live webhook (or a prior sweep tick) beat
            # us to it, it's no longer pending — skip rather than double-handle.
            locked = (
                db.query(Transaction)
                .filter(Transaction.id == tx.id)
                .with_for_update()
                .one()
            )
            if locked.status != TransactionStatus.pending:
                skipped += 1
                continue

            # Idempotent credit keyed on the tx reference. Sub-case (A) credits
            # once; sub-case (B) collides on the marker and no-ops. LOCK policy:
            # landed money is never rejected — over-cap credits in full and
            # spend-locks, exactly like the live DVA path.
            try:
                wallet_svc.credit(
                    user_id=locked.user_id,
                    amount=locked.amount,
                    over_cap=OverCapPolicy.LOCK,
                    idempotency_key=locked.reference,
                )
            except Exception as exc:
                # Never let one bad row kill the batch. Discard partial state
                # and move on; the next tick retries.
                db.rollback()
                log.warning(
                    "reconcile_dva: credit failed ref=%s user=%s err=%s",
                    locked.reference, locked.user_id, exc,
                )
                continue

            tx_svc.transition(
                locked,
                to_status=TransactionStatus.success,
                reason="reconcile.dva.recovered",
            )
            recovered += 1

        db.commit()
        return {"checked": len(stuck), "recovered": recovered, "skipped": skipped}
    finally:
        db.close()
