from uuid import uuid4

import pytest
import pytest_asyncio
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
    _fake_paystack_singleton,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
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


async def _pin_token(client, headers) -> str:
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["pin_token"]


@pytest.mark.asyncio
async def test_fund_wallet_happy_path(client):
    _, headers = await _seed_logged_in_user(client)
    pin_token = await _pin_token(client, headers)

    key = str(uuid4())
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "5000.00"},
        headers={
            **headers,
            "X-Pin-Token": pin_token,
            "Idempotency-Key": key,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["reference"].startswith("TMP-")
    assert "authorization_url" in data
    from app.api.deps import _fake_paystack_singleton as fps
    assert fps.initialized[-1][0] == data["reference"]


@pytest.mark.asyncio
async def test_fund_wallet_requires_pin_token(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "1000.00"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "PIN_TOKEN_REQUIRED"


@pytest.mark.asyncio
async def test_fund_wallet_requires_idempotency_key(client):
    _, headers = await _seed_logged_in_user(client)
    pin_token = await _pin_token(client, headers)
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "1000.00"},
        headers={**headers, "X-Pin-Token": pin_token},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.asyncio
async def test_fund_wallet_replay_returns_same_response(client):
    _, headers = await _seed_logged_in_user(client)
    pin_token = await _pin_token(client, headers)
    key = str(uuid4())
    hdrs = {**headers, "X-Pin-Token": pin_token, "Idempotency-Key": key}
    r1 = await client.post("/api/v1/wallet/fund", json={"amount": "1000.00"}, headers=hdrs)
    r2 = await client.post("/api/v1/wallet/fund", json={"amount": "1000.00"}, headers=hdrs)
    assert r1.json() == r2.json()


@pytest.mark.asyncio
async def test_fund_wallet_key_reuse_with_different_body_conflicts(client):
    _, headers = await _seed_logged_in_user(client)
    pin_token = await _pin_token(client, headers)
    key = str(uuid4())
    hdrs = {**headers, "X-Pin-Token": pin_token, "Idempotency-Key": key}
    await client.post("/api/v1/wallet/fund", json={"amount": "1000.00"}, headers=hdrs)
    r2 = await client.post("/api/v1/wallet/fund", json={"amount": "2000.00"}, headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
