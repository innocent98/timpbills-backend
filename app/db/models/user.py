import enum
import uuid

from sqlalchemy import Boolean, Column, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.services.referral_code import generate_referral_code


def _default_referral_code() -> str:
    """ORM-level default for users.referral_code.

    SQLAlchemy invokes this when no code is supplied at construction. We
    intentionally pass a no-op ``code_exists`` here — the model layer has
    no DB session of its own, and the column's UNIQUE index is the
    backstop against the (vanishingly improbable) collision.

    Production user-creation code (Sprint 5b/B2 onward) should still call
    ``generate_referral_code`` directly with a real DB-backed
    ``code_exists`` so collisions are caught proactively instead of via a
    failed INSERT.
    """
    return generate_referral_code(code_exists=lambda _candidate: False)


class KycLevel(str, enum.Enum):
    tier_0 = "tier_0"
    tier_1 = "tier_1"
    tier_2 = "tier_2"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    pin_hash = Column(String, nullable=True)
    kyc_level = Column(Enum(KycLevel, name="kyc_level_enum"), nullable=False, default=KycLevel.tier_0)
    email_verified = Column(Boolean, nullable=False, default=False, server_default="false")
    is_phone_verified = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Single-bit admin flag — see alembic 202604281200 for rationale
    # (vs a separate admin_users join table). Defaults False so every
    # non-admin user just has it unset.
    is_admin = Column(Boolean, nullable=False, default=False, server_default="false")

    # Sprint 5b: referral system. `referral_code` is the system-generated
    # 6-char invite code (immutable per user). `referred_by_user_id` is
    # nullable — most users sign up cold.
    referral_code = Column(
        String(8),
        nullable=False,
        unique=True,
        index=True,
        default=_default_referral_code,
    )
    referred_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
