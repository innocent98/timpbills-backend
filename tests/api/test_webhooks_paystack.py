import json
from uuid import uuid4

import pytest
import pytest_asyncio
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


async def _init_funding(client, headers, amount="5000.00") -> str:
    """Return Paystack reference."""
    pin_r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    pin_token = pin_r.json()["data"]["pin_token"]
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": amount},
        headers={
            **headers,
            "X-Pin-Token": pin_token,
            "Idempotency-Key": str(uuid4()),
        },
    )
    return r.json()["data"]["reference"]


@pytest.mark.asyncio
async def test_rejects_bad_signature(client):
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=b'{"event":"charge.success","data":{"id":"1","reference":"x"}}',
        headers={"x-paystack-signature": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_charge_success_credits_wallet(client):
    _, headers = await _seed_logged_in_user(client)
    ref = await _init_funding(client, headers, amount="5000.00")

    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)

    body = {"event": "charge.success", "data": {"id": "evt_1", "reference": ref}}
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 200

    # Balance should now reflect credit
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"


@pytest.mark.asyncio
async def test_duplicate_event_is_deduped(client):
    _, headers = await _seed_logged_in_user(client)
    ref = await _init_funding(client, headers, amount="5000.00")

    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)

    body = {"event": "charge.success", "data": {"id": "evt_dup", "reference": ref}}
    r1 = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    r2 = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Wallet credited exactly once
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "5000.00"


@pytest.mark.asyncio
async def test_charge_failed_on_funding_does_not_refund(client):
    """Funding failure must NOT mint balance. Refunds only apply to outbound
    tx types (airtime/data/etc. — Sprint 3+) where the user was actually
    debited before the provider call."""
    _, headers = await _seed_logged_in_user(client)
    ref = await _init_funding(client, headers, amount="5000.00")

    body = {"event": "charge.failed", "data": {"id": "evt_fail", "reference": ref}}
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 200

    # Original tx is failed; no refund row created.
    list_r = await client.get("/api/v1/transactions", headers=headers)
    items = list_r.json()["data"]["items"]

    original = next((i for i in items if i["reference"] == ref), None)
    assert original is not None
    assert original["status"] == "failed"

    refunds = [i for i in items if i["type"] == "refund"]
    assert refunds == []

    # Wallet balance stays at zero — no free credit.
    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "0.00"


@pytest.mark.asyncio
async def test_charge_success_over_kyc_cap_does_not_commit_partial_state(
    db_session, client
):
    """Regression for S2C-2 (M9): a charge.success that would push the wallet
    over the user's KYC cap must not commit partial state.

    Specifically: the Payment row must stay in `pending` (so a later retry /
    reconcile can still act on it), the wallet balance must stay at its
    pre-webhook value, and no refund row is introduced. The webhook returning
    5xx is acceptable here — S2C-8 will upgrade this to a dead-letter with a
    distinct error code so Paystack stops retrying.
    """
    from decimal import Decimal

    from app.db.models.user import User
    from app.db.models.wallet import Wallet

    _tokens, headers = await _seed_logged_in_user(client)

    # Seed wallet near tier-0 cap (₦50,000). A ₦5,000 funding will overshoot.
    user_row = db_session.query(User).filter(User.email == "e@e.co").one()
    existing_wallet = (
        db_session.query(Wallet).filter(Wallet.user_id == user_row.id).first()
    )
    if existing_wallet is None:
        existing_wallet = Wallet(
            user_id=user_row.id,
            balance=Decimal("46000.00"),
            balance_cap=Decimal("50000.00"),
        )
        db_session.add(existing_wallet)
    else:
        existing_wallet.balance = Decimal("46000.00")
    db_session.commit()

    ref = await _init_funding(client, headers, amount="5000.00")
    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(ref)

    body = {"event": "charge.success", "data": {"id": "evt_cap", "reference": ref}}
    # S2C-8 behaviour: the webhook catches KycCapExceeded and returns 422
    # with a distinct error code so Paystack stops retrying (4xx ≠ retry),
    # ops can triage, and no partial state lands in the DB.
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "KYC_LIMIT_EXCEEDED"

    # Wallet balance unchanged — no partial credit committed.
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user_row.id).one()
    assert w.balance == Decimal("46000.00")

    # No refund tx was created.
    list_r = await client.get("/api/v1/transactions", headers=headers)
    items = list_r.json()["data"]["items"]
    refunds = [i for i in items if i["type"] == "refund"]
    assert refunds == []

    # Original tx still in pending/processing — not prematurely flipped to success.
    detail = await client.get(f"/api/v1/transactions/{ref}", headers=headers)
    assert detail.json()["data"]["status"] in ("pending", "processing")


@pytest.mark.asyncio
async def test_charge_failed_on_outbound_tx_issues_refund(db_session, client):
    """For outbound tx types (airtime/data/cable/electricity/flight) a failed
    charge means the user was debited from their wallet but the service was
    not delivered — so we create a refund row and credit the wallet.

    Uses a synthetic airtime transaction seeded directly into the DB, since
    the airtime feature itself doesn't ship until Sprint 3.
    """
    from decimal import Decimal
    from uuid import uuid4 as _uuid
    from app.db.models.transaction import Transaction
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.payment import Payment, PaymentStatus
    from app.db.models.user import User

    _tokens, headers = await _seed_logged_in_user(client)
    user_row = db_session.query(User).filter(User.email == "e@e.co").one()
    user_id = user_row.id

    # Seed an outbound airtime tx in pending status with a Payment row.
    ref = f"TMP-airtime-{_uuid().hex[:8]}"
    tx = Transaction(
        user_id=user_id, reference=ref,
        type=TransactionType.airtime, status=TransactionStatus.pending,
        amount=Decimal("1000.00"), fee=Decimal("0.00"), currency="NGN",
        meta={"network": "MTN", "phone": "08012345678"},
    )
    db_session.add(tx)
    db_session.flush()
    payment = Payment(
        transaction_id=tx.id, provider="paystack",
        provider_reference=ref, status=PaymentStatus.pending,
    )
    db_session.add(payment)
    db_session.commit()

    body = {"event": "charge.failed", "data": {"id": "evt_outbound_fail", "reference": ref}}
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 200

    list_r = await client.get("/api/v1/transactions", headers=headers)
    items = list_r.json()["data"]["items"]
    refunds = [i for i in items if i["type"] == "refund"]
    assert len(refunds) == 1
    assert refunds[0]["amount"] == "1000.00"
    assert refunds[0]["status"] == "success"

    w = await client.get("/api/v1/wallet", headers=headers)
    assert w.json()["data"]["balance"] == "1000.00"


@pytest.mark.asyncio
async def test_charge_failed_refund_over_kyc_cap_returns_422(db_session, client):
    """S3C-P4a regression — if the user's tier was LOWERED between the
    original wallet debit and the charge.failed refund attempt, the
    refund credit would overshoot the new cap. The webhook must catch
    KycCapExceeded, roll back, and return 422 so Paystack keeps retrying
    on its cadence. If we let the exception bubble as 500, Paystack's
    WebhookEvent dedupe swallows the retry and the refund row stays
    orphaned — user silently without their money.
    """
    from decimal import Decimal
    from uuid import uuid4 as _uuid
    from app.db.models.transaction import Transaction
    from app.db.models._enums import TransactionStatus, TransactionType
    from app.db.models.payment import Payment, PaymentStatus
    from app.db.models.user import User
    from app.db.models.wallet import Wallet

    _tokens, headers = await _seed_logged_in_user(client)
    user_row = db_session.query(User).filter(User.email == "e@e.co").one()
    user_id = user_row.id

    # Seed a wallet near its cap (simulating an ops-initiated tier downgrade
    # AFTER the original debit — balance is already elevated relative to
    # the new cap).
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user_id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user_id,
            balance=Decimal("48000.00"),
            balance_cap=Decimal("50000.00"),
        )
        db_session.add(wallet)
    else:
        wallet.balance = Decimal("48000.00")
        wallet.balance_cap = Decimal("50000.00")
    db_session.commit()

    # Original outbound tx: ₦5000 airtime that was debited from the wallet
    # (balance was ₦53000 pre-debit under the old ₦200000 cap).
    ref = f"TMP-airtime-{_uuid().hex[:8]}"
    tx = Transaction(
        user_id=user_id, reference=ref,
        type=TransactionType.airtime, status=TransactionStatus.pending,
        amount=Decimal("5000.00"), fee=Decimal("0.00"), currency="NGN",
        meta={"network": "MTN", "phone": "08012345678"},
    )
    db_session.add(tx)
    db_session.flush()
    db_session.add(Payment(
        transaction_id=tx.id, provider="paystack",
        provider_reference=ref, status=PaymentStatus.pending,
    ))
    db_session.commit()

    # Refund of ₦5000 would put balance at ₦53000 > ₦50000 cap → KycCapExceeded.
    body = {"event": "charge.failed", "data": {"id": "evt_kyc_refund", "reference": ref}}
    r = await client.post(
        "/api/v1/webhooks/paystack",
        content=json.dumps(body).encode(),
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "KYC_LIMIT_EXCEEDED"

    # Balance unchanged (no partial credit), no refund row committed.
    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == user_id).one()
    assert w.balance == Decimal("48000.00")

    list_r = await client.get("/api/v1/transactions", headers=headers)
    items = list_r.json()["data"]["items"]
    refunds = [i for i in items if i["type"] == "refund"]
    assert refunds == []
