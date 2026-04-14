from datetime import datetime, timedelta, timezone
from typing import Any
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
    to_encode: dict[str, Any] = {
        "sub": subject,
        "iat": datetime.now(tz=timezone.utc),
        "exp": datetime.now(tz=timezone.utc) + expires_in,
    }
    if extra:
        to_encode.update(extra)
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(*, subject: str, jti: str, expires_in: timedelta = timedelta(days=30)) -> str:
    to_encode = {
        "sub": subject,
        "jti": jti,
        "typ": "refresh",
        "iat": datetime.now(tz=timezone.utc),
        "exp": datetime.now(tz=timezone.utc) + expires_in,
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
