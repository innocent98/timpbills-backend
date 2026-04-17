import json
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    reset_fake_sms,
    reset_fake_email,
    reset_fake_paystack,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
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
    reset_fake_sms()
    reset_fake_email()
    reset_fake_paystack()
    _test_email_client.sent.clear()

    # Patch the e2e module's email client so _seed_logged_in_user reads our emails
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


async def _init_funding(client, headers, amount="5000.00") -> str:
    """Return Paystack reference."""
    pin_r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    pin_token = pin_r.json()["data"]["pin_token"]
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": amount},
        headers={
            **headers,
            "X-Pin-Token": pin_token,
            "Idempotency-Key": str(uuid4()),
        },
    )
    return r.json()["data"]["reference"]


@pytest.mark.asyncio
async def test_rejects_bad_signature(client):
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=b'{"event":"charge.success","data":{"id":"1","reference":"x"}}',
        headers={"x-paystack-signature": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_charge_success_credits_wallet(client):
    _, headers = await _seed_logged_in_user(client)
    ref = await _init_funding(client, headers, amount="5000.00")

    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)

    body = {"event": "charge.success", "data": {"id": "evt_1", "reference": ref}}
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 200

    # Balance should now reflect credit
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"


@pytest.mark.asyncio
async def test_duplicate_event_is_deduped(client):
    _, headers = await _seed_logged_in_user(client)
    ref = await _init_funding(client, headers, amount="5000.00")

    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)

    body = {"event": "charge.success", "data": {"id": "evt_dup", "reference": ref}}
    r1 = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    r2 = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Wallet credited exactly once
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"
