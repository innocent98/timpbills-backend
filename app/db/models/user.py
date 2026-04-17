import uuid
import enum
from sqlalchemy import Boolean, Column, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base
from app.db.mixins import TimestampMixin


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
