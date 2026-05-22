from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_auth_service,
    get_current_token_claims,
    get_current_user,
    get_db,
    get_pin_service,
    get_token_revocation_service,
    get_token_store,
)
from app.core.limiter import limiter
from app.core.security import decode_token, hash_password, verify_password
from app.db.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SendEmailOtpRequest,
    SetPinRequest,
    VerifyEmailOtpRequest,
    VerifyPhoneOtpRequest,
)
from app.schemas.password_change import PasswordChangeRequest
from app.schemas.phone_change import PhoneChangeConfirm, PhoneChangeRequest
from app.schemas.pin import VerifyPinRequest, VerifyPinResponse
from app.schemas.pin_change import PinChangeRequest
from app.schemas.user_update import GenderEnum, UserResponse, UserUpdateRequest
from app.services.auth_service import AuthService
from app.services.pin_service import InvalidPin, PinLocked, PinNotSet, PinService
from app.services.token_revocation_service import TokenRevocationService
from app.services.token_store import TokenStore
from app.utils.responses import success

router = APIRouter(prefix="/auth", tags=["auth"])


def _build_me_response(user: User) -> UserResponse:
    """Project a User row to the public /me shape.

    Centralised so GET and PATCH /me cannot drift — both must surface
    the same view of identity + verification + profile-extension fields.

    NOTE on the field rename: the ORM column is ``is_phone_verified`` but
    the response field is ``phone_verified`` (matches mobile's existing
    contract). We keep the rename in this single place.
    """
    return UserResponse(
        user_id=str(user.id),
        email=user.email,
        phone=user.phone,
        full_name=user.full_name,
        email_verified=user.email_verified,
        phone_verified=user.is_phone_verified,
        pin_set=user.pin_hash is not None,
        kyc_level=user.kyc_level.numeric,
        date_of_birth=user.date_of_birth,
        gender=user.gender,
        address=user.address,
        avatar_url=user.avatar_url,
    )

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
    # Sprint 5c · Task 5.1 — phone change flow.
    "PHONE_ALREADY_IN_USE": (409, "Phone already in use by another account"),
    "INVALID_REQUEST":      (400, "Invalid or expired phone change request"),
    "USER_MISMATCH":        (403, "Phone change request belongs to a different user"),
    # Sprint 5c · Task 6.1 — soft-delete re-register block.
    "PHONE_RECENTLY_DELETED": (
        409,
        "This phone number was recently used by a deleted account. Try again after 30 days.",
    ),
    "EMAIL_RECENTLY_DELETED": (
        409,
        "This email was recently used by a deleted account. Try again after 30 days.",
    ),
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
# Phone change (Sprint 5c · Task 5.1)
#
# Distinct from /phone/send-otp + /phone/verify-otp above (which upgrade a
# Tier 0 → Tier 1 by verifying the phone on file). This pair lets an
# already-verified user rotate to a *different* phone number — OTP is
# delivered to the NEW phone to prove control, and confirm revokes every
# outstanding session.
# ---------------------------------------------------------------------------

@router.post("/phone/change-request")
@limiter.limit("3/minute")
async def request_phone_change_endpoint(
    request: Request,
    req: PhoneChangeRequest,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    try:
        request_id = await svc.request_phone_change(
            user_id=user.id, new_phone=req.new_phone,
        )
    except ValueError as e:
        _raise(str(e))
    return success(
        {"request_id": request_id},
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/phone/change-confirm")
@limiter.limit("5/minute")
async def confirm_phone_change_endpoint(
    request: Request,
    req: PhoneChangeConfirm,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    try:
        await svc.confirm_phone_change(
            user_id=user.id, request_id=req.request_id, otp=req.otp,
        )
    except ValueError as e:
        _raise(str(e))
    return success(
        {"ok": True},
        request_id=getattr(request.state, "request_id", None),
    )


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


@router.post("/logout", status_code=http_status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    body: LogoutRequest | None = Body(default=None),
    claims: dict = Depends(get_current_token_claims),
    revocation_svc: TokenRevocationService = Depends(get_token_revocation_service),
    token_store: TokenStore = Depends(get_token_store),
):
    """Revoke the current access token (and optionally the refresh token).

    Idempotent: calling logout twice with the same bearer succeeds —
    the second call writes the same blocklist entry with the same TTL
    floor. The route returns 204 either way.

    The access token's jti is added to the JWT blocklist
    (``revoked:jwt:{jti}``) for the remainder of its lifetime. If the
    body carries a refresh token, its rotation entry is also dropped
    from ``RedisTokenStore`` — the next /refresh attempt with that
    token surfaces as the existing replay-detection branch and nukes
    every session for the user.
    """
    jti = claims.get("jti")
    exp = int(claims.get("exp", 0))
    if jti and exp:
        await revocation_svc.revoke(jti=jti, exp_unix_seconds=exp)

    if body and body.refresh_token:
        try:
            refresh_payload = decode_token(body.refresh_token)
        except Exception:  # noqa: BLE001 — silently ignore bad refresh tokens
            refresh_payload = None
        if (
            refresh_payload
            and refresh_payload.get("typ") == "refresh"
            and refresh_payload.get("jti")
            and refresh_payload.get("sub") == claims.get("sub")
        ):
            await token_store.revoke(
                user_id=refresh_payload["sub"],
                jti=refresh_payload["jti"],
            )
    return None


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


@router.post("/pin/change")
async def change_pin(
    request: Request,
    req: PinChangeRequest,
    svc: AuthService = Depends(get_auth_service),
    user: User = Depends(get_current_user),
):
    """Rotate an existing PIN. Requires the current PIN to verify.

    Distinct from /pin/set, which creates the first PIN on accounts where
    pin_hash is NULL. /pin/change refuses to operate without an existing PIN.
    Unlike /password/change, PIN rotation does NOT revoke sessions — see
    AuthService.change_pin for the rationale.
    """
    try:
        await svc.change_pin(user_id=user.id, old_pin=req.old_pin, new_pin=req.new_pin)
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


@router.post("/password/change", status_code=http_status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    token_store: TokenStore = Depends(get_token_store),
):
    """Change the authenticated user's password and revoke ALL sessions.

    Flow:
      1. Verify the supplied ``old_password`` matches the stored hash.
         400 with INVALID_CREDENTIALS on mismatch — same code shape as
         /auth/login uses, so mobile can reuse the same handler.
      2. Re-hash ``new_password`` and write it back.
      3. Stamp ``users.tokens_revoked_at = now()`` — every access token
         issued before this instant is now rejected at the gate via
         ``get_current_user``.
      4. Revoke every refresh token in the rotation keyspace
         (``RedisTokenStore.revoke_all``) so the user's existing devices
         can't refresh themselves back to life.

    Returns 204 — the caller's own current token is now revoked too, so
    we deliberately don't echo any state back. The mobile flow is:
    receive 204 → discard local tokens → re-login.

    Rate-limited 5/min to avoid letting a stolen access token brute-force
    the old password by repeatedly trying values.
    """
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_CREDENTIALS",
                "message": "Current password is incorrect",
            },
        )

    user.password_hash = hash_password(payload.new_password)
    user.tokens_revoked_at = datetime.now(UTC)
    db.add(user)
    db.commit()

    # Revoke every outstanding refresh token. The access-token side is
    # covered by ``tokens_revoked_at`` — no per-jti scan needed because
    # the gate consults ``user.tokens_revoked_at`` on every protected
    # call.
    await token_store.revoke_all(user_id=str(user.id))
    return None


# ---------------------------------------------------------------------------
# Session validation — used by Flutter splash to trust a stale access token
# ---------------------------------------------------------------------------

@router.get("/me")
async def me(request: Request, user: User = Depends(get_current_user)):
    return success(
        _build_me_response(user).model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.patch("/me")
async def patch_me(
    request: Request,
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Partially update the authenticated user's profile.

    Only ``full_name`` / ``date_of_birth`` / ``gender`` / ``address`` are
    editable here. Pydantic's ``extra="forbid"`` rejects ``email`` and
    ``phone`` (which have dedicated verification flows) with 422 before
    we reach this body. ``model_dump(exclude_unset=True)`` makes this a
    true PATCH — keys the client did not send are left untouched.
    """
    update_dict = payload.model_dump(exclude_unset=True)

    # GenderEnum serialises to the enum member by default; the DB column
    # holds the raw string ("male"/"female"/...), so unwrap before assign.
    if "gender" in update_dict and isinstance(update_dict["gender"], GenderEnum):
        update_dict["gender"] = update_dict["gender"].value

    for key, value in update_dict.items():
        setattr(user, key, value)

    db.add(user)
    db.commit()
    db.refresh(user)

    return success(
        _build_me_response(user).model_dump(mode="json"),
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
