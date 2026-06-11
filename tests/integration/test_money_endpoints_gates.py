"""B14: money endpoints are gated by require_full_auth_gates.

Tests strict-mode behavior — when a user has any gate failing, money
endpoints must 403; auth endpoints continue to work for the migration
flow."""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_sms_provider,
    get_token_store,
    reset_fake_email,
    reset_fake_paystack,
    reset_fake_push,
    reset_fake_sms,
    reset_fake_vtpass,
)
from app.core.limiter import limiter
from app.core.security import create_access_token, hash_password, hash_pin
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email = FakeEmailClient()
_test_sms = FakeTermiiClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async TestClient + fakeredis + dep overrides — mirrors the pattern
    in test_referral_end_to_end so we hit the same surface the e2e tests
    cover."""
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email

    def _get_sms():
        return _test_sms

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_sms_provider] = _get_sms
    app.dependency_overrides[get_redis] = _get_redis
    reset_fake_sms()
    reset_fake_email()
    reset_fake_paystack()
    reset_fake_vtpass()
    reset_fake_push()
    _test_email.sent.clear()
    _test_sms.sent.clear()

    # The auth gate fires inside a request handler — rate limiting is
    # orthogonal here. Disable so the strict-mode 403 isn't shadowed by
    # a 429 if the test re-runs in the same minute window.
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    await fake_redis.aclose()
    app.dependency_overrides.clear()


def _seed(db, *, email_verified, phone_verified, has_pin,
          phone="+2348011111111", email="b14@example.com"):
    user = User(
        phone=phone, email=email, full_name="B14 User",
        password_hash=hash_password("Secret1!"),
        referral_code=f"B14{phone[-4:]}",
        kyc_level=KycLevel.tier_1 if phone_verified else KycLevel.tier_0,
        email_verified=email_verified,
        is_phone_verified=phone_verified,
        pin_hash=hash_pin("1234") if has_pin else None,
        is_active=True,
    )
    db.add(user); db.commit(); db.refresh(user)
    return user


def _auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(subject=str(user.id))}"}


@pytest.mark.asyncio
async def test_wallet_blocked_when_phone_unverified_strict(
    client, db_session, monkeypatch,
):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    user = _seed(db_session,
                 email_verified=True, phone_verified=False, has_pin=True,
                 phone="+2348022222222", email="b14-wal-strict@example.com")
    r = await client.get("/api/v1/wallet", headers=_auth_headers(user))
    assert r.status_code == 403
    body = r.json()
    err = body.get("detail") or body.get("error") or {}
    assert err.get("code") == "VERIFICATION_REQUIRED"
    assert (err.get("details") or {}).get("which") == "phone"


@pytest.mark.asyncio
async def test_wallet_allowed_when_all_gates_pass(client, db_session, monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    user = _seed(db_session,
                 email_verified=True, phone_verified=True, has_pin=True,
                 phone="+2348033333333", email="b14-wal-ok@example.com")
    r = await client.get("/api/v1/wallet", headers=_auth_headers(user))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_wallet_passes_in_soft_mode_with_missing_gate(
    client, db_session, monkeypatch,
):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", False)
    user = _seed(db_session,
                 email_verified=True, phone_verified=False, has_pin=True,
                 phone="+2348044444444", email="b14-wal-soft@example.com")
    r = await client.get("/api/v1/wallet", headers=_auth_headers(user))
    assert r.status_code == 200   # soft mode logs warning but lets through


@pytest.mark.asyncio
async def test_auth_me_allowed_even_when_gate_fails(
    client, db_session, monkeypatch,
):
    """Allowlist: /auth/me must work pre-gate-pass so mobile can read state."""
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    user = _seed(db_session,
                 email_verified=True, phone_verified=False, has_pin=False,
                 phone="+2348055555555", email="b14-me@example.com")
    r = await client.get("/api/v1/auth/me", headers=_auth_headers(user))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_transactions_blocked_when_email_unverified_strict(
    client, db_session, monkeypatch,
):
    """Verify the gate's `which` reports the FIRST failing gate (email)."""
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    user = _seed(db_session,
                 email_verified=False, phone_verified=False, has_pin=False,
                 phone="+2348066666666", email="b14-tx-strict@example.com")
    r = await client.get("/api/v1/transactions", headers=_auth_headers(user))
    assert r.status_code == 403
    body = r.json()
    err = body.get("detail") or body.get("error") or {}
    assert (err.get("details") or {}).get("which") == "email"


@pytest.mark.asyncio
async def test_transactions_blocked_when_pin_missing_strict(
    client, db_session, monkeypatch,
):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    user = _seed(db_session,
                 email_verified=True, phone_verified=True, has_pin=False,
                 phone="+2348077777777", email="b14-tx-pin@example.com")
    r = await client.get("/api/v1/transactions", headers=_auth_headers(user))
    assert r.status_code == 403
    body = r.json()
    err = body.get("detail") or body.get("error") or {}
    assert (err.get("details") or {}).get("which") == "pin_setup"
