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

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.notification_preference import NotificationPreference
from app.db.models.user import User
from app.schemas.notification_preference import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
)
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
