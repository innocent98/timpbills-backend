import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException

from app.api.deps import require_admin, require_admin_csrf
from app.db.models.admin_user import AdminUser
from app.services.admin_session_store import AdminSessionStore


class _Req:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.asyncio
async def test_require_admin_no_cookie_401(db_session):
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session=None, store=store, db=db_session)
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "ADMIN_AUTH_REQUIRED"
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_expired_session_401(db_session):
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session="ghost", store=store, db=db_session)
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "ADMIN_SESSION_EXPIRED"
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_happy_path(db_session):
    admin = AdminUser(email="a@x.com", password_hash="h", full_name="A")
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    sid = await store.create(admin_id=str(admin.id), role="superadmin")
    got = await require_admin(admin_session=sid, store=store, db=db_session)
    assert got.id == admin.id
    await redis.aclose()


@pytest.mark.asyncio
async def test_require_admin_disabled_403(db_session):
    admin = AdminUser(email="d@x.com", password_hash="h", full_name="D", is_active=False)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    sid = await store.create(admin_id=str(admin.id), role="superadmin")
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session=sid, store=store, db=db_session)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ADMIN_DISABLED"
    await redis.aclose()


def test_csrf_mismatch_403():
    with pytest.raises(HTTPException) as ei:
        require_admin_csrf(request=_Req({"X-CSRF-Token": "a"}), admin_csrf="b")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "CSRF_FAILED"


def test_csrf_header_without_cookie_403():
    with pytest.raises(HTTPException) as ei:
        require_admin_csrf(request=_Req({"X-CSRF-Token": "tok"}), admin_csrf=None)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "CSRF_FAILED"


def test_csrf_cookie_without_header_403():
    with pytest.raises(HTTPException) as ei:
        require_admin_csrf(request=_Req({}), admin_csrf="tok")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "CSRF_FAILED"


def test_csrf_match_ok():
    # returns None, raises nothing
    assert require_admin_csrf(request=_Req({"X-CSRF-Token": "tok"}), admin_csrf="tok") is None


@pytest.mark.asyncio
async def test_require_admin_malformed_session_payload_401(db_session):
    # A session blob missing "admin_id" must collapse to ADMIN_SESSION_EXPIRED,
    # not 500 — locks in the KeyError guard.
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    await redis.set("admin_session:broken", '{"role": "superadmin"}', ex=100)
    with pytest.raises(HTTPException) as ei:
        await require_admin(admin_session="broken", store=store, db=db_session)
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "ADMIN_SESSION_EXPIRED"
    await redis.aclose()
