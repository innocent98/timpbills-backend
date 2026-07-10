"""KYC API endpoints (A7): GET /kyc/config, POST /kyc/verify/start,
POST /kyc/verify/confirm, GET /kyc/status, POST /kyc/webhook.

Wraps KycService (A6) — FakeKycProvider (autouse FORCE_FAKE_PROVIDERS in
tests/conftest.py) resolves outcomes deterministically by reference-id
substring (see app/integrations/dojah/fake.py): FAILFACE-* fails on face
match, unrecognized prefixes (including our own minted KYC-<TYPE>-<hex>
references) default to success.
"""
import hashlib
import hmac
import json
from datetime import date

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
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
    _test_email_client.sent.clear()

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


def _bump_tier(db_session, *, email, tier, dob=None):
    user = db_session.query(User).filter(User.email == email).one()
    user.kyc_level = tier
    if dob is not None:
        user.date_of_birth = dob
    db_session.commit()
    db_session.refresh(user)
    return user


def _seed_pending_record(db_session, *, user, reference_id, verification_type="bvn"):
    record = KycRecord(
        user_id=user.id,
        verification_type=verification_type,
        provider="dojah",
        provider_reference=reference_id,
        status="pending",
        tier_before=user.kyc_level.numeric,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.mark.asyncio
async def test_get_config_returns_widget_fields(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/kyc/config", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data.keys()) == {
        "app_id", "public_key", "bvn_widget_id", "nin_widget_id", "environment",
    }
    assert data["environment"] == "sandbox"


@pytest.mark.asyncio
async def test_get_config_requires_auth(client):
    r = await client.get("/api/v1/kyc/config")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_start_bvn_for_tier1_user_mints_pending_record(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))

    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert r.status_code == 200
    ref = r.json()["data"]["reference_id"]
    assert ref.startswith("KYC-BVN-")

    record = (
        db_session.query(KycRecord)
        .filter(KycRecord.provider_reference == ref)
        .one()
    )
    assert record.status == "pending"


@pytest.mark.asyncio
async def test_full_happy_path_start_confirm_upgrades_tier(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))

    start_r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert start_r.status_code == 200
    ref = start_r.json()["data"]["reference_id"]

    confirm_r = await client.post(
        "/api/v1/kyc/verify/confirm",
        json={"reference_id": ref},
        headers=headers,
    )
    assert confirm_r.status_code == 200
    body = confirm_r.json()["data"]
    assert body["status"] == "success"
    assert body["tier"] == 2
    assert body["verification_type"] == "bvn"
    assert body["reference"] == ref
    assert body["liveness_passed"] is True
    assert body["face_match"] is True
    assert body["failure_reason"] is None

    me_r = await client.get("/api/v1/auth/me", headers=headers)
    assert me_r.json()["data"]["kyc_level"] == 2


@pytest.mark.asyncio
async def test_confirm_failface_reference_returns_failed(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    user = _bump_tier(
        db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1),
    )
    _seed_pending_record(db_session, user=user, reference_id="FAILFACE-BVN-1")

    r = await client.post(
        "/api/v1/kyc/verify/confirm",
        json={"reference_id": "FAILFACE-BVN-1"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["status"] == "failed"
    assert body["failure_reason"] == "face_mismatch"
    assert body["tier"] == 1

    db_session.refresh(user)
    assert user.kyc_level == KycLevel.tier_1


@pytest.mark.asyncio
async def test_start_bvn_tier0_user_returns_409(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "kyc_tier_precondition"


@pytest.mark.asyncio
async def test_start_bvn_no_dob_returns_422(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=None)

    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "date_of_birth_required"


@pytest.mark.asyncio
async def test_start_invalid_verification_type_returns_422(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))

    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "passport"},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_verification_type"


@pytest.mark.asyncio
async def test_confirm_someone_elses_reference_returns_404(client, db_session):
    _, headers_a = await _seed_logged_in_user(
        client, email="a@a.co", phone="+2348000030001",
    )
    _bump_tier(db_session, email="a@a.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))
    start_r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers_a,
    )
    ref = start_r.json()["data"]["reference_id"]

    _, headers_b = await _seed_logged_in_user(
        client, email="b@b.co", phone="+2348000030002",
    )
    r = await client.post(
        "/api/v1/kyc/verify/confirm",
        json={"reference_id": ref},
        headers=headers_b,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_reference"


@pytest.mark.asyncio
async def test_status_returns_records_and_tier(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))
    start_r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    ref = start_r.json()["data"]["reference_id"]

    r = await client.get("/api/v1/kyc/status", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["tier"] == 1
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["reference"] == ref
    assert rec["status"] == "pending"
    assert rec["verification_type"] == "bvn"
    assert rec["liveness_passed"] is False
    assert rec["face_match"] is False


@pytest.mark.asyncio
async def test_status_requires_auth(client):
    r = await client.get("/api/v1/kyc/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_bad_signature_returns_401(client):
    r = await client.post(
        "/api/v1/kyc/webhook",
        content=b'{"reference_id": "KYC-BVN-doesnotexist"}',
        headers={"x-dojah-signature": "wrong"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_webhook_valid_signature_reconciles_pending_record(
    client, db_session, monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "test-webhook-secret")

    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))
    start_r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    ref = start_r.json()["data"]["reference_id"]

    raw_body = json.dumps({"reference_id": ref}).encode()
    signature = hmac.new(
        b"test-webhook-secret", raw_body, hashlib.sha256,
    ).hexdigest()

    r = await client.post(
        "/api/v1/kyc/webhook",
        content=raw_body,
        headers={"x-dojah-signature": signature},
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"

    record = (
        db_session.query(KycRecord)
        .filter(KycRecord.provider_reference == ref)
        .one()
    )
    assert record.status == "success"

    user = db_session.query(User).filter(User.email == "e@e.co").one()
    assert user.kyc_level == KycLevel.tier_2


@pytest.mark.asyncio
async def test_webhook_missing_reference_returns_200(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "test-webhook-secret")
    raw_body = b"{}"
    signature = hmac.new(
        b"test-webhook-secret", raw_body, hashlib.sha256,
    ).hexdigest()

    r = await client.post(
        "/api/v1/kyc/webhook",
        content=raw_body,
        headers={"x-dojah-signature": signature},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_webhook_unknown_reference_returns_200(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "test-webhook-secret")
    raw_body = json.dumps({"reference_id": "KYC-BVN-nosuchref"}).encode()
    signature = hmac.new(
        b"test-webhook-secret", raw_body, hashlib.sha256,
    ).hexdigest()

    r = await client.post(
        "/api/v1/kyc/webhook",
        content=raw_body,
        headers={"x-dojah-signature": signature},
    )
    assert r.status_code == 200
