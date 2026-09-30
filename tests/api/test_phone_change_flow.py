"""API tests for /auth/phone/change-request + /auth/phone/change-confirm
(Sprint 5c · Task 5.1).

Covers:
  * happy path: request → OTP delivered to the *new* phone via Termii,
    confirm with that OTP, ``users.phone`` is rewritten in the DB.
  * wrong OTP: 400 INVALID_OTP.
  * unknown / expired ``request_id``: 400 INVALID_REQUEST.
  * ``request_id`` belongs to a different user: 403 USER_MISMATCH.
  * collision with existing phone: 409 PHONE_ALREADY_IN_USE.
  * auth required: 401.
  * confirm revokes existing access + refresh tokens.

Mirrors the fixture shape of ``test_password_change`` / ``test_pin_change``
so the async client + fakeredis + dependency-override plumbing stays
consistent across the Sprint 5c auth-and-account surface.
"""
from __future__ import annotations

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


async def _seed_user(
    client: AsyncClient, *, email: str, phone: str
) -> tuple[dict, dict, str]:
    """Register + verify; return (headers, tokens, user_id)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Phone Change",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["data"]["user_id"]

    from tests._b9_seed import stamp_for_email_verify_tokens
    stamp_for_email_verify_tokens(email=email)

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return headers, tokens, user_id


def _last_sms_to(phone: str) -> str | None:
    """Pluck the most-recent OTP captured by the FakeTermiiClient
    singleton that was addressed to ``phone``."""
    for sent in reversed(_fake_sms_singleton.sent):
        if sent.phone == phone:
            return sent.code_or_message
    return None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_phone_change_sends_otp_to_new_phone(client):
    headers, _, _ = await _seed_user(
        client, email="pc-req@test.co", phone="+2348099111101"
    )
    new_phone = "+2348099222201"

    r = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": new_phone},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["data"]["request_id"]

    # Termii fake captured the SMS to the *new* phone (not the old one).
    assert _last_sms_to(new_phone) is not None, "OTP not sent to new phone"


@pytest.mark.asyncio
async def test_confirm_phone_change_updates_phone(client, db_session):
    from app.db.models.user import User

    headers, _, user_id = await _seed_user(
        client, email="pc-cfm@test.co", phone="+2348099111102"
    )
    new_phone = "+2348099222202"

    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": new_phone},
    )
    assert req.status_code == 200, req.text
    request_id = req.json()["data"]["request_id"]
    otp = _last_sms_to(new_phone)
    assert otp is not None

    cfm = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": request_id, "otp": otp},
    )
    assert cfm.status_code == 200, cfm.text
    assert cfm.json()["data"]["ok"] is True

    # Verify the DB row was actually rewritten.
    db_session.expire_all()
    user = db_session.query(User).filter(User.email == "pc-cfm@test.co").first()
    assert user is not None
    assert user.phone == new_phone
    assert user.is_phone_verified is True


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_phone_change_wrong_otp(client):
    headers, _, _ = await _seed_user(
        client, email="pc-bad@test.co", phone="+2348099111103"
    )

    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": "+2348099222203"},
    )
    request_id = req.json()["data"]["request_id"]

    bad = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": request_id, "otp": "000000"},
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "INVALID_OTP"


@pytest.mark.asyncio
async def test_confirm_phone_change_compares_otp_in_constant_time(
    client, monkeypatch
):
    """The OTP check must go through hmac.compare_digest, not ``==``, so
    response timing does not leak how many leading digits matched."""
    import hmac

    from app.services import auth_service as auth_service_mod

    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_service_mod.hmac, "compare_digest", _spy)

    headers, _, _ = await _seed_user(
        client, email="pc-ct@test.co", phone="+2348099111107"
    )
    new_phone = "+2348099222207"
    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": new_phone},
    )
    request_id = req.json()["data"]["request_id"]
    otp = _last_sms_to(new_phone)
    assert otp is not None

    cfm = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": request_id, "otp": otp},
    )
    assert cfm.status_code == 200, cfm.text
    assert (otp.encode(), otp.encode()) in calls


@pytest.mark.asyncio
async def test_confirm_phone_change_unknown_request_id(client):
    headers, _, _ = await _seed_user(
        client, email="pc-unk@test.co", phone="+2348099111104"
    )
    r = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": "not-a-real-request-id", "otp": "123456"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_request_phone_change_rejects_existing_phone(client):
    """User A cannot grab user B's phone number — uniqueness guard."""
    headers_a, _, _ = await _seed_user(
        client, email="pc-a@test.co", phone="+2348099111105"
    )
    # Seed user B holding the target phone.
    await _seed_user(client, email="pc-b@test.co", phone="+2348099222205")

    r = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers_a,
        json={"new_phone": "+2348099222205"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PHONE_ALREADY_IN_USE"


@pytest.mark.asyncio
async def test_confirm_phone_change_rejects_other_user(client):
    """Request IDs are bound to the requesting user. If user A's request_id
    leaks, user B can't redeem it."""
    headers_a, _, _ = await _seed_user(
        client, email="pc-cross-a@test.co", phone="+2348099111106"
    )
    headers_b, _, _ = await _seed_user(
        client, email="pc-cross-b@test.co", phone="+2348099111107"
    )

    new_phone = "+2348099222206"
    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers_a,
        json={"new_phone": new_phone},
    )
    request_id = req.json()["data"]["request_id"]
    otp = _last_sms_to(new_phone)

    bad = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers_b,
        json={"request_id": request_id, "otp": otp},
    )
    assert bad.status_code == 403, bad.text
    assert bad.json()["error"]["code"] == "USER_MISMATCH"


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_phone_change_requires_auth(client):
    r = await client.post(
        "/api/v1/auth/phone/change-request",
        json={"new_phone": "+2348099222299"},
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_confirm_phone_change_requires_auth(client):
    r = await client.post(
        "/api/v1/auth/phone/change-confirm",
        json={"request_id": "x", "otp": "123456"},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Side-effects: existing tokens revoked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_phone_change_revokes_access_token(client):
    headers, _, _ = await _seed_user(
        client, email="pc-rev-a@test.co", phone="+2348099111108"
    )
    new_phone = "+2348099222208"
    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": new_phone},
    )
    request_id = req.json()["data"]["request_id"]
    otp = _last_sms_to(new_phone)
    cfm = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": request_id, "otp": otp},
    )
    assert cfm.status_code == 200

    # The same bearer is now revoked (tokens_revoked_at backstop).
    post = await client.get("/api/v1/auth/me", headers=headers)
    assert post.status_code == 401, post.text
    assert post.json()["error"]["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_confirm_phone_change_revokes_refresh_token(client):
    headers, tokens, _ = await _seed_user(
        client, email="pc-rev-r@test.co", phone="+2348099111109"
    )
    new_phone = "+2348099222209"
    req = await client.post(
        "/api/v1/auth/phone/change-request",
        headers=headers,
        json={"new_phone": new_phone},
    )
    request_id = req.json()["data"]["request_id"]
    otp = _last_sms_to(new_phone)
    cfm = await client.post(
        "/api/v1/auth/phone/change-confirm",
        headers=headers,
        json={"request_id": request_id, "otp": otp},
    )
    assert cfm.status_code == 200

    rf = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rf.status_code == 401, rf.text
