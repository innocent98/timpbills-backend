"""Reusable SQLAlchemy mixins."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime


class TimestampMixin:
    """Adds created_at + updated_at columns with timezone-aware defaults."""

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
