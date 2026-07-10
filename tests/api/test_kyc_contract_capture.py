"""Cross-stack contract capture (A7): dumps REAL JSON responses from the
KYC endpoints to tests/api/_kyc_contract/*.json fixtures so the mobile
repo can prove its DTOs deserialize actual backend output rather than
hand-written fixtures.

Reuses the exact `client` fixture / helper conventions from
test_kyc_endpoints.py (FakeKycProvider via autouse FORCE_FAKE_PROVIDERS
in tests/conftest.py — see that module's docstring). Helpers are
duplicated here rather than imported, matching the instruction to avoid
restructuring shared conftest.py for cross-test-file reuse.

Every test in this module both asserts correctness (same shapes as
test_kyc_endpoints.py) AND writes a fixture file. Success-shaped
fixtures capture only `r.json()["data"]` (what mobile's `fromJson` runs
against, post `_unwrap()`); error fixtures capture the full envelope
since `error.code`/`error.message` is the contract there.
"""
import json
from datetime import date
from pathlib import Path

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

FIXTURE_DIR = Path(__file__).parent / "_kyc_contract"


def _dump(name: str, payload: dict) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
async def test_capture_config(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/kyc/config", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data.keys()) == {
        "app_id", "public_key", "bvn_widget_id", "nin_widget_id", "environment",
    }
    assert data["environment"] == "sandbox"
    _dump("config.json", data)


@pytest.mark.asyncio
async def test_capture_start_and_confirm_success(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=date(1990, 1, 1))

    start_r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert start_r.status_code == 200
    start_data = start_r.json()["data"]
    ref = start_data["reference_id"]
    assert ref.startswith("KYC-BVN-")
    _dump("start.json", start_data)

    confirm_r = await client.post(
        "/api/v1/kyc/verify/confirm",
        json={"reference_id": ref},
        headers=headers,
    )
    assert confirm_r.status_code == 200
    confirm_data = confirm_r.json()["data"]
    assert confirm_data["status"] == "success"
    assert confirm_data["tier"] == 2
    assert confirm_data["verification_type"] == "bvn"
    assert confirm_data["reference"] == ref
    assert confirm_data["liveness_passed"] is True
    assert confirm_data["face_match"] is True
    assert confirm_data["failure_reason"] is None
    _dump("confirm_success.json", confirm_data)

    me_r = await client.get("/api/v1/auth/me", headers=headers)
    assert me_r.json()["data"]["kyc_level"] == 2


@pytest.mark.asyncio
async def test_capture_confirm_failed(client, db_session):
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
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert data["failure_reason"] == "face_mismatch"
    assert data["tier"] == 1
    _dump("confirm_failed.json", data)

    db_session.refresh(user)
    assert user.kyc_level == KycLevel.tier_1


@pytest.mark.asyncio
async def test_capture_status(client, db_session):
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
    # created_at varies run-to-run — assert shape only, don't bake a value.
    assert isinstance(rec["created_at"], str) and date.fromisoformat(
        rec["created_at"][:10]
    )
    _dump("status.json", data)


@pytest.mark.asyncio
async def test_capture_error_409_tier_precondition(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "KYC_TIER_PRECONDITION"
    _dump("error_409_tier_precondition.json", body)


@pytest.mark.asyncio
async def test_capture_error_422_dob_required(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _bump_tier(db_session, email="e@e.co", tier=KycLevel.tier_1, dob=None)

    r = await client.post(
        "/api/v1/kyc/verify/start",
        json={"verification_type": "bvn"},
        headers=headers,
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "DATE_OF_BIRTH_REQUIRED"
    _dump("error_422_dob_required.json", body)


@pytest.mark.asyncio
async def test_capture_error_404_unknown_reference(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.post(
        "/api/v1/kyc/verify/confirm",
        json={"reference_id": "KYC-BVN-doesnotexist"},
        headers=headers,
    )
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "UNKNOWN_REFERENCE"
    _dump("error_404_unknown_reference.json", body)


@pytest.mark.asyncio
async def test_capture_error_401_invalid_signature(client):
    r = await client.post(
        "/api/v1/kyc/webhook",
        content=b'{"reference_id": "KYC-BVN-doesnotexist"}',
        headers={"x-dojah-signature": "wrong"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "INVALID_SIGNATURE"
    _dump("error_401_invalid_signature.json", body)
