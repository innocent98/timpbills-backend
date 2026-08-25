"""Reset a user's KYC tier and clear their KYC records — a dev/staging testing
helper so the BVN/NIN verification flow can be re-run from scratch.

Usage (on the server that owns the DB, e.g. staging):
    python scripts/reset_kyc.py --email you@example.com --tier 1

- Sets the user's kyc_level to the given tier (0/1/2/3).
- Deletes ALL of the user's kyc_records (so /kyc/status is clean and a fresh
  start_verification isn't blocked by a stale pending record).
- The wallet balance_cap re-resolves from the new tier on the next credit
  (WalletService.credit re-reads _KYC_CAPS), so nothing else to do.

Idempotent + safe to re-run. Refuses if the email doesn't exist.
"""
import argparse

from sqlalchemy.orm import Session

from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.db.session import SessionLocal

_TIER_BY_INT = {
    0: KycLevel.tier_0,
    1: KycLevel.tier_1,
    2: KycLevel.tier_2,
    3: KycLevel.tier_3,
}


def reset_kyc(db: Session, *, email: str, tier: int) -> None:
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise SystemExit(f"No user found with email {email!r}")

    level = _TIER_BY_INT[tier]
    deleted = (
        db.query(KycRecord).filter(KycRecord.user_id == user.id).delete()
    )
    user.kyc_level = level
    db.commit()
    print(
        f"Reset {email}: kyc_level -> {level.value}, "
        f"deleted {deleted} kyc_record(s)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset a user's KYC tier + clear their KYC records"
    )
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--tier", type=int, default=1, choices=[0, 1, 2, 3],
        help="Tier to reset the user to (default 1)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        reset_kyc(db, email=args.email, tier=args.tier)
    finally:
        db.close()


if __name__ == "__main__":
    main()
