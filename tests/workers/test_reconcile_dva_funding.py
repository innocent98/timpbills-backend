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


def test_orphaned_non_dva_backlog_does_not_starve_genuine_recovery(db_session):
    """Important review fix: the sweep's `limit(50)` must apply AFTER the
    funding_channel match, not before it.

    Orphaned card-funding `pending` wallet_funding txns (meta has no
    `funding_channel`) can pile up over time. If the SQL query pulls 50 rows
    and only THEN filters to dedicated_nuban in Python, a backlog of 50+
    orphans fills the whole window and shadows a genuinely stuck DVA credit
    below it -- the money is never recovered. Seed 51 orphans (aged past the
    grace window, so they're eligible candidates) ahead of one genuine
    dedicated_nuban stuck tx, and assert the DVA tx is still recovered."""
    user, wallet = _seed_user_wallet(db_session, balance="0.00")
    # Orphan backlog: non-DVA pending funding txns with no funding_channel
    # in meta at all -- these must never satisfy the DVA match.
    for _ in range(51):
        _seed_funding_tx(db_session, user, amount="100.00", channel=None)

    dva_tx = _seed_funding_tx(db_session, user, amount="5000.00", channel="dedicated_nuban")

    result = _run_sweep(db_session)

    assert result["recovered"] == 1
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert w.balance == Decimal("5000.00")
    fresh = db_session.query(Transaction).filter(Transaction.id == dva_tx.id).one()
    assert fresh.status == TransactionStatus.success


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


def test_all_digit_uuid_marker_round_trips(db_session):
    """Regression: a uuid4 whose 32-char hex is all digits must survive the
    WalletCreditKey round-trip on SQLite.

    postgresql.UUID rendered as bare "UUID" gets NUMERIC affinity on SQLite,
    which coerces an all-digit hex to a float; the UUID result processor then
    raised "'float' object has no attribute 'replace'" -- a ~1-in-3.4M CI
    flake. The conftest CHAR(32) compile rule fixes it. This forces the poison
    value deterministically so the flake can never return unnoticed.
    """
    poison = uuid.UUID("1234567890" * 3 + "12")  # 32 hex chars, all digits
    assert poison.hex.isdigit()
    user, _ = _seed_user_wallet(db_session)
    db_session.add(
        WalletCreditKey(
            id=poison,
            key="TMP-DVA-ALLDIGIT",
            user_id=poison,
            amount=Decimal("5000.00"),
        )
    )
    db_session.commit()
    db_session.expire_all()

    row = (
        db_session.query(WalletCreditKey)
        .filter(WalletCreditKey.key == "TMP-DVA-ALLDIGIT")
        .one()
    )
    assert row.id == poison
    assert row.user_id == poison
    assert row.amount == Decimal("5000.00")
