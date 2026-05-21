from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

# Import all models here so Alembic autogenerate picks them up
# on every `alembic revision --autogenerate`.
from app.db.mixins import TimestampMixin  # noqa: F401,E402
from app.db.models import (  # noqa: F401,E402  # noqa: F401,E402  # noqa: F401,E402  # noqa: F401,E402
    app_setting,
    idempotency_key,
    notification_preference,
    otp,
    payment,
    push_token,  # noqa: F401,E402
    referral,
    transaction,
    transaction_event,
    user,
    wallet,  # noqa: F401,E402
    webhook_event,
)
