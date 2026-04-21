"""API-level tests for /bills/data — happy path, idempotency,
insufficient balance, provider failure, plan lookup.

Mirror of tests/api/test_bills_airtime.py. The notable data-only paths:
 • server-side price resolution from the VTPass plan catalog (so a
   client can't spoof the amount via the request body),
 • `UNKNOWN_DATA_PLAN` 400 when `variation_code` isn't in the catalog,
 • `GET /bills/data/plans?network=…` passthrough to the provider.
"""
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_paystack,
    reset_fake_sms,
    reset_fake_vtpass,
)
from app.core.limiter import limiter
from app.db.models._enums import TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.email.fake import FakeEmailClient
from app.integrations.vtpass import factory as _vtpass_factory
from app.main import app
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user


_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email_client

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_redis] = _get_redis
    reset_fake_sms()
    reset_fake_email()
    reset_fake_paystack()
    reset_fake_vtpass()
    _test_email_client.sent.clear()

    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email_client

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _pin_token(client, headers) -> str:
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    return r.json()["data"]["pin_token"]


def _fund_wallet_directly(db, *, email="e@e.co", amount=Decimal("5000.00")) -> User:
    """Skip the Paystack round-trip in tests by seeding the wallet row."""
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id, balance=amount,
            balance_cap=Decimal("50000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = amount
    db.commit()
    return user


# ── Plans passthrough ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_plans_endpoint_returns_seeded_catalog(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/bills/data/plans?network=MTN", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "mtn-data"
    codes = [p["variation_code"] for p in data["plans"]]
    assert "mtn-1gb-monthly" in codes
    # Server exposes price — this is the source of truth for mobile's
    # confirmation UI.
    one_gb = next(p for p in data["plans"] if p["variation_code"] == "mtn-1gb-monthly")
    assert one_gb["price"] == "1000.00"
    assert one_gb["validity"] == "30 days"


# ── Data happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_purchase_happy_path(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/data",
        json={
            "network": "MTN",
            "phone": "08012345678",
            "variation_code": "mtn-1gb-monthly",
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "success"
    # Price resolved server-side from the plan catalog (₦1000), not from
    # any request-body field — the request doesn't carry an amount.
    assert data["price"] == "1000.00"
    assert data["plan_name"] == "1GB - 30 days"

    # Wallet debited by exactly the plan price.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "4000.00"


# ── Data insufficient balance ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_insufficient_balance_returns_402(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("500.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/data",
        json={
            "network": "MTN",
            "phone": "08012345678",
            "variation_code": "mtn-1gb-monthly",   # ₦1000 > ₦500 balance
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    # Wallet unchanged.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "500.00"


# ── Data idempotency replay ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_idempotency_replay_returns_same_response(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)
    idem = str(uuid4())
    payload = {
        "network": "MTN",
        "phone": "08012345678",
        "variation_code": "mtn-1gb-monthly",
    }

    r1 = await client.post(
        "/api/v1/bills/data", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    r2 = await client.post(
        "/api/v1/bills/data", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["reference"] == r2.json()["data"]["reference"]

    # Wallet debited exactly once despite two calls.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "4000.00"


# ── Data provider failure triggers refund ──────────────────────────────


@pytest.mark.asyncio
async def test_data_provider_failure_triggers_refund(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    # Force VTPass to fail this purchase. We don't know the request_id
    # up-front, so wrap purchase_data to mark whatever ref the service
    # generates as "will_fail" right before the real fake method runs.
    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_data

    async def always_fail(**kw):
        fake.will_fail(kw["request_id"])
        return await original_purchase(**kw)
    fake.purchase_data = always_fail  # type: ignore[method-assign]

    try:
        r = await client.post(
            "/api/v1/bills/data",
            json={
                "network": "MTN",
                "phone": "08012345678",
                "variation_code": "mtn-1gb-monthly",
            },
            headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"

        # Wallet back to original — refund+credit completed the round-trip.
        w = await client.get("/api/v1/wallet", headers=headers)
        assert w.json()["data"]["balance"] == "5000.00"

        # A refund tx was created for the full plan price.
        user = db_session.query(User).filter(User.email == "e@e.co").one()
        refunds = (
            db_session.query(Transaction)
            .filter(
                Transaction.user_id == user.id,
                Transaction.type == TransactionType.refund,
            ).all()
        )
        assert len(refunds) == 1
        assert refunds[0].amount == Decimal("1000.00")
    finally:
        fake.purchase_data = original_purchase  # type: ignore[method-assign]


# ── Pin token required ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_requires_pin_token(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    r = await client.post(
        "/api/v1/bills/data",
        json={
            "network": "MTN",
            "phone": "08012345678",
            "variation_code": "mtn-1gb-monthly",
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 401


# ── Idempotency key required ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_requires_idempotency_key(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)
    r = await client.post(
        "/api/v1/bills/data",
        json={
            "network": "MTN",
            "phone": "08012345678",
            "variation_code": "mtn-1gb-monthly",
        },
        headers={**headers, "X-Pin-Token": pin},
    )
    assert r.status_code == 400


# ── Unknown data plan (data-specific 400 branch) ───────────────────────


@pytest.mark.asyncio
async def test_data_unknown_variation_code_returns_400(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/data",
        json={
            "network": "MTN",
            "phone": "08012345678",
            "variation_code": "mtn-doesnt-exist",
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_DATA_PLAN"

    # Wallet unchanged — we reject before any debit.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"
