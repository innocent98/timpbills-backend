from decimal import Decimal
from uuid import uuid4

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
from app.db.models._enums import SpendLockReason
from app.db.models.user import User
from app.db.models.wallet import Wallet
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
    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _pin_token(client, headers) -> str:
    r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    return r.json()["data"]["pin_token"]


@pytest.mark.asyncio
async def test_airtime_blocked_when_wallet_spend_locked(db_session, client):
    _tokens, headers = await _seed_logged_in_user(client)
    user_row = db_session.query(User).filter(User.email == "e@e.co").one()
    # Fund + lock the wallet directly.
    w = Wallet(
        user_id=user_row.id, balance=Decimal("5000.00"),
        balance_cap=Decimal("50000.00"), spend_locked=True,
        spend_locked_reason=SpendLockReason.over_cap,
    )
    db_session.add(w)
    db_session.commit()

    pin = await _pin_token(client, headers)
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "mtn", "phone": "08012345678", "amount": "1000.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "WALLET_SPEND_LOCKED"
    # Balance untouched (never debited).
    db_session.expire_all()
    assert db_session.query(Wallet).filter(Wallet.user_id == user_row.id).one().balance == Decimal("5000.00")
