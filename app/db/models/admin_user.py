import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class AdminRole(str, enum.Enum):
    superadmin = "superadmin"
    support = "support"   # reserved for v2 RBAC; unused in v1


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(
        Enum(AdminRole, name="admin_role_enum"),
        nullable=False,
        default=AdminRole.superadmin,
        server_default="superadmin",
    )
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    last_login_at = Column(DateTime(timezone=True), nullable=True)
