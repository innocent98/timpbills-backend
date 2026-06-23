"""/auth/register sends ONLY the email OTP, returns no tokens, normalises phone.

Covers the register contract:
- The endpoint emits ONLY an email OTP (one ``email_verification`` row in
  ``otp_codes``) and dispatches it through the fake email provider. The
  phone OTP is sent lazily once the phone gate becomes active (after
  email verification) — register dispatches NO SMS and creates NO
  ``phone_verification`` row.
- The response carries ``next_action == "verify_email_and_phone"`` and
  no ``tokens`` field — tokens come only after both verifications +
  PIN set under the phone-only-auth plan.
- Phone normalisation happens *before* the uniqueness check, so a
  caller registering ``08012345678`` after another caller registered
  ``+2348012345678`` collides on USER_ALREADY_EXISTS.
- Unparseable phones surface as 400 INVALID_PHONE_FORMAT (not 422),
  via the ``normalize_to_e164`` helper raising InvalidPhoneFormat.
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

    def _get_redis():
        return fake_redis

    def _get_email():
        return _test_email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_email_provider] = _get_email
    reset_fake_sms()
    reset_fake_email()
    _test_email_client.sent.clear()

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_sends_only_email_otp_and_no_tokens(client):
    """POST /auth/register triggers ONLY the email OTP, returns 201 with
    next_action=verify_email_and_phone, no tokens in the response, and
    NO SMS (phone OTP is deferred to the email-verify step)."""
    pre_email = len(_test_email_client.sent)
    pre_sms = len(_fake_sms_singleton.sent)

    r = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "08011111111",
            "email": "regdual-a@example.com",
            "full_name": "Adebayo Dual",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    assert body["next_action"] == "verify_email_and_phone"
    assert "tokens" not in body
    assert body["phone"] == "+2348011111111"  # normalised
    assert body["email"] == "regdual-a@example.com"

    # Email got exactly one OTP; SMS got none for this register call.
    assert len(_test_email_client.sent) == pre_email + 1
    assert len(_fake_sms_singleton.sent) == pre_sms
    assert _test_email_client.sent[-1].to == "regdual-a@example.com"


@pytest.mark.asyncio
async def test_register_normalises_phone_before_uniqueness_check(client):
    """Registering the same logical phone in a different format must 409."""
    r1 = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "+2348012345678",
            "email": "regdup-a@example.com",
            "full_name": "Alice First",
            "password": "Secret1!",
        },
    )
    assert r1.status_code == 201, r1.text

    r = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "08012345678",  # same logical phone, local format
            "email": "regdup-b@example.com",
            "full_name": "Bob Second",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "USER_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_register_bad_phone_format_returns_400(client):
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "not-a-phone",
            "email": "regbad@example.com",
            "full_name": "Bad Phone",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_PHONE_FORMAT"


@pytest.mark.asyncio
async def test_register_persists_only_email_otp_row(client, db_session):
    """Only the email_verification row exists after register — the phone
    OTP is deferred to the email-verify step."""
    from app.db.models.otp import OtpCode, OtpPurpose
    from app.db.models.user import User

    r = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "08099999999",
            "email": "regotp@example.com",
            "full_name": "Cee Third",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    user = db_session.query(User).filter(User.email == "regotp@example.com").first()
    assert user is not None
    rows = db_session.query(OtpCode).filter(OtpCode.user_id == user.id).all()
    purposes = {row.purpose for row in rows}
    assert purposes == {OtpPurpose.email_verification}
