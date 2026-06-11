"""Shared admin-API test fixtures.

Defined once here so Tasks 8–15 (admin overview / transactions / users /
refunds / notifications / requery) all authenticate the same way via
``login_admin`` instead of re-deriving the cookie dance per module.

The ``db_session`` fixture (in-memory SQLite, StaticPool) lives in the
root ``tests/conftest.py``; these fixtures layer the admin auth surface
on top of it.
"""
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db, get_redis
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.main import app
from app.services.admin_session_store import AdminSessionStore


@pytest_asyncio.fixture
async def admin_ctx(db_session):
    """An AsyncClient wired to the test DB + a fake Redis, limiter disabled.

    Yields ``(client, db_session, fake_redis)`` so tests can seed rows and
    inspect Redis directly. Overrides are torn down on exit.
    """
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _get_redis
    limiter.enabled = False
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, db_session, fake_redis
    finally:
        # Teardown must run even if a test raises inside the yield, or the
        # disabled limiter + dependency overrides would leak into later tests.
        limiter.enabled = True
        await fake_redis.aclose()
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(admin_ctx):
    """Just the AsyncClient half of ``admin_ctx`` for tests that don't need
    to touch the DB/Redis handles directly."""
    c, _db, _r = admin_ctx
    return c


@pytest_asyncio.fixture
async def login_admin(admin_ctx):
    """Seed an AdminUser + a live session, attach the session + csrf cookies
    to the shared client. Returns the csrf token to echo in ``X-CSRF-Token``
    on write endpoints.

    Uses the real ``AdminSessionStore`` against the fake Redis so the
    server-side ``require_admin`` lookup resolves the session the same way
    production would.
    """
    c, db, fake_redis = admin_ctx

    async def _login(*, email="ops@x.com"):
        admin = AdminUser(
            email=email, password_hash=hash_password("pw"), full_name="Ops"
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        store = AdminSessionStore(
            redis=fake_redis, ttl_seconds=settings.ADMIN_SESSION_TTL_SECONDS
        )
        sid = await store.create(admin_id=str(admin.id), role=admin.role.value)
        csrf = "csrf-test-token"
        c.cookies.set(settings.ADMIN_SESSION_COOKIE_NAME, sid)
        c.cookies.set(settings.ADMIN_CSRF_COOKIE_NAME, csrf)
        return csrf

    return _login
