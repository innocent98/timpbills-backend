from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

from app.db.models.user import User  # noqa: F401
from app.db.models.otp import OtpCode  # noqa: F401
