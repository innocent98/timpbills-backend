"""AppSetting — simple key/value store for runtime-tunable config.

Sprint 5b introduces this so ops can tune referral economics (reward
amounts, caps, kill switch) without a redeploy. Values are stored as
TEXT — callers cast to the right type at read time. Keep this dumb on
purpose; over-engineering a typed schema for ~6 keys is not worth it.

Future expansion (e.g. an admin UI in Sprint 8) can add validators per
key without changing the table shape.
"""
from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"

    key = Column(String(128), primary_key=True)
    value = Column(String, nullable=False)
    updated_by = Column(UUID(as_uuid=True), nullable=True)
