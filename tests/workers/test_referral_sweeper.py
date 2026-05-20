"""Nightly sweeper — retries deferred referral rows.

Sprint 5b B2. Three buckets:

* ``pending`` rows older than 1 day where conditions now permit → run
  attempt_credit and let it credit / void / re-defer.
* ``referee_cap_pending`` rows → retry crediting referee's wallet now
  that their KYC may have been upgraded.
* ``clawback_pending`` rows whose 7-day refund window has settled →
  if the qualifying tx is still ``refunded``, debit both wallets and
  move to ``clawed_back``; otherwise revert to ``credited``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
from decimal import Decimal
from uuid import uuid4


from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.app_setting import AppSetting
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.transaction import Transaction
from app.db.models.user import KycLevel, User
from app.services.wallet_service import WalletService
from app.workers.tasks.referral_tasks import sweep_referrals_impl


_DEFAULT_SETTINGS = {
    "REFERRAL_ENABLED": "true",
    "REFERRAL_REWARD_REFERRER_NAIRA": "100",
    "REFERRAL_REWARD_REFEREE_NAIRA": "50",
    "REFERRAL_DAILY_CAP": "5",
    "REFERRAL_LIFETIME_CAP_NAIRA": "50000",
    "REFERRAL_MIN_TX_AMOUNT_NAIRA": "1000",
    "REFERRAL_CLAWBACK_WINDOW_DAYS": "7",
}


def _seed_settings(db, **overrides) -> None:
    merged = {**_DEFAULT_SETTINGS, **overrides}
    for key, value in merged.items():
        db.add(AppSetting(key=key, value=str(value)))
    db.commit()


def _seed_user(db, *, kyc: KycLevel = KycLevel.tier_0, is_active: bool = True) -> User:
    u = User(
        email=f"{uuid4().hex[:10]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
        is_active=is_active,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _seed_tx(db, user: User, *, status=TransactionStatus.success) -> Transaction:
    tx = Transaction(
        user_id=user.id,
        reference=f"tx-{uuid4().hex[:12]}",
        type=TransactionType.airtime,
        status=status,
        amount=Decimal("1000"),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def _backdate_referral(db, referral: Referral, days: int) -> None:
    """Push created_at into the past so the sweeper considers the row."""
    old = datetime.now(UTC) - timedelta(days=days)
    referral.created_at = old
    if referral.credited_at is not None:
        referral.credited_at = old
    db.commit()


# ── pending older than 1 day → re-run attempt_credit ────────────────────


def test_sweeper_re_credits_pending_row_when_conditions_now_allow(db_session):
    _seed_settings(db_session, REFERRAL_DAILY_CAP="1")
    referrer = _seed_user(db_session)
    # Burn the daily cap so the second referee defers
    referee_a = _seed_user(db_session)
    referral_a = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee_a.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.credited,
        credited_at=datetime.now(UTC) - timedelta(days=2),  # yesterday
        qualifying_tx_id=_seed_tx(db_session, referee_a).id,
    )
    db_session.add(referral_a)
    db_session.commit()

    # New referee with a pending row from 2 days ago + a successful tx
    referee_b = _seed_user(db_session)
    tx_b = _seed_tx(db_session, referee_b)
    referral_b = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee_b.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.pending,
        qualifying_tx_id=tx_b.id,
    )
    db_session.add(referral_b)
    db_session.commit()
    _backdate_referral(db_session, referral_b, days=2)

    w = WalletService(db=db_session)
    w.get_or_create(user_id=referrer.id)
    w.get_or_create(user_id=referee_b.id)

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral_b)
    # Today is a fresh UTC day → cap reset → attempt_credit credits
    assert referral_b.status is ReferralStatus.credited
    assert w.balance(user_id=referrer.id) == Decimal("100")
    assert w.balance(user_id=referee_b.id) == Decimal("50")
    assert report["pending_swept"] == 1
    assert report["credited"] == 1


def test_sweeper_skips_pending_row_without_qualifying_tx(db_session):
    """A pending row that never had a qualifying tx attached (e.g. user
    signed up with the code but never made a paid bill purchase) must
    stay pending after the sweep — no synthetic tx, no credit."""
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    referee = _seed_user(db_session)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.pending,
        qualifying_tx_id=None,
    )
    db_session.add(referral)
    db_session.commit()
    _backdate_referral(db_session, referral, days=2)

    WalletService(db=db_session).get_or_create(user_id=referrer.id)
    WalletService(db=db_session).get_or_create(user_id=referee.id)

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.pending
    assert report["pending_swept"] == 0


def test_sweeper_ignores_pending_younger_than_one_day(db_session):
    """Rows from earlier today aren't candidates — the sweeper only picks
    up pending rows older than 1 day. (A fresh tx that's just below
    threshold isn't going to magically be eligible an hour later.)"""
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    referee = _seed_user(db_session)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.pending,
        qualifying_tx_id=_seed_tx(db_session, referee).id,
    )
    db_session.add(referral)
    db_session.commit()
    # Don't backdate — row is fresh

    WalletService(db=db_session).get_or_create(user_id=referrer.id)
    WalletService(db=db_session).get_or_create(user_id=referee.id)

    report = sweep_referrals_impl(db=db_session)
    db_session.refresh(referral)
    assert referral.status is ReferralStatus.pending
    assert report["pending_swept"] == 0


# ── referee_cap_pending → retry referee credit ──────────────────────────


def test_sweeper_credits_referee_cap_pending_after_kyc_upgrade(db_session):
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    # Referee was tier_0 (50k cap) and capped at credit time; now upgraded
    referee = _seed_user(db_session, kyc=KycLevel.tier_2)
    tx = _seed_tx(db_session, referee)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.referee_cap_pending,
        attributed_at=datetime.now(UTC) - timedelta(days=1),
        qualifying_tx_id=tx.id,
    )
    db_session.add(referral)
    db_session.commit()

    w = WalletService(db=db_session)
    w.get_or_create(user_id=referrer.id)
    w.get_or_create(user_id=referee.id)
    # Referrer was credited at the original attempt — simulate that.
    w.credit(user_id=referrer.id, amount=Decimal("100"))

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.credited
    assert w.balance(user_id=referee.id) == Decimal("50")
    assert report["referee_retry_credited"] == 1


def test_sweeper_leaves_referee_cap_pending_when_still_capped(db_session):
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    referee = _seed_user(db_session, kyc=KycLevel.tier_0)
    tx = _seed_tx(db_session, referee)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.referee_cap_pending,
        attributed_at=datetime.now(UTC) - timedelta(days=1),
        qualifying_tx_id=tx.id,
    )
    db_session.add(referral)
    db_session.commit()

    w = WalletService(db=db_session)
    w.get_or_create(user_id=referrer.id)
    w.get_or_create(user_id=referee.id)
    # Park referee 1 naira below cap so +50 reward overflows
    w.credit(user_id=referee.id, amount=Decimal("49999"))

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.referee_cap_pending
    assert report["referee_retry_credited"] == 0


# ── clawback_pending → settle after 7-day window ───────────────────────


def test_sweeper_commits_clawback_after_7d_when_tx_still_refunded(db_session):
    _seed_settings(db_session)
    referrer = _seed_user(db_session, kyc=KycLevel.tier_2)
    referee = _seed_user(db_session, kyc=KycLevel.tier_2)
    tx = _seed_tx(db_session, referee, status=TransactionStatus.refunded)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.clawback_pending,
        attributed_at=datetime.now(UTC) - timedelta(days=8),
        credited_at=datetime.now(UTC) - timedelta(days=8),
        qualifying_tx_id=tx.id,
    )
    db_session.add(referral)
    db_session.commit()
    _backdate_referral(db_session, referral, days=8)

    w = WalletService(db=db_session)
    w.get_or_create(user_id=referrer.id)
    w.get_or_create(user_id=referee.id)
    # Both wallets hold their reward
    w.credit(user_id=referrer.id, amount=Decimal("100"))
    w.credit(user_id=referee.id, amount=Decimal("50"))

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.clawed_back
    assert referral.clawed_back_at is not None
    assert w.balance(user_id=referrer.id) == Decimal("0")
    assert w.balance(user_id=referee.id) == Decimal("0")
    assert report["clawed_back"] == 1


def test_sweeper_reverts_clawback_pending_when_tx_no_longer_refunded(db_session):
    """If the refund was reversed within the 7-day window (tx flipped back
    to success), the sweeper restores the referral to credited."""
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    referee = _seed_user(db_session)
    tx = _seed_tx(db_session, referee, status=TransactionStatus.success)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.clawback_pending,
        attributed_at=datetime.now(UTC) - timedelta(days=8),
        credited_at=datetime.now(UTC) - timedelta(days=8),
        qualifying_tx_id=tx.id,
    )
    db_session.add(referral)
    db_session.commit()
    _backdate_referral(db_session, referral, days=8)

    WalletService(db=db_session).get_or_create(user_id=referrer.id)
    WalletService(db=db_session).get_or_create(user_id=referee.id)

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.credited
    assert report["clawback_reverted"] == 1


def test_sweeper_ignores_clawback_pending_inside_window(db_session):
    """Rows in clawback_pending less than 7 days old are not settled yet."""
    _seed_settings(db_session)
    referrer = _seed_user(db_session)
    referee = _seed_user(db_session)
    tx = _seed_tx(db_session, referee, status=TransactionStatus.refunded)
    referral = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=ReferralStatus.clawback_pending,
        attributed_at=datetime.now(UTC) - timedelta(days=2),
        credited_at=datetime.now(UTC) - timedelta(days=2),
        qualifying_tx_id=tx.id,
    )
    db_session.add(referral)
    db_session.commit()
    _backdate_referral(db_session, referral, days=2)

    WalletService(db=db_session).get_or_create(user_id=referrer.id)
    WalletService(db=db_session).get_or_create(user_id=referee.id)

    report = sweep_referrals_impl(db=db_session)

    db_session.refresh(referral)
    assert referral.status is ReferralStatus.clawback_pending
    assert report["clawed_back"] == 0
    assert report["clawback_reverted"] == 0
