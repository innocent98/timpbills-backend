"""API tests for POST /webhooks/vtpass.

VTPass, unlike Paystack, does not HMAC-sign webhook bodies. Auth is a
shared-secret header. These tests cover:

 • secret rejection paths (missing / wrong / server misconfigured)
 • happy path (delivered → tx success)
 • failure path (failed → tx failed + refund + wallet restored)
 • pending path (no state change; reconcile worker owns finalization)
 • partial delivery (tx success, shortfall refunded)
 • dedupe (two identical POSTs → one state change)
 • already-processed tx (late webhook doesn't double-refund)
 • unknown reference (200, no state change, event logged)
"""
import json
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
from app.core.config import settings
from app.core.limiter import limiter
from app.db.models._enums import (
    TransactionStatus,
    TransactionType,
)
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.webhook_event import WebhookEvent
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user


_test_email_client = FakeEmailClient()
_SECRET = "test-vtpass-secret-abc123"


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

    # Install a test secret so verify_vtpass_secret has something to check
    # against. The un-configured path is covered in its own test.
    _orig_secret = settings.VTPASS_WEBHOOK_SECRET
    settings.VTPASS_WEBHOOK_SECRET = _SECRET

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    settings.VTPASS_WEBHOOK_SECRET = _orig_secret
    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


# ── Helpers ──────────────────────────────────────────────────────────────


def _seed_bill_tx(
    db,
    *,
    email: str = "e@e.co",
    amount: Decimal = Decimal("500.00"),
    wallet_start: Decimal = Decimal("5000.00"),
    tx_type: TransactionType = TransactionType.airtime,
    tx_status: TransactionStatus = TransactionStatus.processing,
) -> tuple[User, Transaction]:
    """Seed a user + wallet + in-flight bill tx (processing state, wallet
    already debited by `amount`). Models the steady-state BillService
    leaves things in when the provider returns pending/transient."""
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id,
            balance=wallet_start - amount,
            balance_cap=Decimal("50000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = wallet_start - amount

    from app.utils.references import new_transaction_reference
    tx = Transaction(
        user_id=user.id,
        reference=new_transaction_reference(user_id=str(user.id)),
        type=tx_type,
        status=tx_status,
        amount=amount,
        fee=Decimal("0.00"),
        meta={"network": "MTN", "phone": "08012345678", "service_id": "mtn"},
    )
    db.add(tx)
    db.flush()
    db.add(Payment(
        transaction_id=tx.id,
        provider="wallet",
        provider_reference=tx.reference,
        status=PaymentStatus.success,
    ))
    db.commit()
    db.refresh(tx)
    return user, tx


def _vtpass_body(
    *,
    reference: str,
    code: str,
    delivered: Decimal | None = None,
    transaction_id: str = "vtp-evt-1",
) -> dict:
    """Shape the request the way VTPass actually POSTs — top-level
    requestId + code + content.transactions with transactionId and the
    delivered amount.  Matches what VTPassClient._translate expects."""
    body = {
        "code": code,
        "response_description": "TRANSACTION SUCCESSFUL" if code == "000" else "",
        "requestId": reference,
        "content": {
            "transactions": {
                "status": "delivered" if code == "000" else ("pending" if code == "099" else "failed"),
                "transactionId": transaction_id,
                "amount": str(delivered) if delivered is not None else None,
            },
        },
    }
    return body


# ── Secret rejection ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_rejects_missing_secret(client):
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=b'{"requestId":"x","code":"000"}',
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_SECRET"


@pytest.mark.asyncio
async def test_vtpass_webhook_rejects_wrong_secret(client):
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=b'{"requestId":"x","code":"000"}',
        headers={"X-VTPass-Secret": "not-the-right-secret"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_vtpass_webhook_500_when_server_secret_not_configured(client):
    # Override just for this test — the fixture's teardown restores.
    settings.VTPASS_WEBHOOK_SECRET = None
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=b'{"requestId":"x","code":"000"}',
        headers={"X-VTPass-Secret": "anything"},
    )
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "WEBHOOK_NOT_CONFIGURED"


# ── Payload shape rejection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_rejects_malformed_json(client):
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=b"this is not json",
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_vtpass_webhook_rejects_missing_request_id(client):
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=b'{"code":"000"}',
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 400


# ── Happy path: delivered ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_delivered_transitions_tx_to_success(client, db_session):
    await _seed_logged_in_user(client)
    _, tx = _seed_bill_tx(db_session, amount=Decimal("500.00"))
    body = _vtpass_body(
        reference=tx.reference, code="000", delivered=Decimal("500.00"),
    )

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ok"] is True

    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert tx2.status == TransactionStatus.success
    # No refund — full delivery.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0


# ── Failed → refund ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_failed_transitions_and_refunds(client, db_session):
    await _seed_logged_in_user(client)
    user, tx = _seed_bill_tx(
        db_session, amount=Decimal("500.00"), wallet_start=Decimal("5000.00"),
    )
    body = _vtpass_body(reference=tx.reference, code="016")  # non-success code

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200, r.text

    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert tx2.status == TransactionStatus.failed
    # Wallet restored (was debited by 500, refund credited 500).
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert wallet.balance == Decimal("5000.00")
    # Refund tx exists with the full amount.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.type == TransactionType.refund)
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("500.00")


# ── Pending → no-op ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_pending_is_noop(client, db_session):
    await _seed_logged_in_user(client)
    _, tx = _seed_bill_tx(db_session, amount=Decimal("500.00"))
    body = _vtpass_body(reference=tx.reference, code="099")

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200

    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    # Still processing — reconcile worker will requery and finalize.
    assert tx2.status == TransactionStatus.processing


# ── Partial delivery ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_partial_delivery_refunds_shortfall(client, db_session):
    await _seed_logged_in_user(client)
    user, tx = _seed_bill_tx(
        db_session, amount=Decimal("500.00"), wallet_start=Decimal("5000.00"),
    )
    # Only ₦450 of the requested ₦500 delivered.
    body = _vtpass_body(
        reference=tx.reference, code="000", delivered=Decimal("450.00"),
    )

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200

    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert tx2.status == TransactionStatus.success
    # Partial-delivery metadata on tx for the mobile banner.
    assert tx2.meta.get("partial_delivery") is True
    assert tx2.meta.get("delivered_amount_ngn") == "450.00"
    assert tx2.meta.get("shortfall_ngn") == "50.00"

    # Wallet is restored by the shortfall only (was debited 500, credited 50).
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert wallet.balance == Decimal("4550.00")

    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.type == TransactionType.refund)
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("50.00")


# ── Dedupe ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_duplicate_event_is_deduped(client, db_session):
    await _seed_logged_in_user(client)
    user, tx = _seed_bill_tx(db_session, amount=Decimal("500.00"))
    body = _vtpass_body(
        reference=tx.reference, code="000",
        delivered=Decimal("500.00"), transaction_id="vtp-evt-dupe",
    )

    r1 = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    r2 = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["ok"] is True
    assert r2.json()["data"].get("deduped") is True

    # Only one WebhookEvent row for this event.
    evts = (
        db_session.query(WebhookEvent)
        .filter(
            WebhookEvent.provider == "vtpass",
            WebhookEvent.provider_event_id == "vtp-evt-dupe",
        ).count()
    )
    assert evts == 1


# ── Already processed → no double-refund ─────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_on_already_succeeded_tx_is_skipped(client, db_session):
    """A late webhook arriving after the sync path / reconcile worker
    already settled the tx must not re-refund or re-transition."""
    await _seed_logged_in_user(client)
    user, tx = _seed_bill_tx(
        db_session, amount=Decimal("500.00"),
        tx_status=TransactionStatus.success,   # already final
    )
    body = _vtpass_body(
        reference=tx.reference, code="016",  # says failed (conflict)
        transaction_id="vtp-evt-late",
    )

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200
    assert r.json()["data"].get("already_processed") is True

    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    # State unchanged — the webhook didn't override the prior settlement.
    assert tx2.status == TransactionStatus.success
    # And no refund was issued.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0


# ── Unknown reference ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_unknown_reference_logs_and_returns_200(client, db_session):
    await _seed_logged_in_user(client)
    body = _vtpass_body(
        reference="timp-ref-never-existed", code="000",
        delivered=Decimal("500.00"), transaction_id="vtp-evt-ghost",
    )
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 200
    assert r.json()["data"].get("note") == "unknown_reference"
    # The event is still recorded for audit.
    evts = db_session.query(WebhookEvent).filter(
        WebhookEvent.provider == "vtpass",
        WebhookEvent.provider_event_id == "vtp-evt-ghost",
    ).count()
    assert evts == 1


# ── S3C-H1: defensive catches on apply_provider_result ──────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_returns_422_on_refund_kyc_cap(client, db_session):
    """S3C-H1 / S3C-P4a sibling — if the user's tier was lowered
    between the original bill debit and the VTPass webhook's refund-
    credit attempt, wallet_svc.credit raises KycCapExceeded. Webhook
    must catch + rollback + return 422 so VTPass retries on cadence
    until ops raises the tier, instead of 500 → WebhookEvent dedupe →
    refund row orphaned."""
    await _seed_logged_in_user(client)
    # Near-cap balance simulating a post-downgrade scenario. Seed with
    # ₦48k balance (= ₦5000-debited snapshot of an earlier state) and
    # new cap of ₦50k. The failure-refund of ₦5k would push to ₦53k.
    user, tx = _seed_bill_tx(
        db_session, amount=Decimal("5000.00"),
        wallet_start=Decimal("53000.00"),   # balance AFTER debit is ₦48k
    )
    # Tighten the cap (simulating ops lowering the user's tier).
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    wallet.balance_cap = Decimal("50000.00")
    db_session.commit()

    body = _vtpass_body(reference=tx.reference, code="016")  # failed

    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _SECRET},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "KYC_LIMIT_EXCEEDED"

    # No state leaked: tx still in its pre-webhook state, wallet
    # unchanged, no refund row committed.
    db_session.expire_all()
    tx2 = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert tx2.status == TransactionStatus.processing
    wallet = db_session.query(Wallet).filter(Wallet.user_id == user.id).one()
    assert wallet.balance == Decimal("48000.00")
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0
