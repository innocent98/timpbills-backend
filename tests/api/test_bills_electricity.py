"""API-level tests for /bills/electricity/* — Sprint 4 B10 + B11.

B10 covers POST /bills/electricity/validate-meter (validate-only, no
pin/idempotency; 5-minute per-user Redis cache inside BillService).
B11 covers POST /bills/electricity (money-moving; pin + idempotency
headers required; cap + balance pre-flight before side effects).
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
    """Skip the Paystack round-trip in tests by seeding the wallet row.

    Mirrors the helper used in test_bills_airtime.py. Raises the cap to
    50000 so the wallet can hold enough for the cap-exceeded case."""
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id, balance=amount,
            balance_cap=Decimal("200000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = amount
        wallet.balance_cap = Decimal("200000.00")
    db.commit()
    return user


# ── 1. Happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_happy_path_returns_customer_and_address(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.post(
        "/api/v1/bills/electricity/validate-meter",
        json={
            "service_id":   "ikeja-electric",
            "meter_number": "1234567890123",
            "meter_type":   "prepaid",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "ikeja-electric"
    assert data["meter_number"] == "1234567890123"
    assert data["meter_type"] == "prepaid"
    # FakeVTPassClient fabricates "FAKE CUSTOMER <last-4>" + DisCo address.
    assert data["customer_name"] == "FAKE CUSTOMER 0123"
    assert "Ikeja Electric" in data["address"]


# ── 2. Invalid meter → 400 INVALID_METER ────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_invalid_returns_400_invalid_meter(client):
    _, headers = await _seed_logged_in_user(client)

    # Poison this (service_id, meter) pair — the fake will raise
    # ProviderPermanentFailure, which the endpoint maps to 400/INVALID_METER.
    # The client fixture also calls reset_fake_vtpass() on teardown so we
    # don't strictly need to unpoison here, but an explicit try/finally
    # makes the test self-contained and robust to future co-located tests
    # that share the singleton within one fixture scope.
    fake = _vtpass_factory.get_fake_singleton()
    fake.will_reject_meter("ikeja-electric", "0000000000000")
    try:
        r = await client.post(
            "/api/v1/bills/electricity/validate-meter",
            json={
                "service_id":   "ikeja-electric",
                "meter_number": "0000000000000",
                "meter_type":   "prepaid",
            },
            headers=headers,
        )
        assert r.status_code == 400, r.text
        err = r.json()["error"]
        assert err["code"] == "INVALID_METER"
        # Sprint 4 B21: raw exception detail (service_id, meter number,
        # upstream VTPass error desc) MUST NOT leak into the API
        # response. Verify the meter number itself is NOT echoed back
        # and the generic, user-friendly copy is returned instead.
        assert "0000000000000" not in err["message"]
        assert "ikeja" not in err["message"].lower()
        assert "vtpass" not in err["message"].lower()
        assert "meter number could not be validated" in err["message"].lower()
    finally:
        fake._rejected_meters.discard(("ikeja-electric", "0000000000000"))


# ── 3. Cache hit: second call doesn't touch the provider ───────────────


@pytest.mark.asyncio
async def test_validate_meter_cache_hits_on_repeat_request(client):
    _, headers = await _seed_logged_in_user(client)

    # Wrap the fake's validate_meter with a counter. Monkeypatch lives
    # on the singleton for the duration of the test (mirrors the
    # airtime-provider-failure pattern in test_bills_airtime.py).
    fake = _vtpass_factory.get_fake_singleton()
    original_validate = fake.validate_meter
    call_count = {"n": 0}

    async def counting_validate(**kw):
        call_count["n"] += 1
        return await original_validate(**kw)
    fake.validate_meter = counting_validate  # type: ignore[method-assign]

    payload = {
        "service_id":   "ikeja-electric",
        "meter_number": "9876543210123",
        "meter_type":   "prepaid",
    }
    try:
        r1 = await client.post(
            "/api/v1/bills/electricity/validate-meter",
            json=payload, headers=headers,
        )
        r2 = await client.post(
            "/api/v1/bills/electricity/validate-meter",
            json=payload, headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["data"] == r2.json()["data"]
        # Provider hit once; the second request was served from the
        # 5-minute per-user Redis cache in BillService.validate_meter.
        assert call_count["n"] == 1
    finally:
        fake.validate_meter = original_validate  # type: ignore[method-assign]


# ══ B11: POST /bills/electricity ═══════════════════════════════════════


def _elec_payload(**overrides):
    base = {
        "service_id":   "ikeja-electric",
        "meter_number": "1234567890123",
        "meter_type":   "prepaid",
        "phone":        "08012345678",
        "amount":       "2000.00",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_electricity_happy_path_returns_reference_token_units(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(amount="2000.00"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "success"
    assert data["service_id"] == "ikeja-electric"
    assert data["meter_number"] == "1234567890123"
    assert Decimal(data["amount"]) == Decimal("2000.00")
    # FakeVTPassClient injects token + units on delivered responses.
    assert data["token"] is not None
    assert data["units"] is not None


@pytest.mark.asyncio
async def test_electricity_insufficient_balance_returns_402(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("100.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(amount="10000.00"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 402
    # The endpoint returns HTTPException — FastAPI surfaces it under
    # `detail`; our error envelope maps `detail` -> `error`.
    err = r.json().get("error") or r.json().get("detail")
    assert err["code"] == "INSUFFICIENT_BALANCE"


@pytest.mark.asyncio
async def test_electricity_cap_exceeded_returns_422_with_details(client, db_session, monkeypatch):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    from app.core.config import settings
    monkeypatch.setattr(
        settings, "ELECTRICITY_DISCO_CAPS",
        {"ikeja-electric": Decimal("5000")},
    )

    r = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(amount="10000.00"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 422, r.text
    err = r.json().get("error") or r.json().get("detail")
    assert err["code"] == "DISCO_CAP_EXCEEDED"
    assert err["details"]["max_allowed"] == "5000"
    assert err["details"]["disco"] == "ikeja-electric"


@pytest.mark.asyncio
async def test_electricity_idempotency_replay_returns_cached_response(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)
    idem = str(uuid4())
    payload = _elec_payload(amount="1000.00")

    r1 = await client.post(
        "/api/v1/bills/electricity", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    r2 = await client.post(
        "/api/v1/bills/electricity", json=payload,
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["reference"] == r2.json()["data"]["reference"]

    # Wallet debited exactly once despite two calls.
    user = db_session.query(User).filter(User.email == "e@e.co").one()
    db_session.expire_all()
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert wallet.balance == Decimal("4000.00")


@pytest.mark.asyncio
async def test_electricity_failure_refund_is_applied(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    # Mirror the airtime-failure pattern: wrap purchase_electricity so
    # it marks the in-flight request_id as failing before forwarding.
    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_electricity

    async def always_fail(**kw):
        fake.will_fail(kw["request_id"])
        return await original_purchase(**kw)
    fake.purchase_electricity = always_fail  # type: ignore[method-assign]

    try:
        r = await client.post(
            "/api/v1/bills/electricity",
            json=_elec_payload(amount="2000.00"),
            headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"

        # Wallet restored to start-balance — refund completed.
        user = db_session.query(User).filter(User.email == "e@e.co").one()
        db_session.expire_all()
        wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
        assert wallet.balance == Decimal("5000.00")

        # A refund transaction was created.
        refunds = (
            db_session.query(Transaction)
            .filter(
                Transaction.user_id == user.id,
                Transaction.type == TransactionType.refund,
            ).all()
        )
        assert len(refunds) == 1
        assert refunds[0].amount == Decimal("2000.00")
    finally:
        fake.purchase_electricity = original_purchase  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_electricity_missing_pin_token_returns_401(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))

    r = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(),
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_electricity_missing_idempotency_key_returns_400(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(),
        headers={**headers, "X-Pin-Token": pin},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_electricity_validate_cache_then_purchase_same_meter(client, db_session):
    """Validate-meter populates a 5-minute per-user Redis cache. A
    subsequent purchase on the same meter must succeed — regression
    guard against any future cache-key collision between the two
    endpoints' Redis namespaces."""
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    r_validate = await client.post(
        "/api/v1/bills/electricity/validate-meter",
        json={
            "service_id":   "ikeja-electric",
            "meter_number": "1234567890123",
            "meter_type":   "prepaid",
        },
        headers=headers,
    )
    assert r_validate.status_code == 200, r_validate.text

    r_purchase = await client.post(
        "/api/v1/bills/electricity",
        json=_elec_payload(amount="1000.00"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r_purchase.status_code == 200, r_purchase.text
    assert r_purchase.json()["data"]["status"] == "success"
