"""NotificationPreference — per-user notification opt-in/out flags.

One row per user (enforced by the UNIQUE constraint on ``user_id`` plus the
ON DELETE CASCADE FK back to ``users``). Defaults follow spec §3.2:

* ``transaction_alerts``    — opt-in  (default ``True``)
* ``referral_updates``      — opt-in  (default ``True``)
* ``email_notifications``   — opt-in  (default ``True``)
* ``promotions``            — opt-out (default ``False``)

The underlying table was created by migration ``202605201200``. This file is
the ORM mirror; column types/defaults must stay aligned with that migration.

NOTE on the ``id`` default: the DB uses ``server_default=gen_random_uuid()``
(Postgres-only). We *also* set a Python-side ``default=uuid.uuid4`` so the
ORM can populate ``id`` before INSERT — important for SQLite-backed tests
that don't have ``pgcrypto``. SQLAlchemy uses the Python default when the
column value is ``None`` at flush time; on Postgres the server_default is
the safety net for raw SQL inserts that bypass the ORM.
"""
import uuid

from sqlalchemy import Boolean, Column, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin


class NotificationPreference(TimestampMixin, Base):
    __tablename__ = "notification_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    transaction_alerts = Column(Boolean, nullable=False, default=True)
    referral_updates = Column(Boolean, nullable=False, default=True)
    promotions = Column(Boolean, nullable=False, default=False)
    email_notifications = Column(Boolean, nullable=False, default=True)

    user = relationship("User", back_populates="notification_preference")
