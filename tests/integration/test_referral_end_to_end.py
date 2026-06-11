"""End-to-end referral flow: signup with code → fund → buy airtime →
both wallets credited + activity feed updated.

Sprint 5b B3. Three scenarios:

1. Happy path — referee signs up with referrer's code, funds wallet,
   buys airtime ≥ ₦1,000 → both wallets credited, both pushes fired,
   row visible in the referrer's activity feed.

2. Sub-threshold first — referee buys ₦500 first, no credit; then
   buys ₦1,500 → credit fires on the second tx.

3. Killswitch off at signup — referral_code silently ignored; subsequent
   qualifying tx is a no-op (no row to credit against).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import tests.e2e.test_auth_full_flows as _e2e_mod
from app.api.deps import (
    _fake_push_singleton,
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
from app.db.models.app_setting import AppSetting
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.main import app
from app.services.token_store import RedisTokenStore
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

_test_email = FakeEmailClient()
_test_sms = FakeTermiiClient()


_DEFAULT_SETTINGS = {
    "REFERRAL_ENABLED": "true",
    "REFERRAL_REWARD_REFERRER_NAIRA": "100",
    "REFERRAL_REWARD_REFEREE_NAIRA": "50",
    "REFERRAL_DAILY_CAP": "5",
    "REFERRAL_LIFETIME_CAP_NAIRA": "50000",
    "REFERRAL_MIN_TX_AMOUNT_NAIRA": "1000",
    "REFERRAL_CLAWBACK_WINDOW_DAYS": "7",
}


def _seed_settings(db, **overrides) -> None:
    merged = {**_DEFAULT_SETTINGS, **overrides}
    for k, v in merged.items():
        existing = db.query(AppSetting).filter(AppSetting.key == k).first()
        if existing is not None:
            existing.value = str(v)
        else:
            db.add(AppSetting(key=k, value=str(v)))
    db.commit()


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

    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


# ── flow helpers ────────────────────────────────────────────────────────


def _fund_wallet_directly(db, *, email: str, amount: Decimal) -> User:
    user = db.query(User).filter(User.email == email).one()
    w = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if w is None:
        w = Wallet(
            user_id=user.id, balance=amount, balance_cap=Decimal("50000.00"),
        )
        db.add(w)
    else:
        w.balance = amount
    db.commit()
    return user


def _wallet_balance(db, *, email: str) -> Decimal:
    db.expire_all()
    user = db.query(User).filter(User.email == email).one()
    w = db.query(Wallet).filter(Wallet.user_id == user.id).one()
    return w.balance


async def _pin_token(client, headers) -> str:
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers,
    )
    return r.json()["data"]["pin_token"]


async def _signup_referee_with_code(
    client, *, code: str, email: str, phone: str,
) -> dict:
    """Register a new user with a referral code, verify email + set pin.
    Returns the auth headers."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Tobi Adebayo",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
            "referral_code": code,
        },
    )
    assert r.status_code == 201, r.text

    # B9: migration-branch pre-stamp so /email/verify yields tokens
    # without requiring a separate /pin/set call.
    from tests._b9_seed import stamp_for_email_verify_tokens
    stamp_for_email_verify_tokens(email=email)

    code_otp = _test_email.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code_otp},
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_happy_path_credits_both_wallets_and_fires_pushes(
    client, db_session,
):
    _seed_settings(db_session)
    # Step 1: referrer signs up cold.
    _, ref_headers = await _seed_logged_in_user(
        client, email="referrer@x.co", phone="+2348011110001",
    )
    referrer = db_session.query(User).filter(User.email == "referrer@x.co").one()
    code = referrer.referral_code

    # Step 2: referee signs up with code.
    rf_headers = await _signup_referee_with_code(
        client, code=code, email="referee@x.co", phone="+2348011110002",
    )

    row = (
        db_session.query(Referral)
        .filter(Referral.code_used == code)
        .one()
    )
    assert row.status is ReferralStatus.pending

    # Step 3: fund referee, buy airtime ≥ ₦1,000.
    _fund_wallet_directly(db_session, email="referee@x.co", amount=Decimal("5000.00"))
    pin = await _pin_token(client, rf_headers)

    _fake_push_singleton.sent.clear()
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "1500.00"},
        headers={**rf_headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "success"

    # Both wallets credited.
    assert _wallet_balance(db_session, email="referrer@x.co") == Decimal("100.00")
    # Referee: 5000 - 1500 (airtime) + 50 (referee reward) = 3550
    assert _wallet_balance(db_session, email="referee@x.co") == Decimal("3550.00")

    # Row transitioned to credited.
    db_session.refresh(row)
    assert row.status is ReferralStatus.credited
    assert row.qualifying_tx_id is not None

    # Pushes fired: referral_credited + welcome_bonus (plus the
    # bill_success + referrer_signup_notified earlier).
    push_events = [p.data.get("event") for p in _fake_push_singleton.sent]
    assert "referral_credited" in push_events
    assert "welcome_bonus" in push_events

    # Activity feed reflects the credit.
    feed = await client.get("/api/v1/users/me/referral", headers=ref_headers)
    feed_data = feed.json()["data"]
    assert feed_data["lifetime_earned_naira"] == 100
    assert feed_data["stats"]["paid_count"] == 1
    assert feed_data["recent_activity"][0]["status"] == "credited"
    assert feed_data["recent_activity"][0]["amount_naira"] == 100


@pytest.mark.asyncio
async def test_e2e_sub_threshold_then_qualifying_tx_credits(
    client, db_session,
):
    _seed_settings(db_session)
    _, ref_headers = await _seed_logged_in_user(
        client, email="r2@x.co", phone="+2348011110010",
    )
    referrer = db_session.query(User).filter(User.email == "r2@x.co").one()
    code = referrer.referral_code

    rf_headers = await _signup_referee_with_code(
        client, code=code, email="rf2@x.co", phone="+2348011110011",
    )
    _fund_wallet_directly(db_session, email="rf2@x.co", amount=Decimal("5000.00"))
    pin = await _pin_token(client, rf_headers)

    # First tx: ₦500 — below threshold, should NOT credit.
    r1 = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**rf_headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r1.status_code == 200

    # Referrer's wallet still untouched.
    referrer_wallet = (
        db_session.query(Wallet)
        .filter(Wallet.user_id == referrer.id)
        .first()
    )
    assert referrer_wallet is None or referrer_wallet.balance == Decimal("0.00")
    row = (
        db_session.query(Referral)
        .filter(Referral.code_used == code)
        .one()
    )
    db_session.refresh(row)
    assert row.status is ReferralStatus.pending  # still pending

    # Second tx: ₦1,500 — above threshold, SHOULD credit.
    pin2 = await _pin_token(client, rf_headers)
    r2 = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "1500.00"},
        headers={**rf_headers, "X-Pin-Token": pin2, "Idempotency-Key": str(uuid4())},
    )
    assert r2.status_code == 200
    assert _wallet_balance(db_session, email="r2@x.co") == Decimal("100.00")
    db_session.refresh(row)
    assert row.status is ReferralStatus.credited


@pytest.mark.asyncio
async def test_e2e_killswitch_off_at_signup_no_attribution(
    client, db_session,
):
    _seed_settings(db_session, REFERRAL_ENABLED="false")
    _, _ = await _seed_logged_in_user(
        client, email="r3@x.co", phone="+2348011110020",
    )
    referrer = db_session.query(User).filter(User.email == "r3@x.co").one()
    code = referrer.referral_code

    rf_headers = await _signup_referee_with_code(
        client, code=code, email="rf3@x.co", phone="+2348011110021",
    )
    # No row created.
    assert (
        db_session.query(Referral)
        .filter(Referral.code_used == code)
        .first()
        is None
    )

    # Subsequent qualifying tx: no row to credit against. The bill
    # completes normally; no referral side-effects.
    _fund_wallet_directly(db_session, email="rf3@x.co", amount=Decimal("5000.00"))
    pin = await _pin_token(client, rf_headers)
    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "1500.00"},
        headers={**rf_headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200
    # Referrer wallet stays untouched.
    referrer_wallet = (
        db_session.query(Wallet)
        .filter(Wallet.user_id == referrer.id)
        .first()
    )
    assert referrer_wallet is None or referrer_wallet.balance == Decimal("0.00")
