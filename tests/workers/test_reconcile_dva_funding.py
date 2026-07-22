"""Reconciliation sweep for dropped DVA inbound-funding credits.

Backend must-fix #2. The DVA funding webhook is not atomic: the WebhookEvent
dedup row + a pending wallet_funding tx are committed BEFORE the wallet is
credited. A crash between those steps leaves the tx stuck `pending` and the
money uncredited, while Paystack redelivery dedups on provider_event_id and
drops the credit forever. This sweep recovers those.

The critical invariant is NO DOUBLE-CREDIT. Two crash sub-cases both leave a
tx `pending`:
  (A) crash before the wallet credit  -> sweep MUST credit exactly once.
  (B) crash after the wallet credit, before tx->success -> sweep MUST NOT
      credit again, only transition.
Idempotency is guaranteed by WalletService.credit(idempotency_key=tx.reference)
recording a uniquely-constrained wallet_credit_keys row in the SAME commit as
the balance change. The live webhook path passes the same key, so sub-case (B)
collides on the marker and no-ops.
"""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.wallet_credit_key import WalletCreditKey


def _seed_user_wallet(db, *, balance="0.00", cap="50000.00"):
    user = User(
        id=uuid.uuid4(),
        email=f"dva-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="DVA Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()
    wallet = Wallet(
        id=uuid.uuid4(),
        user_id=user.id,
        balance=Decimal(balance),
        balance_cap=Decimal(cap),
    )
    db.add(wallet)
    db.flush()
    return user, wallet


def _seed_funding_tx(
    db,
    user,
    *,
    amount="5000.00",
    status=TransactionStatus.pending,
    channel="dedicated_nuban",
    age=timedelta(minutes=10),
):
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-DVA-{uuid.uuid4().hex[:8]}",
        type=TransactionType.wallet_funding,
        status=status,
        amount=Decimal(amount),
        fee=Decimal("0.00"),
        currency="NGN",
        meta={"funding_channel": channel} if channel else {},
    )
    db.add(tx)
    db.flush()
    # Backdate created_at past the grace window unless a fresh tx is wanted.
    tx.created_at = datetime.now(UTC) - age
    db.commit()
    return tx


def _run_sweep(db_session):
    """Run the sweep against the shared test session (mirrors the other
    reconcile tests: patch SessionLocal + neuter close())."""
    from unittest.mock import patch

    from app.workers.tasks import reconcile_tasks as rt

    original_close = db_session.close
    db_session.close = lambda: None
    try:
        with patch.object(rt, "SessionLocal", lambda: db_session):
            return rt.reconcile_dva_funding()
    finally:
        db_session.close = original_close


def test_stuck_pending_uncredited_gets_credited_once(db_session):
    """Sub-case (A): wallet never credited -> sweep credits once + tx success."""
    user, wallet = _seed_user_wallet(db_session, balance="0.00")
    tx = _seed_funding_tx(db_session, user, amount="5000.00")

    result = _run_sweep(db_session)

    assert result["recovered"] == 1
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("5000.00")
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
    # Exactly one idempotency marker recorded.
    markers = (
        db_session.query(WalletCreditKey)
        .filter(WalletCreditKey.key == tx.reference)
        .all()
    )
    assert len(markers) == 1


def test_subcase_b_already_credited_not_double_credited(db_session):
    """Sub-case (B): wallet ALREADY credited + tx still pending. The live
    webhook credited (marker present) then crashed before tx->success. The
    sweep MUST only transition, NOT credit again."""
    user, wallet = _seed_user_wallet(db_session, balance="5000.00")
    tx = _seed_funding_tx(db_session, user, amount="5000.00")
    # Simulate the live path having already credited under this tx reference:
    # the marker row is what proves the credit landed.
    db_session.add(
        WalletCreditKey(
            id=uuid.uuid4(),
            key=tx.reference,
            user_id=user.id,
            amount=Decimal("5000.00"),
        )
    )
    db_session.commit()

    result = _run_sweep(db_session)

    assert result["recovered"] == 1  # transitioned
    db_session.expire_all()
    # Balance UNCHANGED — no second credit.
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("5000.00")
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
    # Still exactly one marker.
    markers = (
        db_session.query(WalletCreditKey)
        .filter(WalletCreditKey.key == tx.reference)
        .all()
    )
    assert len(markers) == 1


def test_fresh_tx_inside_grace_window_ignored(db_session):
    """A tx younger than the grace window is left alone — the live webhook may
    still be finishing it; the sweep must never race the live path."""
    user, wallet = _seed_user_wallet(db_session, balance="0.00")
    tx = _seed_funding_tx(db_session, user, amount="5000.00", age=timedelta(seconds=5))

    result = _run_sweep(db_session)

    assert result["checked"] == 0
    assert result["recovered"] == 0
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("0.00")
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.pending


def test_ignores_non_dva_and_non_pending(db_session):
    """Sweep only touches pending dedicated_nuban funding txns. A card-channel
    funding tx and an already-success DVA tx are both skipped."""
    user, wallet = _seed_user_wallet(db_session, balance="0.00")
    # Non-DVA channel funding tx, stuck pending.
    card_tx = _seed_funding_tx(
        db_session, user, amount="1000.00", channel="card_checkout"
    )
    # DVA tx already success (webhook completed normally).
    done_tx = _seed_funding_tx(
        db_session, user, amount="2000.00", status=TransactionStatus.success
    )

    result = _run_sweep(db_session)

    assert result["checked"] == 0
    assert result["recovered"] == 0
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("0.00")
    assert (
        db_session.query(Transaction).filter(Transaction.id == card_tx.id).one().status
        == TransactionStatus.pending
    )
    assert (
        db_session.query(Transaction).filter(Transaction.id == done_tx.id).one().status
        == TransactionStatus.success
    )


def test_over_cap_stuck_tx_credits_full_and_locks(db_session):
    """Over-cap landed money is never rejected: the sweep credits in full and
    spend-locks the wallet (LOCK policy), same as the live DVA path."""
    # Balance ₦48k, cap ₦50k, funding ₦5k -> ₦53k over cap.
    user, wallet = _seed_user_wallet(db_session, balance="48000.00", cap="50000.00")
    tx = _seed_funding_tx(db_session, user, amount="5000.00")

    result = _run_sweep(db_session)

    assert result["recovered"] == 1
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("53000.00")
    assert w.spend_locked is True
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
