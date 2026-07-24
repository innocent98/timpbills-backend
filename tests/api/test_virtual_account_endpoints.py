
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fakeredis.aioredis import FakeRedis

from app.main import app
from app.api.deps import (
    get_db, get_redis, get_token_store, get_email_provider,
    reset_fake_sms, reset_fake_email, reset_fake_paystack,
)
from app.core.limiter import limiter
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
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
    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email_client
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True


async def _promote(db_session, tier=KycLevel.tier_1):
    u = db_session.query(User).filter(User.email == "e@e.co").one()
    u.kyc_level = tier
    db_session.commit()
    return u


@pytest.mark.asyncio
async def test_provision_requires_kyc(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)  # tier_0 by default
    r = await client.post(
        "/api/v1/wallet/virtual-account",
        json={"bvn": "22222222222", "account_number": "0123456789", "bank_code": "035"},
        headers=headers,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "KYC_REQUIRED"


@pytest.mark.asyncio
async def test_provision_returns_pending(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    await _promote(db_session)
    r = await client.post(
        "/api/v1/wallet/virtual-account",
        json={"bvn": "22222222222", "account_number": "0123456789", "bank_code": "035"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "pending_identity"


@pytest.mark.asyncio
async def test_get_virtual_account_404_when_absent(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/wallet/virtual-account", headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_VIRTUAL_ACCOUNT"


@pytest.mark.asyncio
async def test_list_banks(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/wallet/banks", headers=headers)
    assert r.status_code == 200
    slugs = [b["slug"] for b in r.json()["data"]["banks"]]
    assert "test-bank" in slugs
