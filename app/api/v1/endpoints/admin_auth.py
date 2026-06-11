"""Admin auth — login/logout. Opaque session cookie, not JWT.

Kept separate from admin.py (ops endpoints) so each file has one job.
Login validates credentials → mints an opaque session in Redis (via
``AdminSessionStore``) → sets the httpOnly ``admin_session`` cookie plus a
JS-readable ``admin_csrf`` cookie for the double-submit CSRF check. Logout
deletes the session and clears both cookies.
"""
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_admin_session_store, get_db
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import hash_password, verify_password_async
from app.db.models.admin_user import AdminUser
from app.services.admin_session_store import AdminSessionStore
from app.utils.responses import success

router = APIRouter(prefix="/admin", tags=["admin-auth"])

# Timing-equalizer hash for the no-such-admin path. Verifying the supplied
# password against a real argon2 hash even when the email doesn't exist keeps
# login latency constant whether or not the account is real, so an attacker
# can't enumerate admin emails by response time. Computed lazily once.
_dummy_hash: str | None = None


def _timing_equalizer_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer-not-a-real-secret")
    return _dummy_hash


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


def _set_admin_cookies(response: Response, *, sid: str, csrf: str) -> None:
    common = {
        "domain": settings.ADMIN_COOKIE_DOMAIN,
        "secure": settings.ADMIN_COOKIE_SECURE,
        "samesite": "lax",
        "max_age": settings.ADMIN_SESSION_TTL_SECONDS,
        "path": "/",
    }
    response.set_cookie(
        settings.ADMIN_SESSION_COOKIE_NAME, sid, httponly=True, **common
    )
    # csrf cookie is readable by JS (double-submit), so httponly=False.
    response.set_cookie(
        settings.ADMIN_CSRF_COOKIE_NAME, csrf, httponly=False, **common
    )


@router.post("/login", response_model=None)
@limiter.limit("5/minute")
async def admin_login(
    request: Request,
    response: Response,
    body: AdminLoginRequest,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
):
    admin = (
        db.query(AdminUser)
        .filter(AdminUser.email == body.email.lower())
        .first()
    )
    # Always run a password verify — against the real hash if the admin exists,
    # otherwise against a dummy hash — so the no-such-email and wrong-password
    # paths take the same time (no email enumeration via timing).
    hashed = admin.password_hash if admin is not None else _timing_equalizer_hash()
    password_ok = await verify_password_async(body.password, hashed)
    if admin is None or not password_ok:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "ADMIN_INVALID_CREDENTIALS",
                "message": "Invalid credentials",
            },
        )
    if admin.is_active is False:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_DISABLED", "message": "Admin account disabled"},
        )

    sid = await store.create(admin_id=str(admin.id), role=admin.role.value)
    csrf = secrets.token_urlsafe(32)
    admin.last_login_at = datetime.now(UTC)
    db.commit()
    _set_admin_cookies(response, sid=sid, csrf=csrf)

    return success(
        {
            "admin": {
                "id": str(admin.id),
                "email": admin.email,
                "full_name": admin.full_name,
                "role": admin.role.value,
            },
            "csrf_token": csrf,
        },
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/logout", response_model=None)
async def admin_logout(
    request: Request,
    response: Response,
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
    admin_session: Annotated[
        str | None, Cookie(alias=settings.ADMIN_SESSION_COOKIE_NAME)
    ] = None,
):
    if admin_session:
        await store.delete(admin_session)
    response.delete_cookie(
        settings.ADMIN_SESSION_COOKIE_NAME,
        path="/",
        domain=settings.ADMIN_COOKIE_DOMAIN,
    )
    response.delete_cookie(
        settings.ADMIN_CSRF_COOKIE_NAME,
        path="/",
        domain=settings.ADMIN_COOKIE_DOMAIN,
    )
    return success(
        {"logged_out": True},
        request_id=getattr(request.state, "request_id", None),
    )
