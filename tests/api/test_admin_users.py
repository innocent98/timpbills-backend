"""Task 10 — admin user viewing (PRD §16, READ-ONLY in v1).

List is filterable (q across name/email/phone, tier, status) and paginated;
detail returns the profile + wallet + referral + recent transactions. PII is
returned in FULL — admin is a trusted surface.
"""
from decimal import Decimal

import pytest

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.utils.references import new_transaction_reference


def _seed_user(
    db,
    *,
    full_name="Ada Customer",
    email="ada@x.com",
    phone="+2348030000000",
    kyc_level=KycLevel.tier_0,
):
    u = User(
        email=email, phone=phone, full_name=full_name,
        password_hash="x", kyc_level=kyc_level,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _seed_wallet(db, user_id, *, balance="0.00", balance_cap="50000.00"):
    w = Wallet(
        user_id=user_id, balance=Decimal(balance), balance_cap=Decimal(balance_cap)
    )
    db.add(w); db.commit(); db.refresh(w)
    return w


def _seed_tx(db, user_id, *, type_, status, amount):
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"), meta={},
    )
    db.add(tx); db.commit(); db.refresh(tx)
    return tx


@pytest.mark.asyncio
async def test_users_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/users")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_users_list_returns_full_pii(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    _seed_wallet(db, u.id, balance="1234.50")

    r = await client.get("/api/v1/admin/users")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "items" in data and "total" in data
    assert data["total"] == 1
    item = data["items"][0]
    assert "@" in item["email"]  # full PII, unmasked
    assert item["kyc_tier"] == 0
    assert item["wallet_balance"] == "1234.50"
    assert item["status"] == "active"


@pytest.mark.asyncio
async def test_users_list_q_filter(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    _seed_user(db, full_name="Ada Customer", email="ada@x.com", phone="+2348030000001")
    _seed_user(db, full_name="Bola Seller", email="bola@x.com", phone="+2348030000002")

    r = await client.get("/api/v1/admin/users?q=Bola")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["full_name"] == "Bola Seller"


@pytest.mark.asyncio
async def test_users_list_tier_filter(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    _seed_user(db, full_name="Tier0 User", email="t0@x.com", phone="+2348030000003")
    _seed_user(
        db, full_name="Tier1 User", email="t1@x.com", phone="+2348030000004",
        kyc_level=KycLevel.tier_1,
    )

    r = await client.get("/api/v1/admin/users?tier=tier_1")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["kyc_tier"] == 1

    bad = await client.get("/api/v1/admin/users?tier=bogus")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_user_detail(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    _seed_wallet(db, u.id, balance="500.00")
    _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="100")
    _seed_tx(db, u.id, type_=TransactionType.data, status=TransactionStatus.failed, amount="200")

    r = await client.get(f"/api/v1/admin/users/{u.id}")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["id"] == str(u.id)
    assert d["full_name"] == "Ada Customer"
    assert d["email"] == "ada@x.com"
    assert d["kyc_tier"] == 0
    assert d["wallet_balance"] == "500.00"
    assert d["referral"]["code"] == u.referral_code
    assert len(d["recent_transactions"]) == 2
    assert len(d["recent_transactions"]) <= 10


@pytest.mark.asyncio
async def test_user_detail_404(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()

    import uuid
    unknown = await client.get(f"/api/v1/admin/users/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "USER_NOT_FOUND"

    # A non-UUID id must be a clean 404, never a 500.
    non_uuid = await client.get("/api/v1/admin/users/not-a-uuid")
    assert non_uuid.status_code == 404
    assert non_uuid.json()["error"]["code"] == "USER_NOT_FOUND"
