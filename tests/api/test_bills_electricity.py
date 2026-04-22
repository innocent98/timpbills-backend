"""API-level tests for /bills/electricity/* — Sprint 4 B10.

B10 adds POST /bills/electricity/validate-meter. Validation is NOT
money-moving: no pin_token, no Idempotency-Key. A 5-minute per-user
Redis cache sits inside ``BillService.validate_meter``; these tests
assert the endpoint's auth/shape/error mapping and verify the cache
short-circuits the provider on a second identical request."""
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
        # VTPass error description is surfaced to the client.
        assert "0000000000000" in err["message"]
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
