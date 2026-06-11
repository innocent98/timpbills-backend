"""Audit-log writer for every notification channel send. Best-effort:
a logging failure must never break the send it's recording, so callers
wrap writes defensively (the service itself just does the DB work)."""
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)


class NotificationLogService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def record_pending(
        self, *, user_id, event: str, channel: NotificationChannel, provider: str
    ) -> NotificationLog:
        row = NotificationLog(
            user_id=user_id,
            event=event,
            channel=channel,
            status=NotificationLogStatus.pending,
            provider=provider,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def mark_sent(
        self, row: NotificationLog, *, provider_reference: str | None = None
    ) -> None:
        row.status = NotificationLogStatus.sent
        row.sent_at = datetime.now(UTC)
        if provider_reference:
            row.provider_reference = provider_reference
        self._db.commit()

    def mark_failed(self, row: NotificationLog, *, error: str) -> None:
        row.status = NotificationLogStatus.failed
        row.error = error[:2000]
        self._db.commit()
