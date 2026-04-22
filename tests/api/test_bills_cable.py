"""API-level tests for /bills/cable/* — Sprint 4 B12.

B12 adds:
  GET  /bills/cable/providers       — static catalog, auth only
  POST /bills/cable/validate-smartcard — 5-min per-user cache in BillService

B13 will add /bills/cable/plans + POST /bills/cable (purchase) and
extend this file. Neither endpoint is money-moving, so no pin_token
or Idempotency-Key requirement."""
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


# ── 1. Cable providers catalog ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cable_providers_returns_four_static_entries(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.get("/api/v1/bills/cable/providers", headers=headers)
    assert r.status_code == 200, r.text
    providers = r.json()["data"]["providers"]
    ids = [p["id"] for p in providers]
    assert set(ids) == {"dstv", "gotv", "startimes", "showmax"}
    # Every provider has a display name
    assert all(p["name"] for p in providers)


@pytest.mark.asyncio
async def test_cable_providers_requires_auth(client):
    r = await client.get("/api/v1/bills/cable/providers")
    assert r.status_code == 401


# ── 2. Validate smartcard — happy ───────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_happy_returns_customer_and_plan(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.post(
        "/api/v1/bills/cable/validate-smartcard",
        json={"service_id": "dstv", "smartcard_number": "1234567890"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "dstv"
    assert data["smartcard_number"] == "1234567890"
    # FakeVTPassClient formula: FAKE SUBSCRIBER + last-4 of smartcard_number.
    assert data["customer_name"] == "FAKE SUBSCRIBER 7890"
    assert data["status"] == "active"
    assert data["current_plan_name"]
    assert data["current_plan_code"]


# ── 3. Validate smartcard — poisoned → 400 INVALID_SMARTCARD ───────────


@pytest.mark.asyncio
async def test_validate_smartcard_invalid_returns_400(client):
    _, headers = await _seed_logged_in_user(client)

    fake = _vtpass_factory.get_fake_singleton()
    fake.will_reject_smartcard("dstv", "0000000000")
    try:
        r = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json={"service_id": "dstv", "smartcard_number": "0000000000"},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        err = r.json()["error"]
        assert err["code"] == "INVALID_SMARTCARD"
        assert "0000000000" in err["message"]
    finally:
        fake._rejected_smartcards.discard(("dstv", "0000000000"))


# ── 4. Cache hit: second call doesn't touch the provider ───────────────


@pytest.mark.asyncio
async def test_validate_smartcard_cache_hits_on_repeat_request(client):
    _, headers = await _seed_logged_in_user(client)

    fake = _vtpass_factory.get_fake_singleton()
    original_validate = fake.validate_smartcard
    call_count = {"n": 0}

    async def counting_validate(**kw):
        call_count["n"] += 1
        return await original_validate(**kw)
    fake.validate_smartcard = counting_validate  # type: ignore[method-assign]

    payload = {"service_id": "gotv", "smartcard_number": "9988776655"}
    try:
        r1 = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json=payload, headers=headers,
        )
        r2 = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json=payload, headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["data"] == r2.json()["data"]
        # Provider hit once — second request served from the 5-minute
        # per-user Redis cache keyed on (user, service, smartcard).
        assert call_count["n"] == 1
    finally:
        fake.validate_smartcard = original_validate  # type: ignore[method-assign]


# ── 5. Length-bounded smartcard_number rejected at schema layer ────────


@pytest.mark.asyncio
async def test_validate_smartcard_rejects_empty_smartcard(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.post(
        "/api/v1/bills/cable/validate-smartcard",
        json={"service_id": "dstv", "smartcard_number": ""},
        headers=headers,
    )
    assert r.status_code == 422
