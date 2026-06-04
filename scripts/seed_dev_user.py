"""Seed a known dev user + verified phone + PIN into the database.

Usage:
    make seed                                   # run inside docker-compose
    poetry run python scripts/seed_dev_user.py  # run locally

The script is idempotent — it updates the user if it already exists.

Default user:
    phone:    +2348000000001
    email:    dev@timpbills.test
    password: Test1234!
    pin:      1357
    kyc:      tier_1 (phone verified)
"""
from __future__ import annotations

import sys
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.security import hash_password, hash_pin
from app.db.models.user import KycLevel, User
from app.db.session import SessionLocal

DEV_USER = {
    "phone":     "+2348000000001",
    "email":     "dev@timpbills.test",
    "full_name": "Dev Tester",
    "password":  "Test1234!",
    "pin":       "1357",
}


def seed() -> None:
    db: Session = SessionLocal()
    try:
        existing = db.query(User).filter(User.phone == DEV_USER["phone"]).one_or_none()

        if existing is None:
            user = User(
                phone=DEV_USER["phone"],
                email=DEV_USER["email"],
                full_name=DEV_USER["full_name"],
                password_hash=hash_password(DEV_USER["password"]),
                pin_hash=hash_pin(DEV_USER["pin"]),
                kyc_level=KycLevel.tier_1,
                is_phone_verified=True,
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"✓ Created dev user id={user.id}")
        else:
            existing.email         = DEV_USER["email"]
            existing.full_name     = DEV_USER["full_name"]
            existing.password_hash = hash_password(DEV_USER["password"])
            existing.pin_hash      = hash_pin(DEV_USER["pin"])
            existing.kyc_level     = KycLevel.tier_1
            existing.is_phone_verified = True
            existing.is_active     = True
            existing.updated_at    = datetime.utcnow()
            db.commit()
            print(f"✓ Updated existing dev user id={existing.id}")

        print()
        print("Dev credentials:")
        print(f"  phone     {DEV_USER['phone']}")
        print(f"  email     {DEV_USER['email']}")
        print(f"  password  {DEV_USER['password']}")
        print(f"  pin       {DEV_USER['pin']}")
        print("  kyc_level tier_1 (phone_verified)")
    except Exception as exc:
        db.rollback()
        print(f"✗ Seed failed: {exc}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
