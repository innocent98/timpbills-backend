from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_auth_service, get_current_user, get_pin_service
from app.core.limiter import limiter
from app.db.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SendEmailOtpRequest,
    SetPinRequest,
    VerifyEmailOtpRequest,
    VerifyPhoneOtpRequest,
)
from app.schemas.pin import VerifyPinRequest, VerifyPinResponse
from app.services.auth_service import AuthService
from app.services.pin_service import InvalidPin, PinLocked, PinNotSet, PinService
from app.utils.responses import success

router = APIRouter(prefix="/auth", tags=["auth"])

_ERROR_MAP: dict[str, tuple[int, str]] = {
    "USER_ALREADY_EXISTS": (409, "User already exists"),
    "USER_NOT_FOUND": (404, "User not found"),
    "NO_ACTIVE_OTP": (400, "No active OTP"),
    "OTP_EXPIRED": (410, "OTP expired"),
    "OTP_ATTEMPTS_EXCEEDED": (429, "Too many attempts"),
    "INVALID_OTP": (400, "Invalid OTP"),
    "INVALID_CREDENTIALS": (401, "Invalid credentials"),
    "INVALID_TOKEN": (401, "Invalid or expired token"),
    "EMAIL_ALREADY_VERIFIED": (409, "Email already verified"),
    "PHONE_ALREADY_VERIFIED": (409, "Phone already verified"),
    "EMAIL_NOT_VERIFIED": (403, "Email not verified"),
    "ACCOUNT_DISABLED": (403, "Account is disabled"),
    "IDEMPOTENCY_KEY_REQUIRED": (400, "Idempotency-Key header required"),
    "IDEMPOTENCY_CONFLICT":     (409, "Idempotency key reused with different request"),
    "PIN_NOT_SET":        (400, "PIN has not been set"),
    "PIN_LOCKED":         (423, "PIN is locked due to too many attempts"),
    "INVALID_PIN":        (401, "Invalid PIN"),
    "PIN_TOKEN_REQUIRED": (401, "X-Pin-Token header required"),
    "INVALID_PIN_TOKEN":  (401, "Invalid or expired PIN token"),
}


def _raise(code: str) -> None:
    http_code, msg = _ERROR_MAP.get(code, (500, code))
    raise HTTPException(status_code=http_code, detail={"code": code, "message": msg})


@router.post("/register", status_code=201)
@limiter.limit("3/minute")
async def register(
    request: Request,
    req: RegisterRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        res = await svc.register(req)
    except ValueError as e:
        _raise(str(e))
    return success(res.model_dump(), request_id=getattr(request.state, "request_id", None))


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@router.post("/email/verify")
@limiter.limit("5/minute")
async def verify_email_otp(
    request: Request,
    req: VerifyEmailOtpRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        res = await svc.verify_email_otp(req)
    except ValueError as e:
        _raise(str(e))
    return success(res.model_dump(), request_id=getattr(request.state, "request_id", None))


@router.post("/email/resend")
@limiter.limit("3/minute")
async def resend_email_otp(
    request: Request,
    req: SendEmailOtpRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        await svc.send_email_otp(req.email)
    except ValueError as e:
        _raise(str(e))
    return success({"ok": True}, request_id=getattr(request.state, "request_id", None))


# ---------------------------------------------------------------------------
# Phone verification (authenticated, on-demand Tier 1 upgrade)
# ---------------------------------------------------------------------------

@router.post("/phone/send-otp")
@limiter.limit("3/minute")
async def send_phone_otp(
    request: Request,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    try:
        await svc.send_phone_otp(user_id=user.id)
    except ValueError as e:
        _raise(str(e))
    return success({"ok": True}, request_id=getattr(request.state, "request_id", None))


@router.post("/phone/verify-otp")
@limiter.limit("5/minute")
async def verify_phone_otp(
    request: Request,
    req: VerifyPhoneOtpRequest,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    try:
        res = await svc.verify_phone_otp(user_id=user.id, code=req.code)
    except ValueError as e:
        _raise(str(e))
    return success(res.model_dump(), request_id=getattr(request.state, "request_id", None))


# ---------------------------------------------------------------------------
# Standard auth endpoints
# ---------------------------------------------------------------------------

@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    req: LoginRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        res = await svc.login(req)
    except ValueError as e:
        _raise(str(e))
    return success(res.model_dump(), request_id=getattr(request.state, "request_id", None))


@router.post("/refresh")
async def refresh(
    request: Request,
    req: RefreshRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        res = await svc.refresh(req.refresh_token)
    except ValueError as e:
        _raise(str(e))
    return success(res.model_dump(), request_id=getattr(request.state, "request_id", None))


@router.post("/pin/set")
async def set_pin(
    request: Request,
    req: SetPinRequest,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    try:
        await svc.set_pin(user_id=user.id, pin=req.pin)
    except ValueError as e:
        _raise(str(e))
    return success({"ok": True}, request_id=getattr(request.state, "request_id", None))


@router.post("/password/forgot")
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    req: ForgotPasswordRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        await svc.forgot_password(req.identifier)
    except ValueError:
        # silent success to prevent enumeration
        pass
    return success({"ok": True}, request_id=getattr(request.state, "request_id", None))


@router.post("/password/reset")
async def reset_password(
    request: Request,
    req: ResetPasswordRequest,
    svc: AuthService = Depends(get_auth_service),
):
    try:
        await svc.reset_password(
            identifier=req.identifier, code=req.code, new_password=req.new_password
        )
    except ValueError as e:
        _raise(str(e))
    return success({"ok": True}, request_id=getattr(request.state, "request_id", None))


# ---------------------------------------------------------------------------
# Session validation — used by Flutter splash to trust a stale access token
# ---------------------------------------------------------------------------

@router.get("/me")
async def me(request: Request, user: User = Depends(get_current_user)):
    return success(
        {
            "user_id": str(user.id),
            "phone": user.phone,
            "email": user.email,
            "full_name": user.full_name,
            "email_verified": user.email_verified,
            "phone_verified": user.is_phone_verified,
            "pin_set": user.pin_hash is not None,
            "kyc_level": user.kyc_level.value,
        },
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# PIN verification — issues a short-lived money-ops JWT
# ---------------------------------------------------------------------------

@router.post("/pin/verify", response_model=None)
@limiter.limit("5/minute")
async def verify_pin_endpoint(
    request: Request,
    body: VerifyPinRequest,
    svc: PinService = Depends(get_pin_service),
    user: User = Depends(get_current_user),
):
    try:
        token = await svc.verify_async(user_id=user.id, pin=body.pin)
    except PinNotSet:
        _raise("PIN_NOT_SET")
    except PinLocked:
        _raise("PIN_LOCKED")
    except InvalidPin:
        _raise("INVALID_PIN")

    return success(
        VerifyPinResponse(pin_token=token, expires_in=300).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
