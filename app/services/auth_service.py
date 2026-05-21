import secrets
from datetime import UTC, datetime, timedelta
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
from app.db.models.referral import Referral, ReferralStatus
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
from app.services.app_setting_service import AppSettingService
from app.services.referral_code import generate_referral_code
from app.services.token_revocation_service import TokenRevocationService
from app.services.token_store import TokenStore

_ACCESS_EXPIRE = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
_REFRESH_EXPIRE = timedelta(days=30)
REFRESH_TOKEN_TTL_SECONDS = int(_REFRESH_EXPIRE.total_seconds())


def _ensure_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite (used in tests) strips tzinfo on round-trip even with
    DateTime(timezone=True); Postgres preserves it. This helper lets the same
    comparison code work against both backends.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _is_expired(expires_at: datetime) -> bool:
    return datetime.now(UTC) > _ensure_aware_utc(expires_at)


def _short_display_name(full_name: str) -> str:
    """Mask a user's name for visibility in referral feeds + push copy.

    ``"Tobi Adebayo"`` → ``"Tobi A."``  /  ``"Solo"`` → ``"Solo"``.
    Never exposes email / phone / surname — addresses spec §4.3
    "Mask referee PII"."""
    if not full_name:
        return "Friend"
    parts = full_name.strip().split()
    if not parts:
        return "Friend"
    first = parts[0]
    if len(parts) == 1:
        return first
    last_initial = parts[-1][:1].upper()
    return f"{first} {last_initial}."


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
        revocation_svc: TokenRevocationService | None = None,
    ) -> None:
        self._db = db
        self._sms = sms
        self._email = email
        self._tokens: TokenStore = token_store
        # Optional — older unit tests construct AuthService directly with
        # only the four core deps. When None, the JWT-jti blocklist check
        # in refresh() is skipped (rotation + RedisTokenStore still cover
        # the standard happy path). Production wiring in deps.py always
        # injects the real service.
        self._revocation_svc = revocation_svc

    async def register(self, req: RegisterRequest) -> RegisterResponse:
        existing = (
            self._db.query(User)
            .filter((User.phone == req.phone) | (User.email == req.email))
            .first()
        )
        if existing:
            raise ValueError("USER_ALREADY_EXISTS")

        # Eager referral-code generation (spec §5.1 + B1 decision #3):
        # call the helper with a real DB-backed `code_exists` so collisions
        # are caught before INSERT rather than via a failed unique-index.
        # The ORM-level `default=` on the column is kept as a backstop.
        new_code = generate_referral_code(
            code_exists=lambda c: self._db.query(User)
            .filter(User.referral_code == c)
            .first()
            is not None,
        )

        user = User(
            phone=req.phone,
            email=req.email,
            full_name=req.full_name,
            password_hash=hash_password(req.password),
            kyc_level=KycLevel.tier_0,
            referral_code=new_code,
        )
        self._db.add(user)
        self._db.flush()

        # Process incoming referral_code (spec §5.2). All failure paths
        # are silent — the user is registered regardless. The bool result
        # is surfaced in RegisterResponse so mobile can soft-fail-toast
        # when a code was sent but dropped (B4).
        referred_by = self._maybe_attribute_referral(
            referee=user, raw_code=req.referral_code,
        )

        code = f"{secrets.randbelow(1_000_000):06d}"
        otp = OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin(code),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self._db.add(otp)
        self._db.commit()

        await self._email.send_otp(to=user.email, code=code)
        return RegisterResponse(
            user_id=str(user.id),
            email=user.email,
            phone=user.phone,
            referred_by=referred_by,
        )

    # ── Referral attribution (Sprint 5b/B3) ──────────────────────────────

    def _maybe_attribute_referral(
        self, *, referee: User, raw_code: str | None
    ) -> bool:
        """Look up a referral code at signup; attribute on success; stay
        silent on failure (spec §5.2 / §6).

        Validations (any failure → no row, no error to client):
          * killswitch (``REFERRAL_ENABLED``) is true
          * referrer found by case-insensitive code match
          * referrer is active
          * referrer is not the signing-up user (covers ``my-own-code``)
          * no existing ``referrals`` row for this referee yet
        On success: set ``user.referred_by_user_id`` + INSERT a
        ``referrals`` row in ``pending`` status, and fire a
        ``referrer_signup_notified`` push to the referrer (fire-and-forget
        via the notification Celery task; failure is swallowed).

        Returns ``True`` iff a ``referrals`` row was created (B4 — lets
        the register response signal attribution success/failure to
        mobile). Returns ``False`` for every silent-drop branch."""
        if not raw_code:
            return False

        # Killswitch — if referrals are disabled, ignore the field entirely.
        # AppSettingService TTL=0 because each register call is a fresh
        # request; we want a live read, not a cached one.
        try:
            settings_svc = AppSettingService(db=self._db, ttl_seconds=0)
            if not settings_svc.get_bool("REFERRAL_ENABLED", default=True):
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("referral attribution: settings read failed: %s", exc)
            return False

        code = raw_code.strip().upper()
        if not code:
            return False

        # Case-insensitive lookup (codes are uppercased on write + on read).
        referrer = (
            self._db.query(User)
            .filter(User.referral_code == code)
            .first()
        )
        if referrer is None:
            return False
        if not referrer.is_active:
            return False
        if referrer.id == referee.id:
            return False

        # Idempotency: a referrals row may already exist (re-register
        # attempts shouldn't happen because the duplicate check above
        # bails, but be defensive).
        existing_row = (
            self._db.query(Referral)
            .filter(Referral.referee_user_id == referee.id)
            .first()
        )
        if existing_row is not None:
            return False

        referee.referred_by_user_id = referrer.id
        row = Referral(
            referrer_user_id=referrer.id,
            referee_user_id=referee.id,
            code_used=code,
            status=ReferralStatus.pending,
        )
        self._db.add(row)
        self._db.flush()

        # Fire-and-forget push to the referrer. We deliberately don't
        # block the register response on push delivery. If dispatch_delay
        # is unavailable (test harness without Celery loaded — shouldn't
        # happen but be defensive), swallow.
        try:
            from app.services.notification_service import NotificationEvent
            from app.workers.tasks.notification_tasks import dispatch_delay

            referee_display = _short_display_name(referee.full_name)
            dispatch_delay(
                user_id=str(referrer.id),
                user_email=referrer.email,
                event=NotificationEvent.referrer_signup_notified,
                context={
                    "referee_display_name": referee_display,
                    "reference": str(row.id),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "referral attribution: push enqueue failed referrer=%s err=%s",
                referrer.id, exc,
            )

        return True

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
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
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

        otp.used_at = datetime.now(UTC)
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
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
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

        otp.used_at = datetime.now(UTC)
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

        otp.used_at = datetime.now(UTC)
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

        # JWT-jti blocklist (Sprint 5c · Task 4.1).
        #
        # The rotation keyspace (RedisTokenStore) catches most revocation
        # scenarios — logout, replay, rotate-out. But the blocklist
        # (TokenRevocationService) is the future-proofing hook: anything
        # that needs to kill a specific jti (a password-change flow that
        # also revokes the active refresh, an admin "kill this token"
        # action) can write the same keyspace the access-token gate
        # already consults. If the refresh endpoint skipped this check,
        # a blocklisted refresh could still mint a new pair. Closing
        # that gap defensively here.
        if self._revocation_svc and await self._revocation_svc.is_revoked(old_jti):
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

        # Task 4.2: enforce "log me out everywhere" on refresh too.
        # ``get_current_user`` checks this for access tokens; we mirror
        # it here so an old refresh issued before a password change
        # can't be exchanged for a fresh access pair.
        # Note: uses ``<=`` rather than ``<`` so a same-second issue +
        # change pair (which integer-truncates to the same epoch
        # second) still revokes correctly. See the matching helper in
        # ``app/api/deps.py``.
        if user.tokens_revoked_at is not None:
            iat = payload.get("iat")
            if iat is not None:
                # iat is unix seconds (int) when decoded by jose
                revoked = user.tokens_revoked_at
                if revoked.tzinfo is None:
                    revoked = revoked.replace(tzinfo=UTC)
                if int(iat) <= int(revoked.timestamp()):
                    raise ValueError("INVALID_TOKEN")

        new_tokens, new_jti = _issue_token_pair(str(user.id))
        await self._tokens.save(user_id=str(user.id), jti=new_jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS)
        return new_tokens

    async def set_pin(self, user_id: UUID, pin: str) -> None:
        user = self._db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")
        user.pin_hash = hash_pin(pin)
        self._db.commit()

    async def change_pin(self, user_id: UUID, old_pin: str, new_pin: str) -> None:
        """Rotate an existing PIN. Requires the current PIN to verify.

        Raises:
          PIN_NOT_SET  — user has no PIN yet; they should hit /auth/pin/set instead.
          INVALID_PIN  — the supplied old_pin does not match the stored hash.
          USER_NOT_FOUND — user row missing (defensive; shouldn't happen
                           when called from an authenticated endpoint).

        Unlike /auth/password/change, a PIN change does NOT revoke
        access/refresh tokens: the PIN is a step-up factor on money
        operations, not the session-establishing credential. Killing
        sessions here would be a UX regression with no security gain —
        the PIN itself only matters at money-op time, where the new
        hash will be the one consulted.
        """
        user = self._db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("USER_NOT_FOUND")
        if user.pin_hash is None:
            raise ValueError("PIN_NOT_SET")
        if not verify_pin(old_pin, user.pin_hash):
            raise ValueError("INVALID_PIN")
        user.pin_hash = hash_pin(new_pin)
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
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
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

        otp.used_at = datetime.now(UTC)
        user.password_hash = hash_password(new_password)
        self._db.commit()

        await self._tokens.revoke_all(user_id=str(user.id))
