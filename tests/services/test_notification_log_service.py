from app.db.models.notification_log import (
    NotificationChannel, NotificationLogStatus,
)
from app.services.notification_log_service import NotificationLogService


def test_record_pending_then_mark_sent(db_session):
    svc = NotificationLogService(db=db_session)
    row = svc.record_pending(
        user_id=None, event="otp", channel=NotificationChannel.sms, provider="termii",
    )
    assert row.status is NotificationLogStatus.pending
    svc.mark_sent(row, provider_reference="msg-1")
    db_session.refresh(row)
    assert row.status is NotificationLogStatus.sent
    assert row.sent_at is not None
    assert row.provider_reference == "msg-1"


def test_mark_failed(db_session):
    svc = NotificationLogService(db=db_session)
    row = svc.record_pending(
        user_id=None, event="bill_success", channel=NotificationChannel.email,
        provider="resend",
    )
    svc.mark_failed(row, error="smtp down")
    db_session.refresh(row)
    assert row.status is NotificationLogStatus.failed
    assert row.error == "smtp down"
