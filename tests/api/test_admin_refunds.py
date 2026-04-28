"""API-level tests for /admin/refunds/{reference}/trigger — Sprint 5 BE-52.

Covers the four contract corners:

  1. Auth required → 401 without bearer.
  2. Authenticated but non-admin → 403.
  3. Admin can trigger a refund on a failed bill tx → wallet credited,
     audit row written, original tx walks to `refunded`.
  4. Re-triggering an already-refunded tx → 200, no double credit,
     `was_created=false`, audit row still written.

Sprint 8 will build the admin dashboard UI on top of this; we only ship
the API surface here.
"""
from decimal import Decimal

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
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore
from app.utils.references import new_transaction_reference

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


def _make_admin(db, email: str) -> User:
    """Promote an existing user (created via _seed_logged_in_user) to admin."""
    user = db.query(User).filter(User.email == email).one()
    user.is_admin = True
    db.commit()
    return user


def _seed_failed_bill_tx(db, user_id, *, amount: Decimal = Decimal("1500.00")) -> Transaction:
    """Insert a `failed` airtime tx + ensure the user has a wallet so a
    refund credit doesn't crash on missing-row. Skips the BillService
    orchestration — the admin endpoint contract is independent of how
    the tx got into the failed state."""
    if db.query(Wallet).filter(Wallet.user_id == user_id).first() is None:
        db.add(Wallet(
            user_id=user_id, balance=Decimal("0.00"),
            balance_cap=Decimal("200000.00"),
        ))
    tx = Transaction(
        user_id=user_id,
        reference=new_transaction_reference(user_id=str(user_id)),
        type=TransactionType.airtime,
        status=TransactionStatus.failed,
        amount=amount,
        fee=Decimal("0.00"),
        meta={"phone": "08011111111", "service_id": "mtn"},
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


# ── 1. Auth contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_unauthenticated_rejects_401(client):
    r = await client.post(
        "/api/v1/admin/refunds/TMP-260428-0001/trigger",
        json={"reason": "Customer reported never received airtime"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_refund_non_admin_rejects_403(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    tx = _seed_failed_bill_tx(db_session, db_session.query(User).one().id)

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Customer reported never received airtime"},
        headers=headers,
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ADMIN_REQUIRED"


# ── 2. Happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_credits_wallet_and_walks_tx_to_refunded(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    admin = _make_admin(db_session, "e@e.co")
    tx = _seed_failed_bill_tx(db_session, admin.id, amount=Decimal("1500.00"))

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Customer reported never received airtime"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["was_created"] is True
    assert body["refund_reference"]
    assert body["refund_amount"] == "1500.00"
    assert body["transaction_status"] == "refunded"

    # Wallet credited by the refund amount.
    wallet = db_session.query(Wallet).filter(Wallet.user_id == admin.id).one()
    assert wallet.balance == Decimal("1500.00")

    # Refund row exists, linked back to the original via meta.
    refund = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == admin.id,
            Transaction.type == TransactionType.refund,
        )
        .one()
    )
    assert refund.meta["original_reference"] == tx.reference
    assert refund.amount == Decimal("1500.00")

    # Audit row on the original tx records the admin actor + reason.
    audit_rows = (
        db_session.query(TransactionEvent)
        .filter(TransactionEvent.transaction_id == tx.id)
        .all()
    )
    admin_attempts = [
        e for e in audit_rows
        if e.reason and e.reason.startswith("admin_manual_refund_attempt:")
    ]
    assert len(admin_attempts) == 1
    ctx = admin_attempts[0].context
    assert ctx["actor_admin_user_id"] == str(admin.id)
    assert ctx["actor_admin_email"] == "e@e.co"
    assert ctx["refund_was_created"] is True


# ── 3. Idempotency ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_re_trigger_is_noop_with_audit_row(client, db_session):
    """Second admin trigger after the refund has already landed: no
    double-credit, returns was_created=false, but still writes an audit
    row so 'Adebayo retried this' is reconstructable from history."""
    _, headers = await _seed_logged_in_user(client)
    admin = _make_admin(db_session, "e@e.co")
    tx = _seed_failed_bill_tx(db_session, admin.id, amount=Decimal("2500.00"))

    # First trigger — creates refund.
    r1 = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "First refund — customer disputed"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["was_created"] is True

    wallet_after_first = (
        db_session.query(Wallet).filter(Wallet.user_id == admin.id).one().balance
    )
    assert wallet_after_first == Decimal("2500.00")

    # Second trigger — should be a no-op on the wallet but still 200.
    r2 = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "Re-checking after escalation"},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()["data"]
    assert body["was_created"] is False
    assert body["refund_reference"] == r1.json()["data"]["refund_reference"]

    # Wallet not double-credited.
    db_session.expire_all()
    wallet_after_second = (
        db_session.query(Wallet).filter(Wallet.user_id == admin.id).one().balance
    )
    assert wallet_after_second == Decimal("2500.00")

    # Both audit rows written — one per admin attempt.
    admin_attempts = (
        db_session.query(TransactionEvent)
        .filter(
            TransactionEvent.transaction_id == tx.id,
            TransactionEvent.reason.like("admin_manual_refund_attempt%"),
        )
        .all()
    )
    assert len(admin_attempts) == 2
    reasons = sorted(e.reason for e in admin_attempts)
    assert "First refund" in reasons[0]
    assert "Re-checking" in reasons[1]


# ── 4. Negative paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_refund_unknown_tx_404(client, db_session):
    _, headers = await _seed_logged_in_user(client)
    _make_admin(db_session, "e@e.co")

    r = await client.post(
        "/api/v1/admin/refunds/TMP-999999-NONE/trigger",
        json={"reason": "Looking for a tx that doesn't exist"},
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_admin_refund_rejects_refund_type_tx(client, db_session):
    """A refund can't itself be refunded — that would be a state-machine
    violation. The endpoint must reject before touching any wallet
    state."""
    _, headers = await _seed_logged_in_user(client)
    admin = _make_admin(db_session, "e@e.co")

    # Insert a refund-type tx directly.
    refund_tx = Transaction(
        user_id=admin.id,
        reference=new_transaction_reference(user_id=str(admin.id), prefix="TMPR"),
        type=TransactionType.refund,
        status=TransactionStatus.success,
        amount=Decimal("500.00"),
        fee=Decimal("0.00"),
        meta={"original_reference": "TMP-260428-0099"},
    )
    db_session.add(refund_tx)
    db_session.commit()

    r = await client.post(
        f"/api/v1/admin/refunds/{refund_tx.reference}/trigger",
        json={"reason": "Operator typo — tried to refund a refund"},
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNREFUNDABLE_TX_TYPE"


@pytest.mark.asyncio
async def test_admin_refund_rejects_short_reason(client, db_session):
    """Reason has min_length=3 — a single-character or empty reason
    gives ops nothing useful to read in the audit log later."""
    _, headers = await _seed_logged_in_user(client)
    admin = _make_admin(db_session, "e@e.co")
    tx = _seed_failed_bill_tx(db_session, admin.id)

    r = await client.post(
        f"/api/v1/admin/refunds/{tx.reference}/trigger",
        json={"reason": "x"},
        headers=headers,
    )
    assert r.status_code == 422
