import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import log
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_pin,
    verify_password,
    verify_pin,
)
from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import KycLevel, User
from app.integrations.base import SmsProvider
from app.integrations.email.base import EmailProvider
from app.schemas.auth import (
    AuthTokens,
    EmailVerifiedResponse,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
    VerifyEmailOtpRequest,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from app.services.token_store import NullTokenStore, TokenStore

_ACCESS_EXPIRE = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
_REFRESH_EXPIRE = timedelta(days=30)
REFRESH_TOKEN_TTL_SECONDS = int(_REFRESH_EXPIRE.total_seconds())


def _ensure_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite (used in tests) strips tzinfo on round-trip even with
    DateTime(timezone=True); Postgres preserves it. This helper lets the same
    comparison code work against both backends.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_expired(expires_at: datetime) -> bool:
    return datetime.now(timezone.utc) > _ensure_aware_utc(expires_at)


def _issue_token_pair(user_id: str) -> tuple[AuthTokens, str]:
    """Return (AuthTokens, jti) so callers can persist the jti."""
    jti = uuid4().hex
    access = create_access_token(subject=user_id, expires_in=_ACCESS_EXPIRE)
    refresh = create_refresh_token(subject=user_id, jti=jti, expires_in=_REFRESH_EXPIRE)
    tokens = AuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(_ACCESS_EXPIRE.total_seconds()),
    )
    return tokens, jti


class AuthService:
    def __init__(
        self,
        *,
        db: Session,
        sms: SmsProvider,
        email: EmailProvider,
        token_store: TokenStore,
    ) -> None:
        self._db = db
        self._sms = sms
        self._email = email
        self._tokens: TokenStore = token_store

    async def register(self, req: RegisterRequest) -> RegisterResponse:
        existing = (
            self._db.query(User)
            .filter((User.phone == req.phone) | (User.email == req.email))
            .first()
        )
        if existing:
            raise ValueError("USER_ALREADY_EXISTS")

        user = User(
            phone=req.phone,
            email=req.email,
            full_name=req.full_name,
            password_hash=hash_password(req.password),
            kyc_level=KycLevel.tier_0,
        )
        self._db.add(user)
        self._db.flush()

        code = f"{secrets.randbelow(1_000_000):06d}"
        otp = OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._email.send_otp(to=user.email, code=code)
        return RegisterResponse(user_id=str(user.id), email=user.email, phone=user.phone)

    async def send_email_otp(self, email: str) -> None:
        """Re-send an email OTP for the given address (e.g. resend during countdown)."""
        user = self._db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        if user.email_verified:
            raise ValueError("EMAIL_ALREADY_VERIFIED")

        code = f"{secrets.randbelow(1_000_000):06d}"
        otp = OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._email.send_otp(to=user.email, code=code)

    async def verify_email_otp(self, req: VerifyEmailOtpRequest) -> EmailVerifiedResponse:
        user = self._db.query(User).filter(User.email == req.email).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        otp = (
            self._db.query(OtpCode)
            .filter(
                OtpCode.user_id == user.id,
                OtpCode.purpose == OtpPurpose.email_verification,
                OtpCode.used_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .first()
        )
        if not otp:
            raise ValueError("NO_ACTIVE_OTP")

        if _is_expired(otp.expires_at):
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(req.code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.now(timezone.utc)
        user.email_verified = True
        self._db.commit()

        tokens, jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return EmailVerifiedResponse(
            tokens=tokens,
            pin_set=user.pin_hash is not None,
            phone_verified=user.is_phone_verified,
        )

    async def send_phone_otp(self, user_id: UUID) -> None:
        """Send a phone OTP for an authenticated user (on-demand upgrade to Tier 1)."""
        user = self._db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        if user.is_phone_verified:
            raise ValueError("PHONE_ALREADY_VERIFIED")

        code = f"{secrets.randbelow(1_000_000):06d}"
        otp = OtpCode(
            user_id=user.id,
            phone=user.phone,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.phone_verification,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._sms.send_otp(phone=user.phone, code=code)

    async def verify_phone_otp(self, user_id: UUID, code: str) -> VerifyOtpResponse:
        """Verify phone OTP for an authenticated user and upgrade to Tier 1."""
        user = self._db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        otp = (
            self._db.query(OtpCode)
            .filter(
                OtpCode.user_id == user.id,
                OtpCode.purpose == OtpPurpose.phone_verification,
                OtpCode.used_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .first()
        )
        if not otp:
            raise ValueError("NO_ACTIVE_OTP")

        if _is_expired(otp.expires_at):
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.now(timezone.utc)
        user.kyc_level = KycLevel.tier_1
        user.is_phone_verified = True
        self._db.commit()

        tokens, jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return VerifyOtpResponse(tokens=tokens, pin_set=user.pin_hash is not None)

    # ---------------------------------------------------------------------------
    # Legacy verify_otp — kept for backward compat within service layer.
    # The old /auth/verify-otp endpoint has been removed (clean rename).
    # ---------------------------------------------------------------------------
    async def verify_otp(self, req: VerifyOtpRequest) -> VerifyOtpResponse:
        """DEPRECATED: old phone-based register verification. Use verify_email_otp instead."""
        user = self._db.query(User).filter(User.phone == req.phone).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        otp = (
            self._db.query(OtpCode)
            .filter(
                OtpCode.user_id == user.id,
                OtpCode.purpose == OtpPurpose.register,
                OtpCode.used_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .first()
        )
        if not otp:
            raise ValueError("NO_ACTIVE_OTP")

        if _is_expired(otp.expires_at):
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(req.code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.now(timezone.utc)
        user.kyc_level = KycLevel.tier_1
        user.is_phone_verified = True
        self._db.commit()

        tokens, jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return VerifyOtpResponse(tokens=tokens, pin_set=user.pin_hash is not None)

    async def login(self, req: LoginRequest) -> LoginResponse:
        user = (
            self._db.query(User)
            .filter((User.email == req.identifier) | (User.phone == req.identifier))
            .first()
        )
        if not user or not verify_password(req.password, user.password_hash):
            raise ValueError("INVALID_CREDENTIALS")

        if not user.email_verified:
            raise ValueError("EMAIL_NOT_VERIFIED")
        if not user.is_active:
            raise ValueError("ACCOUNT_DISABLED")

        tokens, jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return LoginResponse(tokens=tokens, pin_set=user.pin_hash is not None)

    async def refresh(self, refresh_token: str) -> AuthTokens:
        try:
            payload = decode_token(refresh_token)
        except Exception:
            raise ValueError("INVALID_TOKEN")

        if payload.get("typ") != "refresh":
            raise ValueError("INVALID_TOKEN")

        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("INVALID_TOKEN")

        old_jti = payload.get("jti")
        if not old_jti:
            raise ValueError("INVALID_TOKEN")

        if not await self._tokens.is_valid(user_id=user_id, jti=old_jti):
            # Replay attack or revoked token — nuke all sessions for this user
            log.warning(
                "Refresh token replay detected — revoking all sessions for user_id=%s jti=%s",
                user_id,
                old_jti,
            )
            await self._tokens.revoke_all(user_id=user_id)
            raise ValueError("INVALID_TOKEN")

        await self._tokens.revoke(user_id=user_id, jti=old_jti)

        try:
            user_uuid = UUID(user_id)
        except (TypeError, ValueError):
            raise ValueError("INVALID_TOKEN")
        user = self._db.query(User).filter(User.id == user_uuid).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")

        new_tokens, new_jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=new_jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return new_tokens

    async def set_pin(self, user_id: UUID, pin: str) -> None:
        user = self._db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")
        user.pin_hash = hash_pin(pin)
        self._db.commit()

    async def forgot_password(self, identifier: str) -> None:
        user = (
            self._db.query(User)
            .filter((User.email == identifier) | (User.phone == identifier))
            .first()
        )
        if not user:
            # Silent — do not reveal whether account exists
            return

        code = f"{secrets.randbelow(1_000_000):06d}"
        otp = OtpCode(
            user_id=user.id,
            phone=user.phone,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.password_reset,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._sms.send_otp(phone=user.phone, code=code)

    async def reset_password(self, identifier: str, code: str, new_password: str) -> None:
        user = (
            self._db.query(User)
            .filter((User.email == identifier) | (User.phone == identifier))
            .first()
        )
        if not user:
            raise ValueError("USER_NOT_FOUND")

        otp = (
            self._db.query(OtpCode)
            .filter(
                OtpCode.user_id == user.id,
                OtpCode.purpose == OtpPurpose.password_reset,
                OtpCode.used_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .first()
        )
        if not otp:
            raise ValueError("NO_ACTIVE_OTP")

        if _is_expired(otp.expires_at):
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.now(timezone.utc)
        user.password_hash = hash_password(new_password)
        self._db.commit()

        await self._tokens.revoke_all(user_id=str(user.id))
