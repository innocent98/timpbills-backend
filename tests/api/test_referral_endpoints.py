"""API tests for /users/me/referral + /users/me/referrals.

Sprint 5b B3. Covers:
- empty state (no referrals → graceful payload)
- mixed-status overview (joined/paid/pending/voided counts + recent feed)
- pagination on the history endpoint
- referee PII masking (only first + last initial; never email / phone)
- referee_deleted voids hidden from both feeds
- killswitch off → config block reports 0s
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import tests.e2e.test_auth_full_flows as _e2e_mod
from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_sms_provider,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.db.models.app_setting import AppSetting
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.main import app
from app.services.referral_service import VoidReason
from app.services.token_store import RedisTokenStore
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

_test_email = FakeEmailClient()
_test_sms = FakeTermiiClient()


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
    reset_fake_email()
    reset_fake_sms()
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


# ── helpers ────────────────────────────────────────────────────────────


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


def _seed_user(db, *, full_name: str = "Tobi Adebayo") -> User:
    u = User(
        email=f"u-{uuid4().hex[:10]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name=full_name,
        password_hash="x",
        email_verified=True,
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _seed_referral(
    db,
    *,
    referrer: User,
    referee: User,
    status: ReferralStatus = ReferralStatus.pending,
    void_reason: str | None = None,
    credited_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Referral:
    r = Referral(
        referrer_user_id=referrer.id,
        referee_user_id=referee.id,
        code_used=referrer.referral_code,
        status=status,
        void_reason=void_reason,
        credited_at=credited_at,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    if created_at is not None:
        r.created_at = created_at
        db.commit()
        db.refresh(r)
    return r


# ── /users/me/referral (overview) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_empty_state_returns_zero_stats_and_no_activity(
    client, db_session,
):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="solo@x.co")

    r = await client.get("/api/v1/users/me/referral", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert isinstance(data["code"], str) and len(data["code"]) >= 6
    assert data["share_url"].endswith(f"/{data['code']}")
    assert data["lifetime_earned_naira"] == 0
    assert data["stats"] == {
        "joined_count": 0,
        "paid_count": 0,
        "pending_count": 0,
        "voided_count": 0,
    }
    assert data["recent_activity"] == []
    assert data["config"] == {
        "referrer_reward_naira": 100,
        "referee_reward_naira": 50,
        "min_tx_amount_naira": 1000,
    }


@pytest.mark.asyncio
async def test_overview_mixed_status_counts_and_lifetime_earnings(
    client, db_session,
):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="ref@x.co")
    referrer = db_session.query(User).filter(User.email == "ref@x.co").one()

    # 3 credited + 2 pending + 1 voided + 1 hidden referee_deleted void
    now = datetime.now(UTC)
    for i in range(3):
        _seed_referral(
            db_session, referrer=referrer, referee=_seed_user(db_session),
            status=ReferralStatus.credited, credited_at=now - timedelta(minutes=i),
        )
    for _ in range(2):
        _seed_referral(
            db_session, referrer=referrer, referee=_seed_user(db_session),
        )
    _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.voided, void_reason="lifetime_cap_exceeded",
    )
    _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.voided, void_reason=VoidReason.referee_deleted,
    )

    r = await client.get("/api/v1/users/me/referral", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]

    # joined_count excludes hidden referee_deleted voids
    assert data["stats"]["joined_count"] == 6
    assert data["stats"]["paid_count"] == 3
    assert data["stats"]["pending_count"] == 2
    assert data["stats"]["voided_count"] == 1
    # lifetime = 3 credits × 100
    assert data["lifetime_earned_naira"] == 300
    # recent_activity capped at 5
    assert len(data["recent_activity"]) == 5


@pytest.mark.asyncio
async def test_overview_masks_referee_pii(client, db_session):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="masker@x.co")
    referrer = db_session.query(User).filter(User.email == "masker@x.co").one()

    referee = _seed_user(db_session, full_name="Tobi Adebayo")
    _seed_referral(
        db_session, referrer=referrer, referee=referee,
        status=ReferralStatus.credited,
        credited_at=datetime.now(UTC),
    )

    r = await client.get("/api/v1/users/me/referral", headers=headers)
    item = r.json()["data"]["recent_activity"][0]
    body = r.text

    assert item["referee_display_name"] == "Tobi A."
    assert item["amount_naira"] == 100
    assert referee.email not in body
    assert referee.phone not in body


@pytest.mark.asyncio
async def test_overview_killswitch_off_reports_zeroed_config(
    client, db_session,
):
    _seed_settings(db_session, REFERRAL_ENABLED="false")
    _, headers = await _seed_logged_in_user(client, email="kill@x.co")

    r = await client.get("/api/v1/users/me/referral", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["config"] == {
        "referrer_reward_naira": 0,
        "referee_reward_naira": 0,
        "min_tx_amount_naira": 0,
    }


# ── /users/me/referrals (paginated history) ────────────────────────────


@pytest.mark.asyncio
async def test_history_pagination_limit_and_offset(client, db_session):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="page@x.co")
    referrer = db_session.query(User).filter(User.email == "page@x.co").one()

    # Seed 12 visible rows (mix of statuses) + 1 hidden referee_deleted
    now = datetime.now(UTC)
    for i in range(12):
        _seed_referral(
            db_session, referrer=referrer, referee=_seed_user(db_session),
            status=ReferralStatus.credited,
            credited_at=now - timedelta(seconds=i),
            created_at=now - timedelta(seconds=i),
        )
    _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.voided, void_reason=VoidReason.referee_deleted,
    )

    page1 = await client.get(
        "/api/v1/users/me/referrals?limit=5&offset=0", headers=headers,
    )
    assert page1.status_code == 200
    p1 = page1.json()["data"]
    assert p1["total"] == 12
    assert p1["limit"] == 5
    assert p1["offset"] == 0
    assert len(p1["items"]) == 5

    page3 = await client.get(
        "/api/v1/users/me/referrals?limit=5&offset=10", headers=headers,
    )
    p3 = page3.json()["data"]
    assert p3["offset"] == 10
    assert len(p3["items"]) == 2  # 12 - 10


@pytest.mark.asyncio
async def test_history_empty_state(client, db_session):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="empty@x.co")

    r = await client.get("/api/v1/users/me/referrals", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_history_hides_referee_deleted_voids(client, db_session):
    _seed_settings(db_session)
    _, headers = await _seed_logged_in_user(client, email="hide@x.co")
    referrer = db_session.query(User).filter(User.email == "hide@x.co").one()

    visible = _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.credited,
        credited_at=datetime.now(UTC),
    )
    _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.voided, void_reason=VoidReason.referee_deleted,
    )
    _seed_referral(
        db_session, referrer=referrer, referee=_seed_user(db_session),
        status=ReferralStatus.voided, void_reason="lifetime_cap_exceeded",
    )

    r = await client.get("/api/v1/users/me/referrals", headers=headers)
    data = r.json()["data"]
    # 2 visible (1 credited + 1 generic voided); referee_deleted hidden
    assert data["total"] == 2
    ids = [it["id"] for it in data["items"]]
    assert str(visible.id) in ids
