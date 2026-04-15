import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.base import Base
from app.db.session import get_db
# Import all models so metadata knows about them
from app.db.models import user, otp  # noqa: F401

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
    """In-memory SQLite session for unit/service tests (no Postgres required)."""
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
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
