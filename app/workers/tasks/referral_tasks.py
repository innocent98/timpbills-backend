"""Nightly referral sweeper.

Sprint 5b B2. Re-evaluates three classes of deferred referral rows:

* ``pending`` rows older than 1 day where a qualifying tx is attached:
  re-run ``attempt_credit``. Captures the daily-cap-deferred case where
  yesterday's last referral spilled into today's quota.
* ``referee_cap_pending`` rows: try to credit the referee's wallet again.
  Useful after the referee upgrades their KYC tier.
* ``clawback_pending`` rows whose 7-day refund window has settled: if
  the qualifying tx is still ``refunded``, debit both wallets and move
  to ``clawed_back``; otherwise the refund was reversed → revert to
  ``credited``.

Runs once a day via Celery beat. The cadence is intentionally coarse —
each bucket above is naturally a "tomorrow" problem (daily-cap reset, KYC
upgrade, refund-window settle).

Per-row failures don't abort the sweep; they're logged and the next row
proceeds. The pipeline already commits per row, so partial progress is
durable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.logger import log
from app.db.models._enums import TransactionStatus
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.transaction import Transaction
from app.db.session import SessionLocal
from app.services.app_setting_service import AppSettingService
from app.services.referral_service import ReferralCreditOutcome, ReferralService
from app.services.wallet_service import (
    InsufficientBalance,
    KycCapExceeded,
    WalletService,
)
from app.workers.celery_app import celery_app

# Minimum age before a pending row is a sweep candidate. The credit
# pipeline already runs at transaction-success time; we only sweep rows
# that have had at least a fresh UTC-day boundary cross since signup so
# the daily-cap counter resets.
_PENDING_MIN_AGE_DAYS = 1


@celery_app.task(name="app.workers.tasks.referral_tasks.sweep_referrals")
def sweep_referrals() -> dict[str, int]:
    """Celery entrypoint. Opens its own DB session and delegates to the
    pure ``sweep_referrals_impl`` so tests can drive the logic directly
    on a test session."""
    db = SessionLocal()
    try:
        return sweep_referrals_impl(db=db)
    finally:
        db.close()


def sweep_referrals_impl(*, db: Session) -> dict[str, int]:
    """Run one sweep pass against the given session. Returns a counter
    dict so the Celery task and tests can both observe what happened."""
    report: dict[str, int] = {
        "pending_swept":          0,
        "credited":               0,
        "voided":                 0,
        "still_pending":          0,
        "referee_retry_credited": 0,
        "referee_retry_capped":   0,
        "clawed_back":            0,
        "clawback_reverted":      0,
        "clawback_deferred":      0,
    }

    settings_svc = AppSettingService(db=db, ttl_seconds=0)
    wallet_svc = WalletService(db=db)
    referral_svc = ReferralService(
        db=db, wallet_svc=wallet_svc, settings_svc=settings_svc, push=None,
    )

    _sweep_pending(db, referral_svc, report)
    _sweep_referee_cap_pending(db, wallet_svc, settings_svc, report)
    _sweep_clawback_pending(db, wallet_svc, settings_svc, report)

    return report


# ── Pending bucket ──────────────────────────────────────────────────────


def _sweep_pending(
    db: Session,
    svc: ReferralService,
    report: dict[str, int],
) -> None:
    """Re-run attempt_credit on pending rows that already have a qualifying
    tx attached and are at least _PENDING_MIN_AGE_DAYS old. A row without
    a qualifying_tx_id never met the threshold gate — no retry possible."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_PENDING_MIN_AGE_DAYS)
    rows = (
        db.query(Referral)
        .filter(
            Referral.status == ReferralStatus.pending,
            Referral.qualifying_tx_id.isnot(None),
            Referral.created_at < cutoff,
        )
        .limit(500)
        .all()
    )
    for r in rows:
        report["pending_swept"] += 1
        try:
            result = svc.attempt_credit(
                referee_user_id=r.referee_user_id,
                qualifying_tx_id=r.qualifying_tx_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "referral.sweep: pending row %s failed: %s", r.id, exc,
            )
            continue
        if result.outcome is ReferralCreditOutcome.credited:
            report["credited"] += 1
        elif result.outcome in (
            ReferralCreditOutcome.voided_lifetime_cap,
            ReferralCreditOutcome.voided_referrer_cap,
            ReferralCreditOutcome.voided_referrer_inactive,
            ReferralCreditOutcome.voided_self_referral_bank,
        ):
            report["voided"] += 1
        else:
            report["still_pending"] += 1


# ── Referee-cap-pending bucket ─────────────────────────────────────────


def _sweep_referee_cap_pending(
    db: Session,
    wallet_svc: WalletService,
    settings_svc: AppSettingService,
    report: dict[str, int],
) -> None:
    """Retry the referee credit for rows blocked by their KYC cap.
    Referrer was already credited at the original attempt — that step
    isn't repeated."""
    rows = (
        db.query(Referral)
        .filter(Referral.status == ReferralStatus.referee_cap_pending)
        .limit(500)
        .all()
    )
    if not rows:
        return
    reward = settings_svc.get_decimal("REFERRAL_REWARD_REFEREE_NAIRA")
    for r in rows:
        try:
            wallet_svc.credit(user_id=r.referee_user_id, amount=reward)
        except KycCapExceeded:
            report["referee_retry_capped"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "referral.sweep: referee retry for %s failed: %s", r.id, exc,
            )
            continue
        # Lock + advance the row state.
        locked = (
            db.query(Referral)
            .filter(Referral.id == r.id)
            .with_for_update()
            .first()
        )
        if locked is None or locked.status is not ReferralStatus.referee_cap_pending:
            # Someone else moved it between the credit and the lock —
            # bail rather than overwrite a more recent transition.
            continue
        locked.status = ReferralStatus.credited
        locked.credited_at = datetime.now(timezone.utc)
        db.commit()
        report["referee_retry_credited"] += 1


# ── Clawback-pending bucket ────────────────────────────────────────────


def _sweep_clawback_pending(
    db: Session,
    wallet_svc: WalletService,
    settings_svc: AppSettingService,
    report: dict[str, int],
) -> None:
    """Settle clawback_pending rows whose refund window has passed. If
    the qualifying tx is still refunded → debit both wallets and move
    to clawed_back. Otherwise the refund was reversed → restore to
    credited."""
    window_days = settings_svc.get_int("REFERRAL_CLAWBACK_WINDOW_DAYS", default=7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    rows = (
        db.query(Referral)
        .filter(
            Referral.status == ReferralStatus.clawback_pending,
            Referral.created_at < cutoff,
        )
        .limit(500)
        .all()
    )
    if not rows:
        return
    referrer_reward = settings_svc.get_decimal("REFERRAL_REWARD_REFERRER_NAIRA")
    referee_reward = settings_svc.get_decimal("REFERRAL_REWARD_REFEREE_NAIRA")
    for r in rows:
        if r.qualifying_tx_id is None:
            # Defensive — clawback_pending without a tx is malformed.
            log.warning(
                "referral.sweep: clawback_pending row %s has no qualifying_tx", r.id,
            )
            report["clawback_deferred"] += 1
            continue

        tx = db.query(Transaction).filter(Transaction.id == r.qualifying_tx_id).first()
        if tx is None or tx.status is not TransactionStatus.refunded:
            # Refund was reversed (or tx vanished) — restore to credited.
            locked = (
                db.query(Referral)
                .filter(Referral.id == r.id)
                .with_for_update()
                .first()
            )
            if locked is not None and locked.status is ReferralStatus.clawback_pending:
                locked.status = ReferralStatus.credited
                db.commit()
                report["clawback_reverted"] += 1
            continue

        # Refund stands → debit both wallets. Insufficient balance on
        # either side defers the row (leave in clawback_pending) — we
        # do not create negative balances; the wallet table CHECK
        # constraint forbids it.
        try:
            wallet_svc.debit(user_id=r.referrer_user_id, amount=referrer_reward)
        except InsufficientBalance:
            log.warning(
                "referral.sweep: clawback referrer %s short on funds — deferring %s",
                r.referrer_user_id, r.id,
            )
            report["clawback_deferred"] += 1
            continue
        try:
            wallet_svc.debit(user_id=r.referee_user_id, amount=referee_reward)
        except InsufficientBalance:
            # Referrer was already debited above; re-credit them so we
            # leave the row in a clean clawback_pending state for retry.
            wallet_svc.credit(user_id=r.referrer_user_id, amount=referrer_reward)
            log.warning(
                "referral.sweep: clawback referee %s short on funds — deferring %s",
                r.referee_user_id, r.id,
            )
            report["clawback_deferred"] += 1
            continue

        locked = (
            db.query(Referral)
            .filter(Referral.id == r.id)
            .with_for_update()
            .first()
        )
        if locked is None or locked.status is not ReferralStatus.clawback_pending:
            continue
        locked.status = ReferralStatus.clawed_back
        locked.clawed_back_at = datetime.now(timezone.utc)
        db.commit()
        report["clawed_back"] += 1
