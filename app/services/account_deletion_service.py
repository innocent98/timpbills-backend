"""Account deletion: shared entry point for the public web flow and the
authenticated DELETE /users/me. Starts a reversible soft-delete (the
existing deleted_at tombstone) and schedules PII anonymization for 30 days
later. The ledger is retained; see the anonymization sweep."""
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import verify_password_async
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.services.notification_service import NotificationEvent
from app.services.token_store import TokenStore
from app.utils.phone import InvalidPhoneFormat, normalize_to_e164
from app.workers.tasks.notification_tasks import dispatch_delay

GRACE_DAYS = 30


def _ensure_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite (used in tests) strips tzinfo on round-trip even with
    DateTime(timezone=True); Postgres preserves it. Same helper as
    ``auth_service._ensure_aware_utc`` -- this lets the idempotent-path
    return value stay comparable to the freshly-computed one regardless
    of backend.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AccountDeletionService:
    def __init__(self, *, db: Session, token_store: TokenStore) -> None:
        self._db = db
        self._token_store = token_store

    async def resolve_and_verify(self, *, identifier: str, password: str) -> User:
        try:
            phone = normalize_to_e164(identifier)
        except InvalidPhoneFormat:
            phone = identifier
        user = (
            self._db.query(User)
            .filter((User.email == identifier) | (User.phone == phone))
            .first()
        )
        # One generic error for both "no such user" and "bad password" so the
        # public endpoint cannot be used to enumerate accounts. Note: we
        # resolve even when is_active is False, so a pending-deletion account
        # can still cancel.
        if not user or not await verify_password_async(password, user.password_hash):
            raise ValueError("INVALID_CREDENTIALS")
        return user

    async def request_deletion(self, *, user: User) -> datetime:
        # Idempotent: an account already inside the grace window returns its
        # existing schedule without re-stamping or re-notifying.
        if user.deleted_at is not None and user.anonymized_at is None:
            return _ensure_aware_utc(user.deleted_at) + timedelta(days=GRACE_DAYS)

        wallet = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user.id)
            .with_for_update()
            .first()
        )
        if wallet is not None and wallet.balance > 0:
            raise ValueError("WALLET_NOT_EMPTY")

        now = datetime.now(UTC)
        user.is_active = False
        user.deleted_at = now
        user.tokens_revoked_at = now
        self._db.add(user)
        self._db.commit()

        await self._token_store.revoke_all(user_id=str(user.id))

        scheduled = now + timedelta(days=GRACE_DAYS)
        dispatch_delay(
            user_id=str(user.id),
            user_email=user.email,
            event=NotificationEvent.account_deletion_requested,
            context={
                "scheduled_date": scheduled.strftime("%d %B %Y"),
                "cancel_url": "https://timpbills.com/delete-account",
            },
        )
        return scheduled

    def cancel_deletion(self, *, user: User) -> None:
        if user.anonymized_at is not None:
            raise ValueError("ALREADY_ANONYMIZED")
        user.is_active = True
        user.deleted_at = None
        self._db.add(user)
        self._db.commit()
