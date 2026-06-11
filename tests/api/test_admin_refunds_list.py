"""Task 11 — admin refunds LIST (PRD §16).

The manual-refund TRIGGER endpoint already exists; this covers the read/LIST
side that the platform-admin refunds page consumes.

Refund storage ground truth (see app/services/transaction_service.py):
  * A refund is a separate ``Transaction`` row with ``type=refund``.
  * It links to its original via ``meta["original_reference"]`` (the original
    tx's reference string) and carries ``meta["original_type"]``.
  * The refund row's own ``status`` is the source of the UI status:
    success → "processed", failed/refund_failed → "failed", else "pending".
  * The refund's ``reason`` + the manual/auto signal live on the
    ``TransactionEvent`` attached to the refund row. An admin-triggered
    refund's event reason is prefixed ``admin_manual_refund``.

Tests seed refunds through the REAL ``TransactionService.create_refund`` path
so the storage shape under test is the production one, not a fabrication.
"""
import uuid
from decimal import Decimal

import pytest

from app.core.security import hash_password
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.services.transaction_service import TransactionService
from app.utils.references import new_transaction_reference


def _seed_user(db, *, full_name="Refund Target", email="target@e.co") -> User:
    user = User(
        email=email,
        phone=f"+23480{uuid.uuid4().int % 10**8:08d}",
        full_name=full_name,
        password_hash=hash_password("Secret1!"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_failed_bill_tx(db, user_id, *, amount=Decimal("1500.00")) -> Transaction:
    if db.query(Wallet).filter(Wallet.user_id == user_id).first() is None:
        db.add(Wallet(user_id=user_id, balance=Decimal("0.00"), balance_cap=Decimal("200000.00")))
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


def _seed_refund(db, original_tx, *, reason: str) -> Transaction:
    """Create a refund through the production service path, then commit
    (create_refund only flushes — its caller owns the commit)."""
    svc = TransactionService(db=db)
    refund, _ = svc.create_refund(
        original_tx=original_tx, amount=original_tx.amount, reason=reason,
    )
    db.commit()
    db.refresh(refund)
    return refund


# ── Auth ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refunds_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/refunds")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


# ── List + field mapping ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refunds_list_returns_mapped_fields(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db, full_name="Ada Customer")
    tx = _seed_failed_bill_tx(db, u.id, amount=Decimal("1500.00"))
    _seed_refund(db, tx, reason="auto refund — provider failed")

    r = await client.get("/api/v1/admin/refunds?limit=10&offset=0")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert {"items", "total", "limit", "offset"} <= data.keys()
    assert data["total"] == 1
    item = data["items"][0]

    assert item["original_reference"] == tx.reference
    assert item["type"] == "airtime"  # original tx's service type
    assert item["amount"] == "1500.00"
    assert item["customer_name"] == "Ada Customer"
    assert item["reason"] == "auto refund — provider failed"
    assert item["status"] == "processed"  # refund row status=success → processed
    assert item["manual"] is False
    assert item["created_at"]
    assert item["reference"]  # the refund's own reference


# ── Status filter (UI vocabulary, mapped) ────────────────────────────────


@pytest.mark.asyncio
async def test_refunds_status_filter_returns_only_matching(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    tx = _seed_failed_bill_tx(db, u.id)
    refund = _seed_refund(db, tx, reason="auto refund")

    # processed matches the success-status refund.
    r_ok = await client.get("/api/v1/admin/refunds?status=processed")
    assert r_ok.status_code == 200
    assert r_ok.json()["data"]["total"] == 1

    # pending excludes it (refund row is success → processed).
    r_pending = await client.get("/api/v1/admin/refunds?status=pending")
    assert r_pending.status_code == 200
    assert r_pending.json()["data"]["total"] == 0

    # sanity: the seeded refund really is success.
    assert refund.status == TransactionStatus.success


@pytest.mark.asyncio
async def test_refunds_invalid_status_filter_400(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/refunds?status=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_refunds_raw_db_status_rejected_as_invalid_filter(admin_ctx, login_admin):
    """The filter takes the UI vocabulary, not the raw DB enum — passing a
    raw enum value like 'success' is a client error, not a silent match."""
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/refunds?status=success")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FILTER"


# ── Manual vs auto ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refunds_manual_flag_true_for_admin_triggered(admin_ctx, login_admin):
    """A refund whose event reason is prefixed ``admin_manual_refund`` —
    the shape the trigger endpoint writes — surfaces as manual=true."""
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db, full_name="Manual Target", email="manual@e.co")
    tx = _seed_failed_bill_tx(db, u.id)
    _seed_refund(db, tx, reason="admin_manual_refund: customer disputed")

    r = await client.get("/api/v1/admin/refunds")
    assert r.status_code == 200
    item = r.json()["data"]["items"][0]
    assert item["manual"] is True
    assert item["reason"] == "admin_manual_refund: customer disputed"


@pytest.mark.asyncio
async def test_refunds_pagination_and_total(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    for i in range(3):
        tx = _seed_failed_bill_tx(db, u.id, amount=Decimal(f"{100 + i}.00"))
        _seed_refund(db, tx, reason=f"refund {i}")

    r1 = await client.get("/api/v1/admin/refunds?limit=2&offset=0")
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert d1["total"] == 3
    assert len(d1["items"]) == 2

    r2 = await client.get("/api/v1/admin/refunds?limit=2&offset=2")
    assert r2.json()["data"]["total"] == 3
    assert len(r2.json()["data"]["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 500])
async def test_refunds_invalid_limit_422(admin_ctx, login_admin, limit):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get(f"/api/v1/admin/refunds?limit={limit}")
    assert r.status_code == 422
