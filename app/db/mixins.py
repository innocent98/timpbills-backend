"""Reusable SQLAlchemy mixins."""
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime


class TimestampMixin:
    """Adds created_at + updated_at columns with timezone-aware defaults."""

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
