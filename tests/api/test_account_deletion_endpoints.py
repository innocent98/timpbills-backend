import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.api.deps import get_db, get_token_store
from app.core.limiter import limiter
from app.core.security import hash_password
from app.db.models.user import User
from app.db.models.wallet import Wallet


class _FakeTokenStore:
    async def revoke_all(self, *, user_id): pass


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    import app.services.account_deletion_service as mod
    monkeypatch.setattr(mod, "dispatch_delay", lambda **kw: None)
    def _get_db():
        yield db_session
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = lambda: _FakeTokenStore()
    limiter.enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()
    limiter.enabled = True


def _seed(db, *, pw="Secret123", email="a@b.co", phone="+2348100000021", balance="0.00"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="A",
             password_hash=hash_password(pw), is_active=True)
    db.add(u); db.flush()
    db.add(Wallet(id=uuid.uuid4(), user_id=u.id, balance=Decimal(balance),
                  balance_cap=Decimal("50000.00")))
    db.commit()
    return u


@pytest.mark.asyncio
async def test_deletion_request_success(db_session, client):
    _seed(db_session)
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "a@b.co", "password": "Secret123"})
    assert r.status_code == 200
    assert "scheduled_deletion_at" in r.json()["data"]


@pytest.mark.asyncio
async def test_deletion_request_bad_password_401(db_session, client):
    _seed(db_session)
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "a@b.co", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_deletion_request_nonzero_balance_409(db_session, client):
    _seed(db_session, email="c@d.co", phone="+2348100000022", balance="500.00")
    r = await client.post("/api/v1/account/deletion-request",
                          json={"identifier": "c@d.co", "password": "Secret123"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WALLET_NOT_EMPTY"


@pytest.mark.asyncio
async def test_cancel_after_request(db_session, client):
    _seed(db_session, email="e@f.co", phone="+2348100000023")
    await client.post("/api/v1/account/deletion-request",
                      json={"identifier": "e@f.co", "password": "Secret123"})
    r = await client.post("/api/v1/account/deletion-request/cancel",
                          json={"identifier": "e@f.co", "password": "Secret123"})
    assert r.status_code == 200 and r.json()["data"]["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_without_pending_deletion_409(db_session, client):
    # No deletion-request was ever made for this account.
    _seed(db_session, email="g@h.co", phone="+2348100000024")
    r = await client.post("/api/v1/account/deletion-request/cancel",
                          json={"identifier": "g@h.co", "password": "Secret123"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NOT_PENDING_DELETION"
