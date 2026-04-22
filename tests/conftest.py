import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.main import app
from app.db.base import Base
from app.db.session import get_db
# Import all models so metadata knows about them
from app.db.models import user, otp  # noqa: F401
from app.integrations.email.fake import FakeEmailClient


@pytest.fixture(autouse=True)
def _force_fake_providers():
    """Tests always run against fake external providers — no real HTTP to
    Paystack/Termii/etc. Individual tests that override the factory
    (e.g. test_paystack_factory) use monkeypatch, which restores after the
    test regardless of this fixture."""
    original = settings.FORCE_FAKE_PROVIDERS
    settings.FORCE_FAKE_PROVIDERS = True
    yield
    settings.FORCE_FAKE_PROVIDERS = original


# Celery runs synchronously in tests so `.delay()` calls (e.g. the
# notification dispatcher) fire their side effects before the HTTP
# request returns, and we can assert on FakeEmailClient / FakePushClient
# straight after the `client.post(...)`. The celery config is read from
# `settings.FORCE_FAKE_PROVIDERS` at worker-module import time (default
# False), so we flip both flags explicitly here regardless of when the
# module was first imported.
from app.workers.celery_app import celery_app as _celery_app
_celery_app.conf.task_always_eager = True
_celery_app.conf.task_eager_propagates = True

# Test database (file-based, used for integration client tests)
SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def db_session():
    """In-memory SQLite session for unit/service tests (no Postgres required).

    Uses StaticPool so all threads (including FastAPI's run_in_threadpool)
    share the same in-memory connection.  Without StaticPool, SQLite creates
    a fresh (empty) connection per thread and synchronous FastAPI dependencies
    like get_current_user lose the schema.
    """
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # SQLAlchemy 2.x automatically maps postgresql.UUID → CHAR(32) on SQLite,
    # and Enum types use VARCHAR with CHECK constraints, so no extra patching needed.
    Base.metadata.create_all(_engine)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        _engine.dispose()


@pytest_asyncio.fixture
async def fake_redis():
    """fakeredis async client for token store tests."""
    from fakeredis.aioredis import FakeRedis
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def token_store(fake_redis):
    """RedisTokenStore backed by fakeredis."""
    from app.services.token_store import RedisTokenStore
    return RedisTokenStore(redis=fake_redis)


# Module-level singleton so each test module shares one fake email client
# (mirrors the _fake_email_singleton pattern in deps.py)
_fake_email_module_singleton = FakeEmailClient()


@pytest.fixture
def fake_email_client():
    """FakeEmailClient singleton — cleared before each test."""
    _fake_email_module_singleton.sent.clear()
    return _fake_email_module_singleton
