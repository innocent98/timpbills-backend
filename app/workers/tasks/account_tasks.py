"""Anonymize accounts soft-deleted at least GRACE_DAYS ago. Scrubs PII from
the users row and deletes PII child rows, but KEEPS the financial ledger
(wallet, transactions, virtual_accounts, wallet_credit_keys) with identity
detached, per AML retention. Idempotent via the anonymized_at marker."""
from datetime import UTC, datetime, timedelta

from app.db.models.kyc_record import KycRecord
from app.db.models.otp import OtpCode
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.db.session import SessionLocal
from app.services.account_deletion_service import GRACE_DAYS
from app.workers.celery_app import celery_app

_DELETED_PASSWORD = "!ACCOUNT_DELETED!"  # not a valid hash; never verifies


@celery_app.task(name="app.workers.tasks.account_tasks.anonymize_deleted_accounts")
def anonymize_deleted_accounts() -> dict:
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=GRACE_DAYS)
        users = (
            db.query(User)
            .filter(User.deleted_at <= cutoff)
            .filter(User.anonymized_at.is_(None))
            .limit(200)
            .all()
        )
        count = 0
        for u in users:
            locked = (
                db.query(User).filter(User.id == u.id).with_for_update().one()
            )
            if locked.anonymized_at is not None:
                continue
            # Delete PII child rows.
            db.query(PushToken).filter(PushToken.user_id == locked.id).delete()
            db.query(OtpCode).filter(OtpCode.user_id == locked.id).delete()
            db.query(KycRecord).filter(KycRecord.user_id == locked.id).delete()
            # Detach the DVA from Paystack identity but keep the row. The
            # column is NOT NULL (see VirtualAccount.paystack_customer_code),
            # so we set a non-functional placeholder rather than nulling it.
            for va in db.query(VirtualAccount).filter(
                VirtualAccount.user_id == locked.id
            ):
                va.paystack_customer_code = f"deleted-{locked.id}"
            # Scrub the user row (keep id + retained-ledger FKs).
            locked.email = f"deleted-{locked.id}@deleted.invalid"
            locked.phone = f"deleted:{locked.id}"
            locked.full_name = "Deleted User"
            locked.password_hash = _DELETED_PASSWORD
            locked.pin_hash = None
            locked.date_of_birth = None
            locked.gender = None
            locked.address = None
            locked.avatar_url = None
            # referral_code is NOT NULL + unique (VARCHAR(8)) and is exposed
            # via the public referral share URL / queryable by exact match,
            # so leaving the real value is a residual identity linkage.
            # Derived from locked.id (not the random generate_referral_code
            # path, which could collide with a live user's code) so the
            # placeholder is deterministic and fits the column's uniqueness
            # guarantee without touching the DB to check for collisions.
            locked.referral_code = f"d{locked.id.hex[:7]}"
            locked.anonymized_at = datetime.now(UTC)
            db.commit()
            count += 1
        return {"anonymized": count}
    finally:
        db.close()
