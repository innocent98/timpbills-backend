"""User-scoped endpoints other than auth (Sprint 5c · Task 2.3).

Currently exposes notification-preference GET/PATCH. The router lives
under ``/users`` rather than ``/auth`` because these endpoints describe
the *user resource* — settings the user owns — not the authentication
flow. As more profile-side endpoints land (avatar upload, contact
preference history, etc.) they accumulate here.

NOTE: row is provisioned lazily. We don't backfill a
``notification_preferences`` row at registration; instead the first
GET/PATCH creates it with spec-default values. That keeps the
registration flow simple at the cost of one extra INSERT on first
read — acceptable because the row is one boolean tuple.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_avatar_service, get_current_user, get_db
from app.db.models.notification_preference import NotificationPreference
from app.db.models.user import User
from app.schemas.notification_preference import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
)
from app.services.avatar_service import AvatarService, AvatarUploadError
from app.utils.responses import success

router = APIRouter(prefix="/users", tags=["users"])


def _get_or_create_prefs(db: Session, user: User) -> NotificationPreference:
    """Fetch the user's preference row, creating one with defaults if missing.

    Defaults live on the SQLAlchemy column declarations (see
    ``app/db/models/notification_preference.py``); we just instantiate
    with ``user_id`` and let the ORM apply them.
    """
    prefs = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user.id)
        .one_or_none()
    )
    if prefs is None:
        prefs = NotificationPreference(user_id=user.id)
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
    return prefs


def _prefs_payload(prefs: NotificationPreference) -> NotificationPreferenceResponse:
    return NotificationPreferenceResponse(
        transaction_alerts=prefs.transaction_alerts,
        referral_updates=prefs.referral_updates,
        promotions=prefs.promotions,
        email_notifications=prefs.email_notifications,
    )


@router.get("/me/notification-preferences")
def get_notification_prefs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    prefs = _get_or_create_prefs(db, current_user)
    return success(
        _prefs_payload(prefs).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


@router.patch("/me/notification-preferences")
def patch_notification_prefs(
    request: Request,
    payload: NotificationPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Partial update — fields not present in the body keep their value."""
    prefs = _get_or_create_prefs(db, current_user)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(prefs, key, value)
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return success(
        _prefs_payload(prefs).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# Avatar upload / delete (Sprint 5c · Task 3.2)
# ---------------------------------------------------------------------------

@router.post("/me/avatar")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    avatar_svc: AvatarService = Depends(get_avatar_service),
):
    """Upload (or replace) the authenticated user's avatar.

    Validation happens in AvatarService:
      * > 5 MB → 413 Request Entity Too Large
      * non JPEG/PNG → 415 Unsupported Media Type
      * any Cloudinary failure → 502 Bad Gateway (upstream)

    Cloudinary's public_id is the user UUID, so re-uploading overwrites
    the previous asset — only one avatar per user lives in the cloud.
    """
    contents = await file.read()
    try:
        url = avatar_svc.upload_avatar(
            user_id=str(current_user.id),
            file_bytes=contents,
            content_type=file.content_type or "",
        )
    except AvatarUploadError as e:
        msg = str(e)
        if "exceeds" in msg:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"code": "AVATAR_TOO_LARGE", "message": msg},
            )
        if "JPEG or PNG" in msg:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"code": "AVATAR_WRONG_MIME", "message": msg},
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "AVATAR_UPSTREAM_FAILURE", "message": msg},
        )

    current_user.avatar_url = url
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return success(
        {"avatar_url": url},
        request_id=getattr(request.state, "request_id", None),
    )


@router.delete("/me/avatar")
def delete_avatar(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Drop the user's avatar reference.

    We don't proactively delete the Cloudinary asset — overwrite-on-next-upload
    keeps the bucket bounded (one asset per user) and a stale asset
    behind no URL costs effectively nothing.
    """
    current_user.avatar_url = None
    db.add(current_user)
    db.commit()
    return success(
        {"avatar_url": None},
        request_id=getattr(request.state, "request_id", None),
    )
