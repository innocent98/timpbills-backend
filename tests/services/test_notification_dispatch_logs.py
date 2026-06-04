"""Task 13: NotificationService writes a notification_logs row per channel
send when wired with a DB session.

A bill_success dispatch fans out to email + push; the fakes both succeed,
so we expect one email row and one push row, both marked ``sent``.
"""
from decimal import Decimal

import pytest

from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.push.fake import FakePushClient
from app.services.notification_service import (
    NotificationEvent,
    NotificationService,
    build_bill_context,
)


def _make_user(db) -> User:
    u = User(
        email="dispatch@test.co",
        phone="+2348011112222",
        full_name="Dispatch User",
        password_hash="x",
        kyc_level=KycLevel.tier_0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_dispatch_writes_email_and_push_logs(db_session):
    user = _make_user(db_session)
    email = FakeEmailClient()
    push = FakePushClient()
    svc = NotificationService(email_client=email, push_client=push, db=db_session)

    ctx = build_bill_context(
        tx_type="airtime",
        amount=Decimal("500"),
        destination="08012345678",
        reference="TMP-DISPATCH-1",
        when="now",
        partial=False,
    )
    await svc.dispatch(
        user_id=str(user.id),
        user_email=user.email,
        event=NotificationEvent.bill_success,
        context=ctx,
    )

    rows = db_session.query(NotificationLog).all()
    by_channel = {r.channel: r for r in rows}

    assert NotificationChannel.email in by_channel
    email_row = by_channel[NotificationChannel.email]
    assert email_row.event == "bill_success"
    assert email_row.provider == "resend"
    assert email_row.status is NotificationLogStatus.sent
    assert email_row.sent_at is not None
    assert email_row.user_id == user.id

    assert NotificationChannel.push in by_channel
    push_row = by_channel[NotificationChannel.push]
    assert push_row.event == "bill_success"
    assert push_row.provider == "fcm"
    assert push_row.status is NotificationLogStatus.sent
    assert push_row.sent_at is not None
    assert push_row.user_id == user.id


@pytest.mark.asyncio
async def test_dispatch_without_db_skips_logging(db_session):
    """Legacy construction (no db) must not attempt any log write and must
    still deliver the notification."""
    email = FakeEmailClient()
    push = FakePushClient()
    svc = NotificationService(email_client=email, push_client=push)  # no db

    ctx = build_bill_context(
        tx_type="airtime",
        amount=Decimal("500"),
        destination="08012345678",
        reference="TMP-NODB-1",
        when="now",
        partial=False,
    )
    await svc.dispatch(
        user_id="00000000-0000-0000-0000-000000000001",
        user_email="nodb@test.co",
        event=NotificationEvent.bill_success,
        context=ctx,
    )

    # Sends still happened.
    assert len(email.sent) == 1
    assert len(push.sent) == 1
    # No rows written to the shared session.
    assert db_session.query(NotificationLog).count() == 0


@pytest.mark.asyncio
async def test_logging_failure_never_breaks_send(db_session, monkeypatch):
    """Headline best-effort guarantee: if the log write itself raises, the
    real send must still happen and dispatch must NOT propagate the error."""
    from app.services import notification_log_service

    def _boom(self, *args, **kwargs):
        raise RuntimeError("log store down")

    # Break the very first log write (pending). The wrappers swallow it.
    monkeypatch.setattr(
        notification_log_service.NotificationLogService, "record_pending", _boom
    )

    user = _make_user(db_session)
    email = FakeEmailClient()
    push = FakePushClient()
    svc = NotificationService(email_client=email, push_client=push, db=db_session)

    ctx = build_bill_context(
        tx_type="airtime",
        amount=Decimal("500"),
        destination="08012345678",
        reference="TMP-LOGFAIL-1",
        when="now",
        partial=False,
    )

    # Must not raise.
    await svc.dispatch(
        user_id=str(user.id),
        user_email=user.email,
        event=NotificationEvent.bill_success,
        context=ctx,
    )

    # The real sends still happened despite the logging fault.
    assert len(email.sent) == 1
    assert len(push.sent) == 1
    # record_pending never succeeded, so no rows landed.
    assert db_session.query(NotificationLog).count() == 0


@pytest.mark.asyncio
async def test_failed_email_send_writes_failed_row(db_session):
    """Email send raises → a failed email row with the error captured."""

    class _BoomEmail(FakeEmailClient):
        async def send_text(self, *, to, subject, html, text=None):
            raise RuntimeError("smtp exploded")

    user = _make_user(db_session)
    email = _BoomEmail()
    push = FakePushClient()
    svc = NotificationService(email_client=email, push_client=push, db=db_session)

    ctx = build_bill_context(
        tx_type="airtime",
        amount=Decimal("500"),
        destination="08012345678",
        reference="TMP-EMAILFAIL-1",
        when="now",
        partial=False,
    )
    await svc.dispatch(
        user_id=str(user.id),
        user_email=user.email,
        event=NotificationEvent.bill_success,
        context=ctx,
    )

    email_rows = (
        db_session.query(NotificationLog)
        .filter(NotificationLog.channel == NotificationChannel.email)
        .all()
    )
    assert len(email_rows) == 1
    row = email_rows[0]
    assert row.status is NotificationLogStatus.failed
    assert row.error is not None
    assert "smtp exploded" in row.error
    # The send genuinely failed — fake captured nothing.
    assert email.sent == []


@pytest.mark.asyncio
async def test_no_devices_writes_no_push_row(db_session):
    """Fix A: token-aware mode with zero registered devices writes NO push
    row (the no-token baseline must not inflate the failure bucket). The
    email row may still be written."""
    from app.services.push_tokens_service import PushTokensService

    user = _make_user(db_session)  # no PushToken rows for this user
    email = FakeEmailClient()
    push = FakePushClient()
    svc = NotificationService(
        email_client=email,
        push_client=push,
        push_tokens_service=PushTokensService(db=db_session),
        db=db_session,
    )

    ctx = build_bill_context(
        tx_type="airtime",
        amount=Decimal("500"),
        destination="08012345678",
        reference="TMP-NODEVICE-1",
        when="now",
        partial=False,
    )
    await svc.dispatch(
        user_id=str(user.id),
        user_email=user.email,
        event=NotificationEvent.bill_success,
        context=ctx,
    )

    push_rows = (
        db_session.query(NotificationLog)
        .filter(NotificationLog.channel == NotificationChannel.push)
        .all()
    )
    assert push_rows == []
    # No actual push was attempted either.
    assert push.sent == []
    # Email channel is unaffected.
    email_rows = (
        db_session.query(NotificationLog)
        .filter(NotificationLog.channel == NotificationChannel.email)
        .all()
    )
    assert len(email_rows) == 1
    assert email_rows[0].status is NotificationLogStatus.sent
