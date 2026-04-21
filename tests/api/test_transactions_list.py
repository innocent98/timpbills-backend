from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    reset_fake_sms,
    reset_fake_email,
    reset_fake_paystack,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user

_test_email_client = FakeEmailClient()


@pytest.fixture
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
    reset_fake_paystack()
    _test_email_client.sent.clear()

    # Patch the e2e module's email client so _seed_logged_in_user reads our emails
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


@pytest.mark.asyncio
async def test_transactions_list_empty_for_new_user(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/transactions", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_transactions_list_shows_pending_after_fund(client):
    _, headers = await _seed_logged_in_user(client)
    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    pin_token = pin_r.json()["data"]["pin_token"]
    await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "1500.00"},
        headers={**headers, "X-Pin-Token": pin_token, "Idempotency-Key": str(uuid4())},
    )
    r = await client.get("/api/v1/transactions", headers=headers)
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["type"] == "wallet_funding"
    assert items[0]["status"] in ("pending", "processing")


@pytest.mark.asyncio
async def test_get_transaction_by_reference(client):
    _, headers = await _seed_logged_in_user(client)
    pin_r = await client.post("/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers)
    pin_token = pin_r.json()["data"]["pin_token"]
    fund_r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "500.00"},
        headers={**headers, "X-Pin-Token": pin_token, "Idempotency-Key": str(uuid4())},
    )
    ref = fund_r.json()["data"]["reference"]
    r = await client.get(f"/api/v1/transactions/{ref}", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["reference"] == ref


@pytest.mark.asyncio
async def test_get_unknown_transaction_returns_404(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get("/api/v1/transactions/TMP-NOPE", headers=headers)
    assert r.status_code == 404


# ── Server-side filtering (S2C-5) ────────────────────────────────────────


async def _seed_mixed_transactions(client, headers, db_session):
    """Insert a mix of tx types so filter tests have something to filter."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from uuid import uuid4 as _uuid
    from app.db.models.transaction import Transaction
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.user import User

    user_row = db_session.query(User).filter(User.email == "e@e.co").one()

    now = datetime.now(timezone.utc)
    rows = [
        (TransactionType.wallet_funding, TransactionStatus.success, now - timedelta(days=1)),
        (TransactionType.airtime,        TransactionStatus.success, now - timedelta(days=5)),
        (TransactionType.electricity,    TransactionStatus.failed,  now - timedelta(days=10)),
        (TransactionType.refund,         TransactionStatus.success, now - timedelta(days=10)),
        (TransactionType.flight,         TransactionStatus.pending, now - timedelta(days=15)),
    ]
    for ttype, status, created in rows:
        t = Transaction(
            user_id=user_row.id,
            reference=f"TMP-{ttype.value}-{_uuid().hex[:6]}",
            type=ttype, status=status,
            amount=Decimal("1000.00"), fee=Decimal("0.00"), currency="NGN",
            created_at=created,
        )
        db_session.add(t)
    db_session.commit()


@pytest.mark.asyncio
async def test_filter_by_single_type(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    await _seed_mixed_transactions(client, headers, db_session)
    r = await client.get(
        "/api/v1/transactions?type=airtime", headers=headers
    )
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["type"] == "airtime"


@pytest.mark.asyncio
async def test_filter_by_multiple_types(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    await _seed_mixed_transactions(client, headers, db_session)
    r = await client.get(
        "/api/v1/transactions?type=airtime&type=electricity&type=cable",
        headers=headers,
    )
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert sorted(i["type"] for i in items) == ["airtime", "electricity"]


@pytest.mark.asyncio
async def test_filter_by_status(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    await _seed_mixed_transactions(client, headers, db_session)
    r = await client.get(
        "/api/v1/transactions?status=failed", headers=headers
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_filter_by_date_range(client, db_session):
    from datetime import datetime, timedelta, timezone
    _, headers = await _seed_logged_in_user(client)
    await _seed_mixed_transactions(client, headers, db_session)

    # Last week only — keeps the wallet_funding (day 1) and airtime (day 5).
    date_from = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    r = await client.get(
        "/api/v1/transactions",
        headers=headers,
        params={"date_from": date_from},
    )
    assert r.status_code == 200, r.text
    items = r.json()["data"]["items"]
    assert sorted(i["type"] for i in items) == ["airtime", "wallet_funding"]


@pytest.mark.asyncio
async def test_filter_unknown_type_returns_400(client):
    _, headers = await _seed_logged_in_user(client)
    r = await client.get(
        "/api/v1/transactions?type=bogus", headers=headers
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_TX_TYPE"


@pytest.mark.asyncio
async def test_pagination_exposes_total_and_honours_offset(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    await _seed_mixed_transactions(client, headers, db_session)

    page1 = await client.get(
        "/api/v1/transactions?limit=2&offset=0", headers=headers
    )
    assert page1.json()["data"]["total"] == 5
    assert len(page1.json()["data"]["items"]) == 2

    page2 = await client.get(
        "/api/v1/transactions?limit=2&offset=2", headers=headers
    )
    assert len(page2.json()["data"]["items"]) == 2

    # No overlap between pages.
    refs1 = {i["reference"] for i in page1.json()["data"]["items"]}
    refs2 = {i["reference"] for i in page2.json()["data"]["items"]}
    assert refs1.isdisjoint(refs2)
