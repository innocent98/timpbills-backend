from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_pin_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(pw: str) -> str:
    return _pwd_ctx.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    return _pwd_ctx.verify(pw, hashed)


def hash_pin(pin: str) -> str:
    return _pin_ctx.hash(pin)


def verify_pin(pin: str, hashed: str) -> bool:
    return _pin_ctx.verify(pin, hashed)


def create_access_token(*, subject: str, extra: dict[str, Any] | None = None, expires_in: timedelta = timedelta(minutes=20)) -> str:
    """Issue an access token with a unique ``jti`` claim.

    The ``jti`` is what the Sprint 5c logout blocklist keys off
    (see ``TokenRevocationService``). Every access token gets a fresh
    UUID — pin tokens included, since they also flow through
    ``create_access_token``. ``extra`` may override ``jti`` for callers
    that want to control it (none in tree today, but the door stays
    open for tests that need a deterministic value).
    """
    to_encode: dict[str, Any] = {
        "sub": subject,
        "jti": uuid4().hex,
        "iat": datetime.now(tz=UTC),
        "exp": datetime.now(tz=UTC) + expires_in,
    }
    if extra:
        to_encode.update(extra)
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(*, subject: str, jti: str, expires_in: timedelta = timedelta(days=30)) -> str:
    to_encode = {
        "sub": subject,
        "jti": jti,
        "typ": "refresh",
        "iat": datetime.now(tz=UTC),
        "exp": datetime.now(tz=UTC) + expires_in,
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
