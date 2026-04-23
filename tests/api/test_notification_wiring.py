"""Integration coverage for the three Sprint-3 notification wire-ins.

Because Celery's `task_always_eager=True` is enabled when
FORCE_FAKE_PROVIDERS is set (see app/workers/celery_app.py), calling
`dispatch_delay(...)` in a test runs the whole NotificationService
path synchronously. That means we can assert against FakeEmailClient
and FakePushClient after each HTTP request.

Covered:
 • BillService purchase delivered → bill_success dispatched
 • BillService purchase failed → bill_failure_refund dispatched
 • /webhooks/vtpass delivered (late) → bill_success dispatched
 • /webhooks/paystack charge.success (wallet funding) → wallet_funded

Not covered here (unit tests own these):
 • render failures
 • channel robustness
"""
import json
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    _fake_email_singleton,
    _fake_push_singleton,
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_paystack,
    reset_fake_push,
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
from app.integrations.email.fake import FakeEmailClient
from app.integrations.vtpass import factory as _vtpass_factory
from app.main import app
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user


_test_email_client = FakeEmailClient()
_VTPASS_SECRET = "test-notif-secret"


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
    reset_fake_push()
    _test_email_client.sent.clear()

    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email_client

    _orig_secret = settings.VTPASS_WEBHOOK_SECRET
    settings.VTPASS_WEBHOOK_SECRET = _VTPASS_SECRET

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    settings.VTPASS_WEBHOOK_SECRET = _orig_secret
    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


# ── Helpers ─────────────────────────────────────────────────────────────


async def _pin_token(client, headers) -> str:
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    return r.json()["data"]["pin_token"]


def _fund_wallet_directly(db, *, email="e@e.co", amount=Decimal("5000.00")) -> User:
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id, balance=amount, balance_cap=Decimal("50000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = amount
    db.commit()
    return user


def _seed_processing_bill_tx(
    db, *,
    email: str = "e@e.co",
    amount: Decimal = Decimal("500.00"),
) -> Transaction:
    user = db.query(User).filter(User.email == email).one()
    wallet = db.query(Wallet).filter(Wallet.user_id == user.id).first()
    if wallet is None:
        wallet = Wallet(
            user_id=user.id,
            balance=Decimal("5000.00") - amount,
            balance_cap=Decimal("50000.00"),
        )
        db.add(wallet)
    else:
        wallet.balance = Decimal("5000.00") - amount

    from app.utils.references import new_transaction_reference
    tx = Transaction(
        user_id=user.id,
        reference=new_transaction_reference(user_id=str(user.id)),
        type=TransactionType.airtime,
        status=TransactionStatus.processing,
        amount=amount, fee=Decimal("0.00"),
        meta={"network": "MTN", "phone": "08012345678", "service_id": "mtn"},
    )
    db.add(tx)
    db.flush()
    db.add(Payment(
        transaction_id=tx.id, provider="wallet",
        provider_reference=tx.reference, status=PaymentStatus.success,
    ))
    db.commit()
    db.refresh(tx)
    return tx


def _vtpass_body(reference: str, code: str) -> dict:
    return {
        "code": code,
        "response_description": "OK",
        "requestId": reference,
        "content": {"transactions": {
            "status": "delivered" if code == "000" else "failed",
            "transactionId": f"vtp-{uuid4().hex[:8]}",
            "amount": "500",
        }},
    }


# ── BillService synchronous path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_purchase_delivered_dispatches_bill_success(
    client, db_session,
):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    push = _fake_push_singleton
    # Notifications go through the Celery task, which resolves its own
    # email client via _resolve_clients(). In tests (FORCE_FAKE_PROVIDERS=True)
    # that returns `_fake_email_singleton` from app.api.deps — NOT the
    # DI-overridden `_test_email_client`. The DI override serves the
    # auth flows (OTP sending); notification emails land on the deps
    # singleton.
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)
    pushes_before = len(push.sent)

    r = await client.post(
        "/api/v1/bills/airtime",
        json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "success"

    bill_success_emails = [
        e for e in notif_email.sent[emails_before:]
        if "airtime" in e.subject.lower() and "on its way" in e.subject.lower()
    ]
    assert len(bill_success_emails) == 1

    bill_success_pushes = [
        p for p in push.sent[pushes_before:]
        if p.data.get("event") == "bill_success"
    ]
    assert len(bill_success_pushes) == 1


@pytest.mark.asyncio
async def test_airtime_purchase_failed_dispatches_bill_failure_refund(
    client, db_session,
):
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    # Force VTPass to fail for whatever reference BillService generates.
    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_airtime

    async def always_fail(**kw):
        fake.will_fail(kw["request_id"])
        return await original_purchase(**kw)
    fake.purchase_airtime = always_fail  # type: ignore[method-assign]

    push = _fake_push_singleton
    notif_email = _fake_email_singleton

    try:
        r = await client.post(
            "/api/v1/bills/airtime",
            json={"network": "MTN", "phone": "08012345678", "amount": "500.00"},
            headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"

        refund_pushes = [p for p in push.sent if p.data.get("event") == "bill_failure_refund"]
        assert len(refund_pushes) == 1

        refund_emails = [
            e for e in notif_email.sent
            if "refund" in e.subject.lower()
        ]
        assert len(refund_emails) == 1
    finally:
        fake.purchase_airtime = original_purchase  # type: ignore[method-assign]


# ── /webhooks/vtpass — late delivery fires bill_success ─────────────────


@pytest.mark.asyncio
async def test_vtpass_webhook_delivered_dispatches_bill_success(
    client, db_session,
):
    _, _ = await _seed_logged_in_user(client)
    tx = _seed_processing_bill_tx(db_session, amount=Decimal("500.00"))

    push = _fake_push_singleton
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)

    body = _vtpass_body(tx.reference, code="000")
    r = await client.post(
        "/api/v1/webhooks/vtpass",
        content=json.dumps(body).encode(),
        headers={"X-VTPass-Secret": _VTPASS_SECRET},
    )
    assert r.status_code == 200, r.text

    success_pushes = [p for p in push.sent if p.data.get("event") == "bill_success"]
    assert any(p.data.get("reference") == tx.reference for p in success_pushes)

    new_emails = notif_email.sent[emails_before:]
    assert any(
        tx.reference in e.code_or_body or "airtime" in e.subject.lower()
        for e in new_emails
    )


# ── /webhooks/paystack — charge.success fires wallet_funded ─────────────


@pytest.mark.asyncio
async def test_paystack_webhook_charge_success_dispatches_wallet_funded(
    client, db_session,
):
    """When a user's Paystack funding charge finally settles, the
    webhook handler credits the wallet *and* fires the wallet_funded
    notification so the user gets a push + receipt email."""
    _, headers = await _seed_logged_in_user(client)
    pin = await _pin_token(client, headers)

    # Initialize funding so a Payment row exists.
    r = await client.post(
        "/api/v1/wallet/fund",
        json={"amount": "5000.00"},
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    reference = r.json()["data"]["reference"]

    # Mark the fake paystack verify as "success" for this reference so
    # the webhook handler passes its integrity cross-check.
    from app.api.deps import _fake_paystack_singleton as fps
    fps.will_succeed(reference)

    # Forge a matching Paystack webhook. The fake paystack client
    # accepts the literal signature "FAKE_SIG" — no HMAC compute needed.
    payload = {
        "event": "charge.success",
        "data": {
            "id":        "paystack-evt-notif-1",
            "reference": reference,
        },
    }
    raw = json.dumps(payload).encode()

    push = _fake_push_singleton
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)

    w = await client.post(
        "/api/v1/webhooks/paystack",
        content=raw,
        headers={"x-paystack-signature": "FAKE_SIG"},
    )
    assert w.status_code == 200, w.text

    funded_pushes = [p for p in push.sent if p.data.get("event") == "wallet_funded"]
    assert any(p.data.get("reference") == reference for p in funded_pushes)

    new_emails = notif_email.sent[emails_before:]
    assert any("funded" in e.subject.lower() for e in new_emails)


# ── Sprint 4 · B18: electricity_token_delivered branch ───────────────────


@pytest.mark.asyncio
async def test_electricity_purchase_delivered_dispatches_electricity_token_delivered(
    client, db_session,
):
    """Electricity wins its own notification event: the email carries
    the token + units + DisCo + meter. Asserts that bill_success is NOT
    dispatched for electricity (branch exclusivity)."""
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    push = _fake_push_singleton
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)
    pushes_before = len(push.sent)

    r = await client.post(
        "/api/v1/bills/electricity",
        json={
            "service_id":   "ikeja-electric",
            "meter_number": "1234567890123",
            "meter_type":   "prepaid",
            "phone":        "08012345678",
            "amount":       "2000.00",
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "success"

    new_pushes = push.sent[pushes_before:]
    electricity_pushes = [
        p for p in new_pushes
        if p.data.get("event") == "electricity_token_delivered"
    ]
    assert len(electricity_pushes) == 1
    # bill_success should NOT fire for electricity — the branch is exclusive.
    bill_success_pushes = [
        p for p in new_pushes if p.data.get("event") == "bill_success"
    ]
    assert bill_success_pushes == []

    new_emails = notif_email.sent[emails_before:]
    electricity_emails = [
        e for e in new_emails if "electricity token" in e.subject.lower()
    ]
    assert len(electricity_emails) == 1
    # The plaintext body (FakeEmailClient stores text-or-html in
    # code_or_body) surfaces the meter + token.
    body = electricity_emails[0].code_or_body
    assert "1234567890123" in body
    assert "TOKEN" in body.upper()


# ── Sprint 4 · B19: cable_activated branch ───────────────────────────────


@pytest.mark.asyncio
async def test_cable_change_mode_dispatches_cable_activated(
    client, db_session,
):
    """Cable purchases fire cable_activated (and NOT bill_success).
    Asserts both change-mode dispatch and the email body surfacing
    the plan + provider display label."""
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("50000.00"))
    pin = await _pin_token(client, headers)

    push = _fake_push_singleton
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)
    pushes_before = len(push.sent)

    r = await client.post(
        "/api/v1/bills/cable",
        json={
            "service_id":       "dstv",
            "smartcard_number": "1234567890",
            "mode":             "change",
            "variation_code":   "dstv-premium",
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "success"

    new_pushes = push.sent[pushes_before:]
    activated = [
        p for p in new_pushes
        if p.data.get("event") == "cable_activated"
    ]
    assert len(activated) == 1
    # Branch exclusivity — the generic bill_success must NOT also fire.
    bill_success_pushes = [
        p for p in new_pushes if p.data.get("event") == "bill_success"
    ]
    assert bill_success_pushes == []
    # The push title uses the provider display label, not the wire slug.
    assert "DStv" in activated[0].title

    new_emails = notif_email.sent[emails_before:]
    cable_emails = [e for e in new_emails if "DStv" in e.subject]
    assert len(cable_emails) == 1
    body = cable_emails[0].code_or_body
    assert "Premium" in body
    assert "1234567890" in body


# ── Sprint 4 · B23: electricity delivered without token — fallback ──────


@pytest.mark.asyncio
async def test_electricity_delivered_without_token_falls_back_to_bill_success(
    client, db_session, monkeypatch,
):
    """Sprint 4 B23 regression: a delivered electricity tx whose
    ``result.raw`` lacks a ``token`` (malformed VTPass response) must
    fall through to the generic ``bill_success`` event rather than
    dispatching the electricity-specific event with an empty token.

    The B18 path has an explicit `if token:` guard in
    ``_notify_bill_success`` — without this test, a regression that
    accidentally dispatches ``electricity_token_delivered`` with a blank
    token (producing a broken email body reading "Your token is .") would
    slip through. We also assert branch exclusivity: exactly one event
    fires, never two.

    We simulate the malformed upstream by monkeypatching the fake
    VTPass client's ``purchase_electricity`` to strip ``token`` and
    ``units`` from the raw payload while keeping delivered status.
    """
    _, headers = await _seed_logged_in_user(client)
    _fund_wallet_directly(db_session, amount=Decimal("5000.00"))
    pin = await _pin_token(client, headers)

    fake = _vtpass_factory.get_fake_singleton()
    original_purchase = fake.purchase_electricity

    async def _strip_token(**kw):
        response = await original_purchase(**kw)
        # Drop token + units from raw to simulate a malformed delivered
        # response — status remains delivered, only the token is absent.
        stripped_raw = {
            k: v for k, v in (response.raw or {}).items()
            if k not in ("token", "units")
        }
        return response.model_copy(update={"raw": stripped_raw})

    monkeypatch.setattr(fake, "purchase_electricity", _strip_token)

    push = _fake_push_singleton
    notif_email = _fake_email_singleton
    emails_before = len(notif_email.sent)
    pushes_before = len(push.sent)

    r = await client.post(
        "/api/v1/bills/electricity",
        json={
            "service_id":   "ikeja-electric",
            "meter_number": "1234567890123",
            "meter_type":   "prepaid",
            "phone":        "08012345678",
            "amount":       "2000.00",
        },
        headers={**headers, "X-Pin-Token": pin, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "success"

    new_pushes = push.sent[pushes_before:]
    # The electricity-specific event MUST NOT fire — the token guard
    # in _notify_bill_success should have dropped through to the
    # generic path.
    electricity_pushes = [
        p for p in new_pushes
        if p.data.get("event") == "electricity_token_delivered"
    ]
    assert electricity_pushes == [], (
        "electricity_token_delivered dispatched despite missing token — "
        "B23 regression"
    )

    # Exactly one generic bill_success push — no double-dispatch.
    bill_success_pushes = [
        p for p in new_pushes if p.data.get("event") == "bill_success"
    ]
    assert len(bill_success_pushes) == 1, (
        f"expected exactly one bill_success push, got {len(bill_success_pushes)}: "
        f"{[p.data.get('event') for p in new_pushes]}"
    )

    # Email path follows the same fallback — no electricity-token-
    # specific email (subject starts with "Electricity token —" from
    # _email_subject), but the generic bill_success email (subject
    # pattern: "Your ₦X electricity token is on its way") is allowed.
    # Distinguish by prefix, not the substring "electricity token".
    new_emails = notif_email.sent[emails_before:]
    electricity_specific_emails = [
        e for e in new_emails if e.subject.startswith("Electricity token")
    ]
    assert electricity_specific_emails == [], (
        "electricity_token_delivered email dispatched despite missing token"
    )
    # Exactly one generic bill-success email did fire — the fallback
    # path landed on the generic template.
    generic_bill_emails = [
        e for e in new_emails
        if e.subject.startswith("Your ₦") and "on its way" in e.subject
    ]
    assert len(generic_bill_emails) == 1, (
        f"expected exactly one generic bill_success email, got "
        f"{[e.subject for e in new_emails]}"
    )
