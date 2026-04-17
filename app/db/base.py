from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

# Import all models here so Alembic autogenerate picks them up
# on every `alembic revision --autogenerate`.
from app.db.mixins import TimestampMixin  # noqa: F401,E402
from app.db.models import user, otp  # noqa: F401,E402
from app.db.models import wallet  # noqa: F401,E402
