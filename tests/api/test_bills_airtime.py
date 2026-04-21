"""API-level tests for /bills/airtime — happy path, idempotency,
insufficient balance, provider failure."""
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


# ── Networks catalog ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_networks_endpoint_returns_four_networks(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/bills/airtime/networks", headers=headers)
    assert r.status_code == 200
    networks = r.json()["data"]["networks"]
    ids = [n["id"] for n in networks]
    assert ids == ["mtn", "airtel", "glo", "etisalat"]
    # MTN prefixes include the common ones.
    mtn = next(n for n in networks if n["id"] == "mtn")
    assert "0803" in mtn["prefixes"]


# ── Airtime happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_purchase_happy_path(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "success"
    assert data["delivered_amount"] == "500.00"
    assert data["requested_amount"] == "500.00"
    assert data["partial"] is False

    # Wallet debited by exactly 500.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "4500.00"


# ── Airtime insufficient balance ───────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_insufficient_balance_returns_402(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("100.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    # Wallet unchanged.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "100.00"


# ── Airtime idempotency replay ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_idempotency_replay_returns_same_response(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)
    idem = str(uuid4())
    payload = {"network": "MTN", "phone": "08012345678", "amount": "500.00"}

    r1 = await client.post(
        "/api/v1/bills/airtime", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    r2 = await client.post(
        "/api/v1/bills/airtime", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Same reference returned both times.
    assert r1.json()["data"]["reference"] == r2.json()["data"]["reference"]

    # Wallet debited exactly once despite two calls.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "4500.00"


# ── Airtime provider failure triggers refund ────────────────────────────


@pytest.mark.asyncio
async def test_airtime_provider_failure_triggers_refund(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    # Make the fake VTPass fail for whatever reference we end up with.
    # Since we don't know the reference ahead of time, tweak the fake to
    # fail all purchases for the duration of this test.
    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_airtime

    async def always_fail(**kw):
        fake.will_fail(kw["request_id"])
        return await original_purchase(**kw)
    fake.purchase_airtime = always_fail  # type: ignore[method-assign]

    try:
        r = await client.post(
            "/api/v1/bills/airtime",
            json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
            headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"

        # Wallet back to original — refund+credit completed the round-trip.
        w = await client.get("/api/v1/wallet", headers=headers)
        assert w.json()["data"]["balance"] == "5000.00"

        # A refund tx was created.
        user = db_session.query(User).filter(User.email == "e@e.co").one()
        refunds = (
            db_session.query(Transaction)
            .filter(
                Transaction.user_id == user.id,
                Transaction.type == TransactionType.refund,
            ).all()
        )
        assert len(refunds) == 1
        assert refunds[0].amount == Decimal("500.00")
    finally:
        fake.purchase_airtime = original_purchase  # type: ignore[method-assign]


# ── Pin token required ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_requires_pin_token(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 401


# ── Idempotency key required ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_requires_idempotency_key(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**headers, "X-Pin-Token": pin},
    )
    assert r.status_code == 400
