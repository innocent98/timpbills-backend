"""Push tokens service — CRUD for user FCM device tokens.

Called by the /users/me/push-tokens endpoints for registration + deletion,
and by the FCM client when it reports a DeadFCMToken to evict stale rows.

One FCM token is globally unique (enforced at DB level). If a device is
re-used across users (logout + new login), the token row is reassigned to
the new user rather than duplicated, so push targeting never sends to a
stale owner.
"""
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
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

        Sprint 4 B24: the previous SELECT-then-INSERT sequence had a
        TOCTOU race — two concurrent registrations of the same
        ``fcm_token`` (rare but possible: same device, rapid login
        toggles) could both observe no row and both attempt INSERT; the
        second would hit the ``fcm_token`` unique constraint and surface
        as a 500. Fix: on IntegrityError, rollback and fall through to
        the UPDATE path. Portable across Postgres (prod) and SQLite
        (tests) without depending on dialect-specific ``ON CONFLICT``.
        """
        now = datetime.now(UTC)

        # Fast path: row already exists — reassign / touch last_seen_at.
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

        # Cold path: attempt INSERT. A concurrent registration of the
        # same token would have won the unique-constraint race — catch
        # IntegrityError, rollback, and transition to the UPDATE path
        # on the now-existing row.
        #
        # Sprint 4 B30 (B-I3 follow-up) handles a SECOND concurrency
        # scenario beyond the original B24: between our failed INSERT
        # and the subsequent SELECT, a FCM `DeadFCMToken` handler on
        # another thread could have called `delete_by_fcm_token`,
        # removing the row we expected to UPDATE. Previously `.one()`
        # would throw NoResultFound and surface as a 500 — user's
        # device silently loses push registration. Now we bounded-retry
        # the whole upsert (caps at 2 attempts to prevent a pathological
        # ping-pong with a deletion loop), then fall through to raising
        # IntegrityError only if the contention persists.
        for attempt in range(2):
            row = PushToken(
                user_id=user_id,
                fcm_token=fcm_token,
                platform=platform,
                last_seen_at=now,
            )
            self._db.add(row)
            try:
                self._db.commit()
                self._db.refresh(row)
                return row
            except IntegrityError:
                self._db.rollback()

            # UPDATE path after losing the INSERT race.
            winner = (
                self._db.query(PushToken)
                .filter(PushToken.fcm_token == fcm_token)
                .one_or_none()
            )
            if winner is not None:
                winner.user_id = user_id
                winner.platform = platform
                winner.last_seen_at = now
                self._db.commit()
                self._db.refresh(winner)
                return winner
            # Row vanished between the failed INSERT and this SELECT —
            # a concurrent DeadFCMToken deletion ran. Loop back and
            # retry the whole upsert from scratch; the next iteration's
            # INSERT should succeed cleanly.
        # Both attempts exhausted. The contention pattern implies a
        # pathological insert/delete loop — let the caller see the
        # IntegrityError so it surfaces to ops alerting rather than
        # silently spinning.
        raise RuntimeError(
            f"push_tokens upsert exceeded retry budget for token "
            f"prefix={fcm_token[:8]}••• — likely contending with a "
            f"concurrent DeadFCMToken handler"
        )

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
