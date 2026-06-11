"""API-level tests for /admin/refunds/{reference}/trigger — Sprint 5 BE-52.

Auth migrated (admin sprint, Task 6) to the opaque session-cookie +
double-submit CSRF model. The acting actor is now an ``AdminUser``
(authenticated via ``login_admin``), which is distinct from the regular
``User`` whose wallet the refund credits — so each refund test seeds a
target ``User`` + ``Wallet`` + failed ``Transaction`` and authenticates
as a *separate* admin.

Covers the contract corners:

  1. No session cookie → 401 ``ADMIN_AUTH_REQUIRED``.
  2. Disabled admin row → 403 ``ADMIN_DISABLED``.
  3. Authenticated admin without ``X-CSRF-Token`` → 403 ``CSRF_FAILED``.
  4. Admin can trigger a refund on a failed bill tx → target wallet
     credited, audit row written, original tx walks to ``refunded``.
  5. Re-triggering an already-refunded tx → 200, no double credit,
     ``was_created=false``, audit row still written.
  6. Negative paths: unknown tx → 404, refund-type tx → 400, short
     reason → 422.
"""
import uuid
from decimal import Decimal

import pytest

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.utils.references import new_transaction_reference


def _seed_target_user(db, *, email: str = "target@e.co") -> User:
    """Create the regular ``User`` who OWNS the transaction being refunded.

    This is the wallet-credit target — separate from the ``AdminUser``
    performing the action (authenticated via ``login_admin``).
    """
    user = User(
        email=email,
        phone=f"+23480{uuid.uuid4().int % 10**8:08d}",
        full_name="Refund Target",
        password_hash=hash_password("Secret1!"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_failed_bill_tx(db, user_id, *, amount: Decimal = Decimal("1500.00")) -> Transaction:
    """Insert a `failed` airtime tx + ensure the user has a wallet so a
    refund credit doesn't crash on missing-row. Skips the BillService
    orchestration — the admin endpoint contract is independent of how
    the tx got into the failed state."""
    if db.query(Wallet).filter(Wallet.user_id == user_id).first() is None:
        db.add(Wallet(
            user_id=user_id, balance=Decimal("0.00"),
            balance_cap=Decimal("200000.00"),
        ))
    tx = Transaction(
        user_id=user_id,
        reference=new_transaction_reference(user_id=str(user_id)),
        type=TransactionType.airtime,
        status=TransactionStatus.failed,
        amount=amount,
        fee=Decimal("0.00"),
        meta={"phone": "08011111111", "service_id": "mtn"},
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


# ── 1. Auth contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_unauthenticated_rejects_401(admin_client):
    r = await admin_client.post(
        "/api/v1/admin/refunds/TMP-260428-0001/trigger",
        json={"reason": "Customer reported never received airtime"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_admin_refund_disabled_admin_rejects_403(admin_ctx, login_admin):
    """A seeded-but-disabled admin row holding a valid session is rejected
    with 403 ADMIN_DISABLED — the session resolves, but the admin can no
    longer act."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()  # seeds the ops@x.com admin + session cookies

    admin = db.query(AdminUser).filter_by(email="ops@x.com").one()
    admin.is_active = False
    db.commit()

    # Send a VALID csrf header so the 403 can only come from the disabled
    # check, not a CSRF mismatch — this also asserts auth resolves first.
    r = await client.post(
        "/api/v1/admin/refunds/TMP-260428-0001/trigger",
        json={"reason": "Customer reported never received airtime"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ADMIN_DISABLED"


@pytest.mark.asyncio
async def test_admin_refund_missing_csrf_rejects_403(admin_ctx, login_admin):
    """Authenticated admin (session cookies set) but no X-CSRF-Token header
    → 403 CSRF_FAILED. With the header the request proceeds (asserted in the
    happy-path test below)."""
    client, db, _redis = admin_ctx
    await login_admin()
    target = _seed_target_user(db)
    tx = _seed_failed_bill_tx(db, target.id)

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Customer reported never received airtime"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_FAILED"


# ── 2. Happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_credits_wallet_and_walks_tx_to_refunded(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    target = _seed_target_user(db)
    tx = _seed_failed_bill_tx(db, target.id, amount=Decimal("1500.00"))

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Customer reported never received airtime"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["was_created"] is True
    assert body["refund_reference"]
    assert body["refund_amount"] == "1500.00"
    assert body["transaction_status"] == "refunded"

    # Wallet credited by the refund amount.
    wallet = db.query(Wallet).filter(Wallet.user_id == target.id).one()
    assert wallet.balance == Decimal("1500.00")

    # Refund row exists, linked back to the original via meta.
    refund = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == target.id,
            Transaction.type == TransactionType.refund,
        )
        .one()
    )
    assert refund.meta["original_reference"] == tx.reference
    assert refund.amount == Decimal("1500.00")

    # Audit row on the original tx records the admin actor + reason.
    audit_rows = (
        db.query(TransactionEvent)
        .filter(TransactionEvent.transaction_id == tx.id)
        .all()
    )
    admin_attempts = [
        e for e in audit_rows
        if e.reason and e.reason.startswith("admin_manual_refund_attempt:")
    ]
    assert len(admin_attempts) == 1
    ctx = admin_attempts[0].context
    assert ctx["actor_admin_email"] == "ops@x.com"
    assert ctx["refund_was_created"] is True


# ── 3. Idempotency ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_re_trigger_is_noop_with_audit_row(admin_ctx, login_admin):
    """Second admin trigger after the refund has already landed: no
    double-credit, returns was_created=false, but still writes an audit
    row so 'Adebayo retried this' is reconstructable from history."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    target = _seed_target_user(db)
    tx = _seed_failed_bill_tx(db, target.id, amount=Decimal("2500.00"))

    # First trigger — creates refund.
    r1 = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "First refund — customer disputed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["was_created"] is True

    wallet_after_first = (
        db.query(Wallet).filter(Wallet.user_id == target.id).one().balance
    )
    assert wallet_after_first == Decimal("2500.00")

    # Second trigger — should be a no-op on the wallet but still 200.
    r2 = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Re-checking after escalation"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()["data"]
    assert body["was_created"] is False
    assert body["refund_reference"] == r1.json()["data"]["refund_reference"]

    # Wallet not double-credited.
    db.expire_all()
    wallet_after_second = (
        db.query(Wallet).filter(Wallet.user_id == target.id).one().balance
    )
    assert wallet_after_second == Decimal("2500.00")

    # Both audit rows written — one per admin attempt.
    admin_attempts = (
        db.query(TransactionEvent)
        .filter(
            TransactionEvent.transaction_id == tx.id,
            TransactionEvent.reason.like("admin_manual_refund_attempt%"),
        )
        .all()
    )
    assert len(admin_attempts) == 2
    reasons = sorted(e.reason for e in admin_attempts)
    assert "First refund" in reasons[0]
    assert "Re-checking" in reasons[1]


# ── 4. Negative paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_unknown_tx_404(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()

    r = await client.post(
        "/api/v1/admin/refunds/TMP-999999-NONE/trigger",
        json={"reason": "Looking for a tx that doesn't exist"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_admin_refund_rejects_refund_type_tx(admin_ctx, login_admin):
    """A refund can't itself be refunded — that would be a state-machine
    violation. The endpoint must reject before touching any wallet
    state."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    target = _seed_target_user(db)

    # Insert a refund-type tx directly.
    refund_tx = Transaction(
        user_id=target.id,
        reference=new_transaction_reference(user_id=str(target.id), prefix="TMPR"),
        type=TransactionType.refund,
        status=TransactionStatus.success,
        amount=Decimal("500.00"),
        fee=Decimal("0.00"),
        meta={"original_reference": "TMP-260428-0099"},
    )
    db.add(refund_tx)
    db.commit()

    r = await client.post(
        f"/api/v1/admin/refunds/{refund_tx.reference}/trigger",
        json={"reason": "Operator typo — tried to refund a refund"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNREFUNDABLE_TX_TYPE"


@pytest.mark.asyncio
async def test_admin_refund_rejects_short_reason(admin_ctx, login_admin):
    """Reason has min_length=3 — a single-character or empty reason
    gives ops nothing useful to read in the audit log later."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    target = _seed_target_user(db)
    tx = _seed_failed_bill_tx(db, target.id)

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "x"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422
