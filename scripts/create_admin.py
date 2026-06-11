"""Create the first (or another) admin_users row.

Usage (interactive):
    python scripts/create_admin.py

Idempotent: if the email already exists, prints a notice and exits 0
without modifying the existing row.
"""
import argparse
import getpass

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.db.session import SessionLocal


def create_admin(db: Session, *, email: str, password: str, full_name: str) -> AdminUser:
    email = email.strip().lower()
    existing = db.query(AdminUser).filter(AdminUser.email == email).first()
    if existing is not None:
        return existing
    admin = AdminUser(
        email=email, password_hash=hash_password(password), full_name=full_name
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an admin user")
    parser.add_argument("--email")
    parser.add_argument("--full-name")
    args = parser.parse_args()

    email = args.email or input("Admin email: ").strip()
    full_name = args.full_name or input("Full name: ").strip()
    password = getpass.getpass("Password: ")
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters.")

    db = SessionLocal()
    try:
        admin = create_admin(db, email=email, password=password, full_name=full_name)
        print(f"Admin ready: {admin.email} (id={admin.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
