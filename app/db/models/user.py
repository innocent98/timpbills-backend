import enum
import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

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

    @property
    def numeric(self) -> int:
        """Tier as an integer (0/1/2) for the public API response.

        The DB column stores the enum string, but mobile expects a
        numeric tier so it can render labels (Tier 0/1/2/3) and gate
        features by tier threshold via comparison.  The mobile DTO
        already declares ``int kycLevel`` and switches on it — see
        ``profile_account_card.dart``.
        """
        return int(self.value.rsplit("_", 1)[-1])


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

    # Sprint 5c: profile-extension columns. All nullable — these are
    # optional, surfaced by the profile screen and edited via PATCH /me.
    # Migration 202605201200 added the underlying columns.
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    avatar_url = Column(String(512), nullable=True)

    # Sprint 5c · Task 4.2: "log me out everywhere" stamp.
    # When set, every access token whose ``iat`` claim predates this
    # timestamp is rejected by the auth gate. Updated atomically by
    # ``/auth/password/change`` so a stolen password can't outlive
    # the user's discovery + remediation window.
    # Migration 202605210900 added the column.
    tokens_revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Sprint 5c · Task 6.1: soft-delete tombstone for DELETE /users/me.
    # Set alongside ``is_active=False`` + ``tokens_revoked_at`` when the
    # user self-deletes. The /auth/register flow consults this column to
    # block re-registration with the same phone or email for 30 days.
    # Hard delete (PII purge) is a Sprint 8 / compliance concern.
    # Migration 202605220900 added the column.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # 1:1 back-reference to NotificationPreference. `cascade="all,
    # delete-orphan"` mirrors the FK's ON DELETE CASCADE — deleting the
    # user from the ORM also drops their preference row in the same flush.
    notification_preference = relationship(
        "NotificationPreference",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
