"""GET /transactions/{reference}/events — audit timeline."""
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
from fakeredis.aioredis import FakeRedis

from app.main import app
from app.api.deps import (
    get_db, get_redis, get_token_store, get_email_provider,
    reset_fake_sms, reset_fake_email, reset_fake_paystack,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

_test_email_client = FakeEmailClient()


@pytest.fixture
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
    reset_fake_sms(); reset_fake_email(); reset_fake_paystack()
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


@pytest.mark.asyncio
async def test_get_events_for_existing_transaction_returns_list(client):
    _, headers = await _seed_logged_in_user(client)
    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    pin_token = pin_r.json()["data"]["pin_token"]
    fund_r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "1000.00"},
        headers={**headers, "X-Pin-Token": pin_token, "Idempotency-Key": str(uuid4())},
    )
    ref = fund_r.json()["data"]["reference"]

    r = await client.get(f"/api/v1/transactions/{ref}/events", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["data"]["items"]
    assert len(items) >= 1  # At least the initial pending → processing transition
    assert all("at" in e and "to_status" in e for e in items)


@pytest.mark.asyncio
async def test_get_events_for_other_user_tx_returns_404(client):
    _, headers_a = await _seed_logged_in_user(client)
    # Fund under user A
    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers_a)
    pin_token = pin_r.json()["data"]["pin_token"]
    fund_r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "500.00"},
        headers={**headers_a, "X-Pin-Token": pin_token, "Idempotency-Key": str(uuid4())},
    )
    ref = fund_r.json()["data"]["reference"]

    # Log in user B (a fresh seed) — different user, shouldn't see user A's events
    _, headers_b = await _seed_logged_in_user(
        client,
        email="user_b@example.com",
        phone="+2348000010002",
    )
    r = await client.get(f"/api/v1/transactions/{ref}/events", headers=headers_b)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_events_for_unknown_reference_returns_404(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/transactions/TMP-NOPE/events", headers=headers)
    assert r.status_code == 404
