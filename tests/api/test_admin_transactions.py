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
async def test_transactions_status_success_exclude_type_refund(admin_ctx, login_admin):
    # Refund payout rows are type=refund AND status=success, so the bare
    # "Success" status filter wrongly sweeps them in. exclude_type=refund lets
    # the UI ask for "success purchases, excluding refunds".
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    purchase = _seed_tx(
        db, u.id, type_=TransactionType.airtime,
        status=TransactionStatus.success, amount="1000",
    )
    refund = _seed_tx(
        db, u.id, type_=TransactionType.refund,
        status=TransactionStatus.success, amount="1000",
    )

    r = await client.get(
        "/api/v1/admin/transactions?status=success&exclude_type=refund"
    )
    assert r.status_code == 200
    data = r.json()["data"]
    refs = {i["reference"] for i in data["items"]}
    assert purchase.reference in refs
    assert refund.reference not in refs
    assert all(i["type"] != "refund" for i in data["items"])
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_transactions_invalid_exclude_type_filter_400(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/transactions?exclude_type=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_transactions_q_filters_by_customer_name(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    ada = _seed_user(db, full_name="Ada Customer", email="ada@x.com", phone="+2348030000001")
    bola = _seed_user(db, full_name="Bola Seller", email="bola@x.com", phone="+2348030000002")
    _seed_tx(db, ada.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    _seed_tx(db, bola.id, type_=TransactionType.data, status=TransactionStatus.success, amount="500")

    r = await client.get("/api/v1/admin/transactions?q=Ada")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert all(i["customer_name"] == "Ada Customer" for i in data["items"])


@pytest.mark.asyncio
async def test_transactions_q_filters_by_reference_fragment(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    tx = _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    _seed_user(db, full_name="Other", email="other@x.com", phone="+2348030000099")

    fragment = tx.reference[-6:]
    r = await client.get(f"/api/v1/admin/transactions?q={fragment}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["reference"] == tx.reference


@pytest.mark.asyncio
async def test_transactions_pagination_and_total(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    for _ in range(3):
        _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="100")

    r1 = await client.get("/api/v1/admin/transactions?limit=2&offset=0")
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert d1["total"] == 3
    assert len(d1["items"]) == 2

    r2 = await client.get("/api/v1/admin/transactions?limit=2&offset=2")
    assert r2.status_code == 200
    d2 = r2.json()["data"]
    assert d2["total"] == 3
    assert len(d2["items"]) == 1


@pytest.mark.asyncio
async def test_transactions_invalid_status_filter_400(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/transactions?status=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_transactions_invalid_type_filter_400(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/transactions?type=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 500])
async def test_transactions_invalid_limit_422(admin_ctx, login_admin, limit):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get(f"/api/v1/admin/transactions?limit={limit}")
    assert r.status_code == 422


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
    assert d["user"]["wallet_balance"] == "0.00"  # no wallet row seeded -> zero
    assert isinstance(d["user"]["created_at"], str) and d["user"]["created_at"]


@pytest.mark.asyncio
async def test_transaction_detail_404(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/transactions/NOPE-123")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_transaction_detail_user_wallet_balance(admin_ctx, login_admin):
    from app.db.models.wallet import Wallet
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    db.add(Wallet(user_id=u.id, balance=Decimal("12400.00"))); db.commit()
    tx = _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    r = await client.get(f"/api/v1/admin/transactions/{tx.reference}")
    assert r.status_code == 200
    assert r.json()["data"]["user"]["wallet_balance"] == "12400.00"


@pytest.mark.asyncio
async def test_transaction_detail_includes_linked_refund(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    tx = _seed_tx(db, u.id, type_=TransactionType.electricity, status=TransactionStatus.failed, amount="5000")
    refund = Transaction(
        user_id=u.id, reference=new_transaction_reference(user_id=str(u.id)),
        type=TransactionType.refund, status=TransactionStatus.success,
        amount=Decimal("5000"), fee=Decimal("0.00"),
        meta={"original_reference": tx.reference},
    )
    db.add(refund); db.commit()
    r = await client.get(f"/api/v1/admin/transactions/{tx.reference}")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["refund"] is not None
    assert d["refund"]["reference"] == refund.reference
    assert d["refund"]["status"] == "success"
    assert d["refund"]["amount"] == "5000.00"
    assert isinstance(d["refund"]["created_at"], str) and d["refund"]["created_at"]


@pytest.mark.asyncio
async def test_transaction_detail_no_linked_refund_is_null(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    tx = _seed_tx(db, u.id, type_=TransactionType.electricity, status=TransactionStatus.failed, amount="5000")
    r = await client.get(f"/api/v1/admin/transactions/{tx.reference}")
    assert r.status_code == 200
    assert r.json()["data"]["refund"] is None
