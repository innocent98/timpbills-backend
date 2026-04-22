"""Push tokens service — CRUD for user FCM device tokens.

Called by the /users/me/push-tokens endpoints for registration + deletion,
and by the FCM client when it reports a DeadFCMToken to evict stale rows.

One FCM token is globally unique (enforced at DB level). If a device is
re-used across users (logout + new login), the token row is reassigned to
the new user rather than duplicated, so push targeting never sends to a
stale owner.
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.push_token import PushToken


class PushTokensService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def upsert_for_user(
        self,
        *,
        user_id: UUID,
        fcm_token: str,
        platform: str,
    ) -> PushToken:
        """Insert or update a push token for a user.

        Three paths:
        - Row exists for ``fcm_token`` and belongs to ``user_id``:
          bump ``last_seen_at`` and return it.
        - Row exists for ``fcm_token`` but belongs to a different user:
          reassign to ``user_id``, update ``platform`` + ``last_seen_at``.
          (One device = one active user. Logout + new login transfers
          ownership.)
        - No row: insert a new one.
        """
        now = datetime.now(timezone.utc)
        existing = (
            self._db.query(PushToken)
            .filter(PushToken.fcm_token == fcm_token)
            .one_or_none()
        )
        if existing is not None:
            existing.user_id = user_id
            existing.platform = platform
            existing.last_seen_at = now
            self._db.commit()
            self._db.refresh(existing)
            return existing

        row = PushToken(
            user_id=user_id,
            fcm_token=fcm_token,
            platform=platform,
            last_seen_at=now,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete_for_user(self, *, user_id: UUID, token_id: UUID) -> bool:
        """Delete a token the user owns. False if missing or not theirs."""
        row = (
            self._db.query(PushToken)
            .filter(PushToken.id == token_id, PushToken.user_id == user_id)
            .one_or_none()
        )
        if row is None:
            return False
        self._db.delete(row)
        self._db.commit()
        return True

    def list_for_user(self, *, user_id: UUID) -> list[PushToken]:
        """All tokens for a user, most-recently-seen first."""
        return (
            self._db.query(PushToken)
            .filter(PushToken.user_id == user_id)
            .order_by(PushToken.last_seen_at.desc())
            .all()
        )

    def delete_by_fcm_token(self, *, fcm_token: str) -> bool:
        """Delete a row by raw FCM token (called by FCM client on
        DeadFCMToken). Returns True if a row was removed."""
        row = (
            self._db.query(PushToken)
            .filter(PushToken.fcm_token == fcm_token)
            .one_or_none()
        )
        if row is None:
            return False
        self._db.delete(row)
        self._db.commit()
        return True
