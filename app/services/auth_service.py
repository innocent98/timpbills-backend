import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

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
from app.schemas.auth import (
    AuthTokens,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
    VerifyOtpRequest,
    VerifyOtpResponse,
)

_ACCESS_EXPIRE = timedelta(minutes=60)
_REFRESH_EXPIRE = timedelta(days=30)


def _issue_token_pair(user_id: str) -> AuthTokens:
    jti = uuid4().hex
    access = create_access_token(subject=user_id, expires_in=_ACCESS_EXPIRE)
    refresh = create_refresh_token(subject=user_id, jti=jti, expires_in=_REFRESH_EXPIRE)
    return AuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(_ACCESS_EXPIRE.total_seconds()),
    )


class AuthService:
    def __init__(self, *, db: Session, sms: SmsProvider) -> None:
        self._db = db
        self._sms = sms

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
            phone=user.phone,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.register,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._sms.send_otp(phone=user.phone, code=code)
        return RegisterResponse(user_id=str(user.id), phone=user.phone)

    async def verify_otp(self, req: VerifyOtpRequest) -> VerifyOtpResponse:
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

        if datetime.utcnow() > otp.expires_at:
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(req.code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.utcnow()
        user.kyc_level = KycLevel.tier_1
        user.is_phone_verified = True
        self._db.commit()

        tokens = _issue_token_pair(str(user.id))
        return VerifyOtpResponse(tokens=tokens, pin_set=user.pin_hash is not None)

    async def login(self, req: LoginRequest) -> LoginResponse:
        user = (
            self._db.query(User)
            .filter((User.email == req.identifier) | (User.phone == req.identifier))
            .first()
        )
        if not user or not verify_password(req.password, user.password_hash):
            raise ValueError("INVALID_CREDENTIALS")

        tokens = _issue_token_pair(str(user.id))
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

        return _issue_token_pair(user_id)

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
            expires_at=datetime.utcnow() + timedelta(minutes=5),
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

        if datetime.utcnow() > otp.expires_at:
            raise ValueError("OTP_EXPIRED")

        if otp.attempts >= 3:
            raise ValueError("OTP_ATTEMPTS_EXCEEDED")

        if not verify_pin(code, otp.code_hash):
            otp.attempts += 1
            self._db.commit()
            raise ValueError("INVALID_OTP")

        otp.used_at = datetime.utcnow()
        user.password_hash = hash_password(new_password)
        self._db.commit()
