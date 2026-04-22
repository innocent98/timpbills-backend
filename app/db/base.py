from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

# Import all models here so Alembic autogenerate picks them up
# on every `alembic revision --autogenerate`.
from app.db.mixins import TimestampMixin  # noqa: F401,E402
from app.db.models import user, otp  # noqa: F401,E402
from app.db.models import wallet  # noqa: F401,E402
from app.db.models import transaction, transaction_event, payment  # noqa: F401,E402
from app.db.models import idempotency_key, webhook_event  # noqa: F401,E402
from app.db.models import push_token  # noqa: F401,E402
