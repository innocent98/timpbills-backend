import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.utils.email import normalize_email


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    # B8: format checked at the service layer via ``normalize_to_e164``
    # so a bad phone surfaces as 400 INVALID_PHONE_FORMAT, not a generic
    # 422 VALIDATION_ERROR. The previous regex-based field_validator was
    # removed deliberately.
    phone: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # Sprint 5b: optional referral code at signup. Invalid / missing
    # codes are non-fatal — the user is registered regardless. See
    # spec §5.2 and the auth_service.register flow.
    referral_code: str | None = Field(default=None, max_length=8)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain an uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain a lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain a digit")
        return v

    @field_validator("referral_code")
    @classmethod
    def _normalise_referral_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        return v or None

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        # Case-insensitive email: ``Example@X.com`` and ``example@x.com``
        # must resolve to the same account. See app.utils.email.
        return normalize_email(v)


class RegisterResponse(BaseModel):
    user_id: str
    email: str
    phone: str
    # Sprint 5b/B4: true when a referral_code was supplied AND attribution
    # succeeded (a pending `referrals` row was created). False when no code
    # was supplied OR the code was rejected (invalid, self-referral,
    # killswitch off, referrer inactive, etc.). Lets mobile surface a
    # soft-fail SnackBar when a code was sent but silently dropped.
    referred_by: bool = False
    # B8: phone-only-auth — register no longer issues tokens. Mobile
    # routes off ``next_action`` to the verify-email-and-phone step.
    next_action: Literal["verify_email_and_phone"] = "verify_email_and_phone"


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class AuthTokens(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


class VerifyOtpResponse(BaseModel):
    tokens: AuthTokens
    pin_set: bool


# --- Email verification ---

class SendEmailOtpRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return normalize_email(v)


class VerifyEmailOtpRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return normalize_email(v)


class EmailVerifiedResponse(BaseModel):
    email_verified: bool
    phone_verified: bool
    pin_set: bool
    next_action: Literal[
        "phone_verification_required",
        "pin_setup_required",
        "tokens_issued",
    ]
    pin_setup_token: str | None = None
    tokens: AuthTokens | None = None
    # Populated only on the ``phone_verification_required`` branch: True
    # when the lazy phone OTP was dispatched at this step, False when the
    # cooldown / daily-cap helper blocked the send (mobile then shows the
    # existing-OTP countdown instead of a "we just sent it" toast). Left
    # at the default for the pin_setup_required / tokens_issued branches.
    phone_otp_sent: bool = False


# --- Phone verification (signup + existing-user migration; unauthenticated) ---
#
# B10: distinct from VerifyPhoneOtpRequest below, which is the body for the
# *authenticated* /auth/phone/verify-otp endpoint (in-session Tier 1 upgrade).
# This pair is consumed by the unauth /auth/phone/verify endpoint that mirrors
# /auth/email/verify and emits the same next_action / pin_setup_token shape.

class PhoneVerifyRequest(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class PhoneResendRequest(BaseModel):
    # Public signup resend — no ``code`` (distinct from PhoneVerifyRequest):
    # the user is re-requesting an OTP, not submitting one.
    phone: str


class PhoneVerifiedResponse(BaseModel):
    email_verified: bool
    phone_verified: bool
    pin_set: bool
    next_action: Literal[
        "email_verification_required",
        "pin_setup_required",
        "tokens_issued",
    ]
    pin_setup_token: str | None = None
    tokens: AuthTokens | None = None


# --- Phone verification (on-demand upgrade) ---

class SendPhoneOtpRequest(BaseModel):
    """Request body for on-demand phone OTP send. Phone comes from the authenticated user."""

    pass  # no fields — phone is taken from the authenticated user's profile


class VerifyPhoneOtpRequest(BaseModel):
    """Request body for phone OTP verification (authenticated endpoint)."""

    code: str = Field(min_length=6, max_length=6)


class LoginRequest(BaseModel):
    # B12: phone-only login. Format is validated at the service layer via
    # ``normalize_to_e164`` so a bad value surfaces as 400
    # INVALID_PHONE_FORMAT rather than a generic 422 VALIDATION_ERROR.
    # The old ``identifier`` field (email|phone) is removed deliberately.
    phone: str
    password: str


class LoginResponse(BaseModel):
    """B12: shape mirrors the post-verify response — mobile reuses the
    same router that consumes /auth/email/verify and /auth/phone/verify.

    Gate evaluation order is email → phone → pin; ``next_action`` reports
    the first unverified gate. Tokens / pin_setup_token are populated
    only on the branch they apply to; ``phone_otp_sent`` / ``email_otp_sent``
    are populated only on their respective verification branch and report
    whether the inline OTP dispatch succeeded (False when blocked by
    cooldown / daily cap, or when the send path errored).

    ``email`` is the authenticated user's email — always present so mobile
    can prefill the verify-email screen on the ``email_verification_required``
    branch (the client no longer holds the address on a returning-user login).
    """

    next_action: Literal[
        "tokens_issued",
        "email_verification_required",
        "phone_verification_required",
        "pin_setup_required",
    ]
    pin_set: bool
    email: str
    tokens: AuthTokens | None = None
    pin_setup_token: str | None = None
    phone_otp_sent: bool | None = None
    email_otp_sent: bool | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """Optional body for /auth/logout (Sprint 5c · Task 3.3).

    The access token's jti is always revoked via the JWT blocklist.
    When ``refresh_token`` is supplied, its rotation entry is also
    deleted from the existing ``RedisTokenStore`` so the device's
    session can't refresh itself back to life.
    """

    refresh_token: str | None = None


class SetPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


# B11: distinct request/response for the rewritten /auth/pin/set, which
# is gated by an ``X-Pin-Setup-Token`` header (scoped JWT issued by
# /auth/email/verify, /auth/phone/verify, or /auth/login) instead of
# a bearer access token. The token is consumed one-time: the endpoint
# blocklists its jti on success, then mints a full access+refresh pair.
class SetPinFirstTimeRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class SetPinFirstTimeResponse(BaseModel):
    pin_set: bool
    tokens: AuthTokens


# B13: /auth/pin-login — cold-start PIN authentication.
#
# Mobile boots without a fresh access token (e.g. after a kill+relaunch
# past the access TTL) but still holds the persisted refresh token. It
# asks the user for the 4-digit PIN and trades {refresh_token, pin} for
# a brand-new access+refresh pair. Reuses PinService's lockout machinery
# so brute-force attempts share the counter with /auth/pin/verify.
class PinLoginRequest(BaseModel):
    refresh_token: str
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class PinLoginResponse(BaseModel):
    tokens: AuthTokens
    # pin_set is always True on this branch (no PIN → no login at all),
    # but we surface it explicitly so mobile's routing code can treat the
    # response identically to /auth/email/verify and /auth/login.
    pin_set: bool = True


def _normalise_identifier(v: str) -> str:
    # ``identifier`` is a combined email|phone value. Lowercase only when it
    # looks like an email (contains "@") so a phone number keeps its exact
    # form (phone normalisation to E.164 happens at the service layer). This
    # keeps ``Example@X.com`` matching the stored lowercased email.
    return normalize_email(v) if "@" in v else v


class ForgotPasswordRequest(BaseModel):
    identifier: str

    @field_validator("identifier")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _normalise_identifier(v)


class ResetPasswordRequest(BaseModel):
    identifier: str
    code: str = Field(min_length=6, max_length=6)
    new_password: str

    @field_validator("identifier")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return _normalise_identifier(v)
