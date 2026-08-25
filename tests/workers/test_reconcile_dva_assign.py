"""Reconciliation sweep for DVA *provisioning* stuck on a missed/rejected
assign webhook.

Production incident: the Paystack DVA lifecycle webhooks
(customeridentification.* / dedicatedaccount.assign.*) were rejected with 400
because the endpoint demanded a `data.id` that real Paystack DVA events don't
carry. The assigned account number never landed, so the VirtualAccount hung in
`pending_assign` forever and the app polled `GET /wallet/virtual-account`
indefinitely with no self-healing.

This sweep is the self-healing: for a VA stuck pending past the grace window it
asks Paystack directly (by customer_code) whether an account was assigned and
backfills it. It also recovers accounts stranded by the historical bug.
"""
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from app.db.models._enums import VirtualAccountStatus
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.integrations.paystack.fake import FakePaystackClient


def _seed_user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"dva-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="DVA Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _seed_va(
    db,
    user,
    *,
    status=VirtualAccountStatus.pending_assign,
    customer_code="CUS_stuck_1",
    age=timedelta(minutes=10),
):
    va = VirtualAccount(
        id=uuid.uuid4(),
        user_id=user.id,
        paystack_customer_code=customer_code,
        status=status,
        currency="NGN",
    )
    db.add(va)
    db.flush()
    va.created_at = datetime.now(UTC) - age
    db.commit()
    return va


def _run_sweep(db_session, fake):
    """Run the assign-recovery sweep against the shared test session, with the
    paystack factory returning `fake` (mirrors the other reconcile tests)."""
    from app.workers.tasks import reconcile_tasks as rt

    original_close = db_session.close
    db_session.close = lambda: None
    try:
        with patch.object(rt, "SessionLocal", lambda: db_session), \
             patch.object(rt, "select_paystack_client", lambda: fake), \
             patch.object(rt, "dispatch_delay", Mock()) as disp:
            result = rt.reconcile_pending_dva_assign()
            return result, disp
    finally:
        db_session.close = original_close


def test_stuck_pending_assign_backfilled_and_activated(db_session):
    user = _seed_user(db_session)
    va = _seed_va(db_session, user, customer_code="CUS_stuck_1")
    fake = FakePaystackClient()
    fake.will_have_dedicated_account(
        customer_code="CUS_stuck_1", account_number="9911223344",
        bank_name="Wema Bank", bank_slug="wema-bank",
    )

    result, disp = _run_sweep(db_session, fake)

    assert result["recovered"] == 1
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.active
    assert row.account_number == "9911223344"
    assert row.bank_slug == "wema-bank"
    # user is notified their account is ready
    assert disp.called


def test_pending_identity_also_recovered(db_session):
    user = _seed_user(db_session)
    va = _seed_va(
        db_session, user, status=VirtualAccountStatus.pending_identity,
        customer_code="CUS_ident_1",
    )
    fake = FakePaystackClient()
    fake.will_have_dedicated_account(customer_code="CUS_ident_1", account_number="9900001111")

    result, _ = _run_sweep(db_session, fake)

    assert result["recovered"] == 1
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.active
    assert row.account_number == "9900001111"


def test_no_account_assigned_yet_left_pending(db_session):
    """Assignment genuinely still in flight (Paystack returns no account) -> the
    VA is left pending for a later tick, not force-failed."""
    user = _seed_user(db_session)
    va = _seed_va(db_session, user, customer_code="CUS_inflight_1")
    fake = FakePaystackClient()  # no will_have_dedicated_account -> returns None

    result, disp = _run_sweep(db_session, fake)

    assert result["recovered"] == 0
    assert result["skipped"] == 1
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.pending_assign
    assert not disp.called


def test_fresh_va_inside_grace_window_ignored(db_session):
    """A VA provisioned seconds ago is left alone — the live assign webhook may
    still be completing it; the sweep must not race the live path."""
    user = _seed_user(db_session)
    va = _seed_va(db_session, user, age=timedelta(seconds=5), customer_code="CUS_fresh_1")
    fake = FakePaystackClient()
    fake.will_have_dedicated_account(customer_code="CUS_fresh_1", account_number="9900002222")

    result, _ = _run_sweep(db_session, fake)

    assert result["checked"] == 0
    assert result["recovered"] == 0
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.pending_assign


def test_already_active_va_untouched(db_session):
    """An active VA (webhook completed normally) is never re-processed."""
    user = _seed_user(db_session)
    va = _seed_va(db_session, user, status=VirtualAccountStatus.active, customer_code="CUS_active_1")
    va_number = va.account_number
    fake = FakePaystackClient()

    result, _ = _run_sweep(db_session, fake)

    assert result["checked"] == 0
    db_session.expire_all()
    row = db_session.query(VirtualAccount).filter(VirtualAccount.id == va.id).one()
    assert row.status == VirtualAccountStatus.active
    assert row.account_number == va_number
