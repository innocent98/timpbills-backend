"""API-level tests for POST /admin/transactions/{reference}/requery — Task 15.

The on-demand twin of the periodic reconcile sweep. Re-polls the provider
for a STUCK (pending/processing) transaction and applies the resolved
outcome through the EXACT same machinery the reconcile tasks use:

  * bill txs (airtime/data/electricity/cable) → ``BillProvider.requery``
    + ``BillService.apply_provider_result`` (mirrors ``_reconcile_bills``);
  * wallet_funding txs → Paystack ``verify`` on the linked Payment +
    wallet credit / transition (mirrors ``_reconcile``).

Auth model identical to the refund-trigger endpoint (Task 6): opaque
session cookie + double-submit CSRF, ``require_admin`` resolving BEFORE
``require_admin_csrf`` so an unauthenticated request is 401 not 403.

The acting actor is an ``AdminUser`` (via ``login_admin``); the wallet
credited on a failure-refund belongs to a SEPARATE regular ``User`` who
owns the transaction — same split as ``test_admin_refunds.py``.

Provider injection: we override ``get_vtpass_provider`` /
``get_paystack_provider`` with deterministic stubs so each test pins the
exact requery/verify outcome (same pattern as the reconcile suites).
"""
import uuid
from decimal import Decimal

import pytest

from app.api.deps import get_paystack_provider, get_vtpass_provider
from app.core.security import hash_password
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.paystack.schemas import (
    PaystackAuthorization,
    VerifyResponse,
)
from app.integrations.vtpass.base import BillProvider
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    BillPurchaseResponse,
)
from app.main import app
from app.utils.references import new_transaction_reference


# ── Seed helpers ──────────────────────────────────────────────────────────


def _seed_user(db, *, email: str | None = None) -> User:
    """Regular User who OWNS the tx being requeried (wallet-credit target)."""
    user = User(
        email=email or f"owner-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+23480{uuid.uuid4().int % 10**8:08d}",
        full_name="Requery Target",
        password_hash=hash_password("Secret1!"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_wallet(db, user_id, *, balance: Decimal) -> Wallet:
    wallet = Wallet(
        user_id=user_id, balance=balance, balance_cap=Decimal("200000.00")
    )
    db.add(wallet)
    db.commit()
    return wallet


def _seed_bill_tx(
    db,
    user_id,
    *,
    amount: Decimal = Decimal("500.00"),
    status: TransactionStatus = TransactionStatus.processing,
    tx_type: TransactionType = TransactionType.airtime,
) -> Transaction:
    """A bill tx + its wallet-debit Payment row, mirroring the live
    BillService shape (provider='wallet', PaymentStatus.success)."""
    tx = Transaction(
        user_id=user_id,
        reference=new_transaction_reference(user_id=str(user_id)),
        type=tx_type,
        status=status,
        amount=amount,
        fee=Decimal("0.00"),
        meta={"phone": "08011111111", "service_id": "mtn", "network": "MTN"},
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
    return tx


def _seed_funding_tx(
    db,
    user_id,
    *,
    amount: Decimal = Decimal("1000.00"),
    status: TransactionStatus = TransactionStatus.pending,
    paystack_ref: str | None = None,
) -> tuple[Transaction, Payment]:
    """A wallet_funding tx + its pending Paystack Payment row."""
    ref = paystack_ref or f"ps_{uuid.uuid4().hex[:16]}"
    tx = Transaction(
        user_id=user_id,
        reference=new_transaction_reference(user_id=str(user_id)),
        type=TransactionType.wallet_funding,
        status=status,
        amount=amount,
        fee=Decimal("0.00"),
        meta={},
    )
    db.add(tx)
    db.flush()
    payment = Payment(
        transaction_id=tx.id,
        provider="paystack",
        provider_reference=ref,
        status=PaymentStatus.pending,
    )
    db.add(payment)
    db.commit()
    db.refresh(tx)
    return tx, payment


# ── Deterministic provider stubs ───────────────────────────────────────────


class _StubVTPass(BillProvider):
    """Bill provider where the test pins what requery returns per request_id.
    Only ``requery`` is exercised; everything else asserts (the endpoint
    must never purchase on a requery)."""

    def __init__(self) -> None:
        self._by_ref: dict[str, BillPurchaseResponse] = {}

    def set_response(self, request_id: str, response: BillPurchaseResponse) -> None:
        self._by_ref[request_id] = response

    async def requery(self, *, request_id: str) -> BillPurchaseResponse:
        if request_id not in self._by_ref:
            raise AssertionError(f"no requery response configured for {request_id!r}")
        return self._by_ref[request_id]

    async def purchase_airtime(self, **k):
        raise AssertionError("requery must not purchase")

    async def purchase_data(self, **k):
        raise AssertionError("requery must not purchase")

    async def list_data_plans(self, **k):
        raise AssertionError("requery must not list plans")


class _StubPaystack:
    """Payment provider where the test pins what verify returns per reference."""

    def __init__(self) -> None:
        self._by_ref: dict[str, str] = {}
        self._amounts: dict[str, Decimal] = {}

    def set_outcome(self, reference: str, status: str, amount: Decimal) -> None:
        self._by_ref[reference] = status
        self._amounts[reference] = amount

    async def verify(self, *, reference: str) -> VerifyResponse:
        if reference not in self._by_ref:
            raise AssertionError(f"no verify outcome configured for {reference!r}")
        return VerifyResponse(
            reference=reference,
            status=self._by_ref[reference],
            amount=self._amounts[reference],
            paid_at="2026-06-04T12:00:00Z",
            authorization=PaystackAuthorization(
                channel="card", last4="4081", bank=None
            ),
        )


def _delivered(ref: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id=f"vtp_{ref[:8]}",
        status=BillDeliveryStatus.delivered, code="000",
        requested_amount_ngn=amount, delivered_amount_ngn=amount,
        description="TRANSACTION SUCCESSFUL", raw={"code": "000"},
    )


def _failed(ref: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id="",
        status=BillDeliveryStatus.failed, code="016",
        requested_amount_ngn=amount, delivered_amount_ngn=Decimal("0.00"),
        description="TRANSACTION FAILED", raw={"code": "016"},
    )


def _override_vtpass(stub: _StubVTPass) -> None:
    app.dependency_overrides[get_vtpass_provider] = lambda: stub


def _override_paystack(stub: _StubPaystack) -> None:
    app.dependency_overrides[get_paystack_provider] = lambda: stub


# ── 1. Auth contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requery_unauthenticated_rejects_401(admin_client):
    r = await admin_client.post(
        "/api/v1/admin/transactions/TMP-260604-0001/requery",
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_requery_missing_csrf_rejects_403(admin_ctx, login_admin):
    """Authenticated admin but no X-CSRF-Token header → 403 CSRF_FAILED.
    Auth resolves first (cookies present), so the 403 is purely the CSRF
    gate."""
    client, db, _redis = admin_ctx
    await login_admin()
    owner = _seed_user(db)
    tx = _seed_bill_tx(db, owner.id)

    r = await client.post(f"/api/v1/admin/transactions/{tx.reference}/requery")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CSRF_FAILED"


# ── 2. Terminal no-op (provider NOT called) ──────────────────────────────


@pytest.mark.asyncio
async def test_requery_terminal_tx_is_noop_and_skips_provider(admin_ctx, login_admin):
    """A tx already in a terminal state (success) must NOT hit the provider;
    the endpoint echoes the current state with requeried=false."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    owner = _seed_user(db)
    tx = _seed_bill_tx(db, owner.id, status=TransactionStatus.success)

    # A stub that ASSERTS if requery is ever called proves the no-op path
    # never touched the provider.
    stub = _StubVTPass()  # no responses configured → requery() asserts
    _override_vtpass(stub)
    try:
        r = await client.post(
            f"/api/v1/admin/transactions/{tx.reference}/requery",
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.pop(get_vtpass_provider, None)

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["requeried"] is False
    assert body["status"] == "success"
    assert body["reference"] == tx.reference

    db.expire_all()
    fresh = db.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success


# ── 3. Pending bill → success ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requery_pending_bill_resolves_to_success_with_audit(
    admin_ctx, login_admin
):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    owner = _seed_user(db)
    _seed_wallet(db, owner.id, balance=Decimal("0.00"))
    tx = _seed_bill_tx(
        db, owner.id, amount=Decimal("500.00"),
        status=TransactionStatus.processing,
    )

    stub = _StubVTPass()
    stub.set_response(tx.reference, _delivered(tx.reference, Decimal("500.00")))
    _override_vtpass(stub)
    try:
        r = await client.post(
            f"/api/v1/admin/transactions/{tx.reference}/requery",
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.pop(get_vtpass_provider, None)

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["requeried"] is True
    assert body["status"] == "success"

    db.expire_all()
    fresh = db.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success

    # Audit event recording the admin actor.
    admin_events = (
        db.query(TransactionEvent)
        .filter(
            TransactionEvent.transaction_id == tx.id,
            TransactionEvent.reason == "admin_requery",
        )
        .all()
    )
    assert len(admin_events) == 1
    from app.db.models.admin_user import AdminUser
    admin = db.query(AdminUser).filter_by(email="ops@x.com").one()
    assert admin_events[0].context["actor_admin_user_id"] == str(admin.id)


# ── 4. Pending bill → failed → refund (mirrors reconcile) ────────────────


@pytest.mark.asyncio
async def test_requery_pending_bill_failed_refunds_wallet(admin_ctx, login_admin):
    """Permanent failure on requery → tx failed + refund tx created + wallet
    credited, EXACTLY as _reconcile_bills' apply_provider_result path."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    owner = _seed_user(db)
    # Wallet already debited (BillService debits on purchase); refund restores.
    _seed_wallet(db, owner.id, balance=Decimal("4500.00"))
    tx = _seed_bill_tx(
        db, owner.id, amount=Decimal("500.00"),
        status=TransactionStatus.processing,
    )

    stub = _StubVTPass()
    stub.set_response(tx.reference, _failed(tx.reference, Decimal("500.00")))
    _override_vtpass(stub)
    try:
        r = await client.post(
            f"/api/v1/admin/transactions/{tx.reference}/requery",
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.pop(get_vtpass_provider, None)

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["requeried"] is True
    assert body["status"] == "failed"

    db.expire_all()
    fresh = db.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.failed

    # Wallet restored by the refund credit: 4500 + 500 = 5000.
    wallet = db.query(Wallet).filter(Wallet.user_id == owner.id).one()
    assert wallet.balance == Decimal("5000.00")

    refunds = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == owner.id,
            Transaction.type == TransactionType.refund,
        )
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("500.00")
    assert refunds[0].meta["original_reference"] == tx.reference


# ── 5. Pending wallet_funding → success (Paystack verify) ────────────────


@pytest.mark.asyncio
async def test_requery_pending_funding_verifies_and_credits(admin_ctx, login_admin):
    """A pending wallet_funding tx → Paystack verify=success → Payment
    flips to success, wallet credited, tx → success. Mirrors _reconcile."""
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    owner = _seed_user(db)
    _seed_wallet(db, owner.id, balance=Decimal("0.00"))
    tx, payment = _seed_funding_tx(
        db, owner.id, amount=Decimal("1000.00"),
        status=TransactionStatus.pending,
    )

    stub = _StubPaystack()
    stub.set_outcome(payment.provider_reference, "success", Decimal("1000.00"))
    _override_paystack(stub)
    try:
        r = await client.post(
            f"/api/v1/admin/transactions/{tx.reference}/requery",
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.pop(get_paystack_provider, None)

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["requeried"] is True
    assert body["status"] == "success"

    db.expire_all()
    fresh = db.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
    wallet = db.query(Wallet).filter(Wallet.user_id == owner.id).one()
    assert wallet.balance == Decimal("1000.00")
    fresh_payment = db.query(Payment).filter(Payment.id == payment.id).one()
    assert fresh_payment.status == PaymentStatus.success


# ── 6. Pending bill still pending → no state change ──────────────────────


@pytest.mark.asyncio
async def test_requery_pending_bill_still_pending_no_change(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    owner = _seed_user(db)
    _seed_wallet(db, owner.id, balance=Decimal("4500.00"))
    tx = _seed_bill_tx(
        db, owner.id, amount=Decimal("500.00"),
        status=TransactionStatus.processing,
    )

    stub = _StubVTPass()
    stub.set_response(
        tx.reference,
        BillPurchaseResponse(
            request_id=tx.reference, transaction_id="",
            status=BillDeliveryStatus.pending, code="099",
            requested_amount_ngn=Decimal("500.00"),
            delivered_amount_ngn=Decimal("0.00"),
            description="PENDING", raw={"code": "099"},
        ),
    )
    _override_vtpass(stub)
    try:
        r = await client.post(
            f"/api/v1/admin/transactions/{tx.reference}/requery",
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        app.dependency_overrides.pop(get_vtpass_provider, None)

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["requeried"] is True
    assert body["status"] == "processing"

    db.expire_all()
    fresh = db.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing


# ── 7. Unknown reference → 404 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_requery_unknown_reference_404(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()

    r = await client.post(
        "/api/v1/admin/transactions/TMP-999999-NONE/requery",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"
