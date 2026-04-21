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
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

# Module-level fake email so _seed_logged_in_user can read the OTP code
_test_email_client = FakeEmailClient()

# Import the helper — it lives in e2e; we patch it to use our email client below
from tests.e2e.test_auth_full_flows import _seed_logged_in_user  # noqa: E402
import tests.e2e.test_auth_full_flows as _e2e_mod


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


@pytest.mark.asyncio
async def test_get_wallet_returns_zero_balance_for_new_user(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/wallet", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["balance"] == "0.00"
    assert data["balance_cap"] == "50000.00"


@pytest.mark.asyncio
async def test_get_wallet_requires_auth(client):
    r = await client.get("/api/v1/wallet")
    assert r.status_code == 401
