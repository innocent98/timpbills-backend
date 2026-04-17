from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from jose import JWTError
from redis.asyncio import Redis

from app.db.session import SessionLocal
from app.services.auth_service import AuthService
from app.services.token_store import RedisTokenStore, TokenStore
from app.integrations.base import SmsProvider
from app.integrations.email.base import EmailProvider
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.termii.client import TermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.integrations.email.resend import ResendClient
from app.core.config import settings
from app.core.security import decode_token
from app.db.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# Singleton fake SMS client so tests can inspect .sent
_fake_sms_singleton = FakeTermiiClient()

# Singleton fake email client so tests can inspect .sent
_fake_email_singleton = FakeEmailClient()

# Redis client singleton
_redis_client: Redis | None = None


def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def get_token_store(redis: Redis = Depends(get_redis)) -> TokenStore:
    return RedisTokenStore(redis=redis)


def reset_fake_sms() -> None:
    """Clear the fake SMS singleton's sent messages (for test isolation)."""
    _fake_sms_singleton.sent.clear()


def reset_fake_email() -> None:
    """Clear the fake email singleton's sent messages (for test isolation)."""
    _fake_email_singleton.sent.clear()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_sms_provider() -> SmsProvider:
    env = getattr(settings, "ENVIRONMENT", "dev")
    if settings.FORCE_FAKE_PROVIDERS or env in ("dev", "test", "development"):
        return _fake_sms_singleton
    return TermiiClient()


def get_email_provider() -> EmailProvider:
    env = getattr(settings, "ENVIRONMENT", "dev")
    if settings.FORCE_FAKE_PROVIDERS or env in ("dev", "test", "development"):
        return _fake_email_singleton
    return ResendClient()


def get_auth_service(
    db: Session = Depends(get_db),
    sms: SmsProvider = Depends(get_sms_provider),
    email: EmailProvider = Depends(get_email_provider),
    token_store: TokenStore = Depends(get_token_store),
) -> AuthService:
    return AuthService(db=db, sms=sms, email=email, token_store=token_store)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Missing credentials"},
        )
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid or expired token"},
        )
    if payload.get("typ") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Refresh token cannot be used as access token"},
        )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid token"},
        )
    try:
        user_uuid = UUID(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid token"},
        )
    user = db.query(User).filter(User.id == user_uuid).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "USER_NOT_FOUND", "message": "User not found"},
        )
    return user
