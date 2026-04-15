from fastapi import APIRouter, Depends, HTTPException, Request, status
from app.api.deps import get_auth_service, get_current_user
from app.db.models.user import User
from app.schemas.auth import (
    EmailVerifiedResponse,
    RegisterRequest,
    RegisterResponse,
    SendEmailOtpRequest,
    VerifyEmailOtpRequest,
    VerifyPhoneOtpRequest,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    SetPinRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
)
from app.services.auth_service import AuthService
from app.utils.responses import success
from app.core.limiter import limiter

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
