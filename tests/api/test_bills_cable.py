"""API-level tests for /bills/cable/* — Sprint 4 B12 + B13.

B12 covers GET /bills/cable/providers and POST /bills/cable/validate-smartcard.
B13 covers GET /bills/cable/plans (renew|change) and POST /bills/cable
with pin + idem."""
from decimal import Decimal
from uuid import uuid4

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
    reset_fake_paystack,
    reset_fake_sms,
    reset_fake_vtpass,
)
from app.core.limiter import limiter
from app.db.models._enums import TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.email.fake import FakeEmailClient
from app.integrations.vtpass import factory as _vtpass_factory
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
    reset_fake_paystack()
    reset_fake_vtpass()
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


async def _pin_token(client, headers) -> str:
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    return r.json()["data"]["pin_token"]


def _fund_wallet_directly(db, *, email="e@e.co", amount=Decimal("50000.00")) -> User:
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id, balance=amount,
            balance_cap=Decimal("200000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = amount
        wallet.balance_cap = Decimal("200000.00")
    db.commit()
    return user


async def _validate_smartcard(client, headers, *, service_id="dstv",
                              smartcard_number="1234567890"):
    """Prime the SmartcardValidation cache for a subsequent renew purchase."""
    r = await client.post(
        "/api/v1/bills/cable/validate-smartcard",
        json={"service_id": service_id, "smartcard_number": smartcard_number},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ── 1. Cable providers catalog ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cable_providers_returns_four_static_entries(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.get("/api/v1/bills/cable/providers", headers=headers)
    assert r.status_code == 200, r.text
    providers = r.json()["data"]["providers"]
    ids = [p["id"] for p in providers]
    assert set(ids) == {"dstv", "gotv", "startimes", "showmax"}
    # Every provider has a display name
    assert all(p["name"] for p in providers)


@pytest.mark.asyncio
async def test_cable_providers_requires_auth(client):
    r = await client.get("/api/v1/bills/cable/providers")
    assert r.status_code == 401


# ── 2. Validate smartcard — happy ───────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_happy_returns_customer_and_plan(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.post(
        "/api/v1/bills/cable/validate-smartcard",
        json={"service_id": "dstv", "smartcard_number": "1234567890"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "dstv"
    assert data["smartcard_number"] == "1234567890"
    # FakeVTPassClient formula: FAKE SUBSCRIBER + last-4 of smartcard_number.
    assert data["customer_name"] == "FAKE SUBSCRIBER 7890"
    assert data["status"] == "active"
    assert data["current_plan_name"]
    assert data["current_plan_code"]


# ── 3. Validate smartcard — poisoned → 400 INVALID_SMARTCARD ───────────


@pytest.mark.asyncio
async def test_validate_smartcard_invalid_returns_400(client):
    _, headers = await _seed_logged_in_user(client)

    fake = _vtpass_factory.get_fake_singleton()
    fake.will_reject_smartcard("dstv", "0000000000")
    try:
        r = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json={"service_id": "dstv", "smartcard_number": "0000000000"},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        err = r.json()["error"]
        assert err["code"] == "INVALID_SMARTCARD"
        # Sprint 4 B21: raw exception detail (service_id, smartcard,
        # upstream VTPass desc) MUST NOT leak to API consumers. Verify
        # the smartcard is NOT echoed back and the generic copy is used.
        assert "0000000000" not in err["message"]
        assert "dstv" not in err["message"].lower()
        assert "vtpass" not in err["message"].lower()
        assert "smartcard number could not be validated" in err["message"].lower()
    finally:
        fake._rejected_smartcards.discard(("dstv", "0000000000"))


# ── 4. Cache hit: second call doesn't touch the provider ───────────────


@pytest.mark.asyncio
async def test_validate_smartcard_cache_hits_on_repeat_request(client):
    _, headers = await _seed_logged_in_user(client)

    fake = _vtpass_factory.get_fake_singleton()
    original_validate = fake.validate_smartcard
    call_count = {"n": 0}

    async def counting_validate(**kw):
        call_count["n"] += 1
        return await original_validate(**kw)
    fake.validate_smartcard = counting_validate  # type: ignore[method-assign]

    payload = {"service_id": "gotv", "smartcard_number": "9988776655"}
    try:
        r1 = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json=payload, headers=headers,
        )
        r2 = await client.post(
            "/api/v1/bills/cable/validate-smartcard",
            json=payload, headers=headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["data"] == r2.json()["data"]
        # Provider hit once — second request served from the 5-minute
        # per-user Redis cache keyed on (user, service, smartcard).
        assert call_count["n"] == 1
    finally:
        fake.validate_smartcard = original_validate  # type: ignore[method-assign]


# ── 5. Length-bounded smartcard_number rejected at schema layer ────────


@pytest.mark.asyncio
async def test_validate_smartcard_rejects_empty_smartcard(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.post(
        "/api/v1/bills/cable/validate-smartcard",
        json={"service_id": "dstv", "smartcard_number": ""},
        headers=headers,
    )
    assert r.status_code == 422


# ══ B13: cable plans + purchase ════════════════════════════════════════


# ── 6. GET cable plans (change mode) ────────────────────────────────────


@pytest.mark.asyncio
async def test_cable_plans_change_mode_returns_full_catalog(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.get(
        "/api/v1/bills/cable/plans?provider=dstv&mode=change",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "dstv"
    codes = [p["variation_code"] for p in data["plans"]]
    assert "dstv-compact" in codes
    assert "dstv-premium" in codes


# ── 7. GET cable plans (renew mode) — same full catalog ────────────────


@pytest.mark.asyncio
async def test_cable_plans_renew_mode_returns_full_catalog(client):
    """Renew mode returns the full catalog too; the mobile UI filters
    by current_plan_code from the cached smartcard validation. See
    docstring on BillService.list_cable_plans for rationale."""
    _, headers = await _seed_logged_in_user(client)

    r = await client.get(
        "/api/v1/bills/cable/plans?provider=gotv&mode=renew",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["service_id"] == "gotv"
    assert len(data["plans"]) >= 2


# ── 8. GET cable plans — bad mode → 400 ────────────────────────────────


@pytest.mark.asyncio
async def test_cable_plans_invalid_mode_returns_400(client):
    _, headers = await _seed_logged_in_user(client)

    r = await client.get(
        "/api/v1/bills/cable/plans?provider=dstv&mode=bogus",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CABLE_MODE"


# ── 9. POST /bills/cable — change mode happy path ──────────────────────


def _purchase_payload(**overrides):
    base = {
        "service_id":       "dstv",
        "smartcard_number": "1234567890",
        "mode":             "change",
        "variation_code":   "dstv-premium",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_cable_change_mode_happy_path(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "success"
    assert data["service_id"] == "dstv"
    assert data["mode"] == "change"
    assert data["plan_code"] == "dstv-premium"
    # Server-resolved price from the catalog, not the client.
    assert Decimal(data["amount"]) == Decimal("44500.00")


# ── 10. POST /bills/cable — change mode unknown plan → 400 ─────────────


@pytest.mark.asyncio
async def test_cable_change_unknown_plan_returns_400(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(variation_code="dstv-imaginary"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_CABLE_PLAN"


# ── 11. POST /bills/cable — change without variation_code → 400 ────────


@pytest.mark.asyncio
async def test_cable_change_without_variation_code_returns_400(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(variation_code=None),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VARIATION_CODE_REQUIRED"


# ── 12. POST /bills/cable — renew mode happy path ──────────────────────


@pytest.mark.asyncio
async def test_cable_renew_mode_happy_path(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    # Prime the SmartcardValidation cache.
    await _validate_smartcard(client, headers, service_id="dstv",
                              smartcard_number="1234567890")

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(mode="renew", variation_code=None),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "success"
    assert data["mode"] == "renew"
    # FakeVTPassClient's validate_smartcard pins the current plan to
    # the first seeded variation for the service (dstv → dstv-compact
    # at 15500.00), so the renew purchase executes at that price.
    assert data["plan_code"] == "dstv-compact"
    assert Decimal(data["amount"]) == Decimal("15500.00")


# ── 13. POST /bills/cable — renew without prior validation → 409 ───────


@pytest.mark.asyncio
async def test_cable_renew_without_cache_returns_409(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(mode="renew", variation_code=None),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CABLE_RENEWAL_UNAVAILABLE"


# ── 14. POST /bills/cable — insufficient balance → 402 ─────────────────


@pytest.mark.asyncio
async def test_cable_insufficient_balance_returns_402(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("1000.00"))
    pin = await _pin_token(client, headers)

    r = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"


# ── 15. POST /bills/cable — idempotency replay ─────────────────────────


@pytest.mark.asyncio
async def test_cable_idempotency_replay_returns_cached_response(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)
    idem = str(uuid4())

    r1 = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(variation_code="dstv-access"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    r2 = await client.post(
        "/api/v1/bills/cable",
        json=_purchase_payload(variation_code="dstv-access"),
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": idem},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["reference"] == r2.json()["data"]["reference"]

    user = db_session.query(User).filter(User.email == "e@e.co").one()
    db_session.expire_all()
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    # Debited exactly once for 9000.00 (dstv-access price).
    assert wallet.balance == Decimal("50000.00") - Decimal("9000.00")


# ── 16. POST /bills/cable — provider failure triggers refund ───────────


@pytest.mark.asyncio
async def test_cable_failure_refund_is_applied(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_cable

    async def always_fail(**kw):
        fake.will_fail(kw["request_id"])
        return await original_purchase(**kw)
    fake.purchase_cable = always_fail  # type: ignore[method-assign]

    try:
        r = await client.post(
            "/api/v1/bills/cable",
            json=_purchase_payload(variation_code="dstv-access"),
            headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"

        user = db_session.query(User).filter(User.email == "e@e.co").one()
        db_session.expire_all()
        wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
        assert wallet.balance == Decimal("50000.00")  # refunded

        refunds = (
            db_session.query(Transaction)
            .filter(
                Transaction.user_id == user.id,
                Transaction.type == TransactionType.refund,
            ).all()
        )
        assert len(refunds) == 1
        assert refunds[0].amount == Decimal("9000.00")
    finally:
        fake.purchase_cable = original_purchase  # type: ignore[method-assign]
