"""E2E: register → verify email → set PIN → fund wallet → webhook → balance credited → tx visible."""
import json
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
from fakeredis.aioredis import FakeRedis

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
async def test_full_funding_journey(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.get("/api/v1/wallet", headers=headers)
    assert r.json()["data"]["balance"] == "0.00"

    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    pin_token = pin_r.json()["data"]["pin_token"]

    idem = str(uuid4())
    fund_r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "5000.00"},
        headers={**headers, "X-Pin-Token": pin_token, "Idempotency-Key": idem},
    )
    assert fund_r.status_code == 200, fund_r.text
    ref = fund_r.json()["data"]["reference"]

    list_r = await client.get("/api/v1/transactions", headers=headers)
    items = list_r.json()["data"]["items"]
    assert items[0]["reference"] == ref
    assert items[0]["status"] == "processing"

    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)
    body = {"event": "charge.success", "data": {"id": "evt_e2e", "reference": ref}}
    wh_r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert wh_r.status_code == 200, wh_r.text

    r = await client.get("/api/v1/wallet", headers=headers)
    assert r.json()["data"]["balance"] == "5000.00"

    detail = await client.get(f"/api/v1/transactions/{ref}", headers=headers)
    assert detail.json()["data"]["status"] == "success"


@pytest.mark.asyncio
async def test_failed_payment_does_not_credit(client):
    _, headers = await _seed_logged_in_user(client)
    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    pin_token = pin_r.json()["data"]["pin_token"]
    fund_r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "3000.00"},
        headers={**headers, "X-Pin-Token": pin_token, "Idempotency-Key": str(uuid4())},
    )
    ref = fund_r.json()["data"]["reference"]

    body = {"event": "charge.failed", "data": {"id": "evt_e2e_fail", "reference": ref}}
    await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    r = await client.get("/api/v1/wallet", headers=headers)
    assert r.json()["data"]["balance"] == "0.00"

    detail = await client.get(f"/api/v1/transactions/{ref}", headers=headers)
    assert detail.json()["data"]["status"] == "failed"
