"""Case-insensitive email normalisation across the user auth surface.

Proves that email is canonicalised (lowercased) at the boundary so that
``Example@X.com`` and ``example@x.com`` resolve to the same account for
registration uniqueness, email-OTP send/verify, and forgot-password.

Self-contained client fixture (mirrors tests/api/test_auth_flow.py) so
this module doesn't depend on shared fixtures and doesn't clash with a
parallel test run.
"""
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    _fake_sms_singleton,
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_email_client = FakeEmailClient()


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

    def _get_redis():
        return fake_redis

    def _get_email():
        return _email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_email_provider] = _get_email
    reset_fake_sms()
    reset_fake_email()
    _email_client.sent.clear()

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _register(client, *, phone, email):
    return await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Case User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )


@pytest.mark.asyncio
async def test_register_stores_lowercased_email(client):
    r = await _register(client, phone="+2348133333330", email="Example@X.com")
    assert r.status_code == 201, r.text
    # Stored + echoed lowercased even though the input was mixed-case.
    assert r.json()["data"]["email"] == "example@x.com"


@pytest.mark.asyncio
async def test_duplicate_email_different_case_rejected(client):
    r1 = await _register(client, phone="+2348133333331", email="Example@X.com")
    assert r1.status_code == 201, r1.text

    # Same email, different case, different phone → must collide on email.
    r2 = await _register(client, phone="+2348133333332", email="example@x.com")
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "USER_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_email_verify_finds_user_regardless_of_case(client):
    r = await _register(client, phone="+2348133333333", email="Verify@Case.co")
    assert r.status_code == 201, r.text
    code = _email_client.sent[-1].code_or_body

    # Verify with a completely different casing than registration.
    r_v = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "VERIFY@case.CO", "code": code},
    )
    assert r_v.status_code == 200, r_v.text
    assert r_v.json()["data"]["email_verified"] is True


@pytest.mark.asyncio
async def test_email_resend_finds_user_regardless_of_case(client):
    r = await _register(client, phone="+2348133333334", email="Resend@Case.co")
    assert r.status_code == 201, r.text
    assert len(_email_client.sent) == 1

    r_r = await client.post(
        "/api/v1/auth/email/resend", json={"email": "resend@CASE.co"}
    )
    assert r_r.status_code == 200, r_r.text
    assert r_r.json()["data"]["ok"] is True
    assert len(_email_client.sent) == 2


@pytest.mark.asyncio
async def test_forgot_password_finds_user_by_email_regardless_of_case(client):
    r = await _register(client, phone="+2348133333335", email="Forgot@Case.co")
    assert r.status_code == 201, r.text

    sms_before = len(_fake_sms_singleton.sent)
    r_f = await client.post(
        "/api/v1/auth/password/forgot", json={"identifier": "FORGOT@case.CO"}
    )
    # Endpoint is silent-success regardless; the proof it resolved the user is
    # that a reset OTP SMS was dispatched to the account's phone.
    assert r_f.status_code == 200, r_f.text
    assert len(_fake_sms_singleton.sent) == sms_before + 1
