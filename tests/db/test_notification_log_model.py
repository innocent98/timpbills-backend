from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)


def test_notification_log_row(db_session):
    log = NotificationLog(
        user_id=None,
        event="otp",
        channel=NotificationChannel.sms,
        status=NotificationLogStatus.pending,
        provider="termii",
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    assert log.id is not None
    assert log.status is NotificationLogStatus.pending
    assert log.sent_at is None


def test_notification_log_sent(db_session):
    from datetime import UTC, datetime

    log = NotificationLog(
        user_id=None,
        event="bill_success",
        channel=NotificationChannel.email,
        status=NotificationLogStatus.sent,
        provider="resend",
        provider_reference="msg-1",
        sent_at=datetime.now(UTC),
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    assert log.channel is NotificationChannel.email
    assert log.provider_reference == "msg-1"
    assert log.sent_at is not None
