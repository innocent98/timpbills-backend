"""Task 9 — admin transaction monitoring (PRD §16).

List is filterable/paginated; detail returns the full investigation picture
(tx + user summary + linked payment + event timeline). PII is returned in
FULL — admin is a trusted surface.
"""
from decimal import Decimal

import pytest

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.utils.references import new_transaction_reference


def _seed_user(db, *, full_name="Ada Customer", email="ada@x.com", phone="+2348030000000"):
    u = User(
        email=email, phone=phone, full_name=full_name,
        password_hash="x",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _seed_tx(db, user_id, *, type_, status, amount):
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"), meta={},
    )
    db.add(tx); db.commit(); db.refresh(tx)
    return tx


@pytest.mark.asyncio
async def test_transactions_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/transactions")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_transactions_list_filters_by_status(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    _seed_tx(db, u.id, type_=TransactionType.data, status=TransactionStatus.failed, amount="500")
    r = await client.get("/api/v1/admin/transactions?status=failed&limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "items" in data and "total" in data
    assert all(i["status"] == "failed" for i in data["items"])
    assert data["items"][0]["customer_name"] == "Ada Customer"


@pytest.mark.asyncio
async def test_transaction_detail_returns_events_and_user(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    tx = _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    r = await client.get(f"/api/v1/admin/transactions/{tx.reference}")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["reference"] == tx.reference
    assert d["user"]["full_name"] == "Ada Customer"
    assert d["user"]["email"] == "ada@x.com"  # full PII, unmasked
    assert "events" in d


@pytest.mark.asyncio
async def test_transaction_detail_404(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/transactions/NOPE-123")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"
