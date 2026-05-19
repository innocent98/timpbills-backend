"""User-facing referral endpoints (Sprint 5b/B3).

Two GETs, both auth-required:

* ``GET /users/me/referral`` — overview payload for the Earn tab home.
  Stable shape per spec §4.3 so the mobile UI can render "how it works"
  copy from the embedded ``config`` block without a second call.
* ``GET /users/me/referrals?limit=20&offset=0`` — paginated full history
  for the dedicated history screen. Offset pagination matches the
  transactions endpoint pattern; limit capped at 50.

Both endpoints:

* Hide referee-deleted rows from the activity feed (status=voided with
  void_reason=referee_deleted) — spec §6 says these should not appear
  to the referrer.
* Mask referee PII via ``_short_display_name`` (first + last initial).
* Surface the killswitch via the config block — when REFERRAL_ENABLED
  is false the rewards are reported as 0 so the mobile UI can hide /
  grey out the "how it works" panel without a separate flag.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter, per_user_or_ip
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.user import User
from app.schemas.referral import (
    ReferralActivityItem,
    ReferralConfig,
    ReferralHistoryResponse,
    ReferralOverviewResponse,
    ReferralStats,
)
from app.services.app_setting_service import AppSettingService
from app.services.auth_service import _short_display_name
from app.services.referral_service import VoidReason
from app.utils.responses import success

router = APIRouter(prefix="/users", tags=["referrals"])


# Share URL is hardcoded to the production domain. If the Sprint 6
# deep-link work adds a configurable host this becomes a settings read.
_SHARE_BASE_URL = "https://timpbills.com/r"


# Rows we never surface in either endpoint — referee-deleted voids are
# noise to the referrer (spec §6: "Hidden from referrer's activity feed").
_HIDDEN_VOID_REASONS = {VoidReason.referee_deleted}


def _hidden_filter():
    """SQLA filter clause for "exclude rows hidden from the referrer
    feed". Today the only hidden bucket is referee_deleted voids."""
    return ~(
        (Referral.status == ReferralStatus.voided)
        & (Referral.void_reason.in_(_HIDDEN_VOID_REASONS))
    )


def _build_activity_item(
    *, row: Referral, referee: Optional[User], referrer_reward: int,
) -> ReferralActivityItem:
    return ReferralActivityItem(
        id=str(row.id),
        status=row.status.value,
        referee_display_name=_short_display_name(
            referee.full_name if referee is not None else "",
        ),
        # Amount the referrer earned for this row. credited rows carry
        # the configured reward; everything else is 0 until / unless it
        # transitions to credited.
        amount_naira=(
            referrer_reward if row.status is ReferralStatus.credited else 0
        ),
        credited_at=row.credited_at,
        created_at=row.created_at,
    )


def _config_block(settings_svc: AppSettingService) -> ReferralConfig:
    """Read the three knobs that drive the Earn-tab "how it works" copy.

    When the killswitch is off we report 0s across the board so the
    mobile UI can render a "referrals paused" empty state from the same
    payload — avoids a second config endpoint or a separate killswitch
    flag the client has to interpret."""
    enabled = settings_svc.get_bool("REFERRAL_ENABLED", default=True)
    if not enabled:
        return ReferralConfig(
            referrer_reward_naira=0,
            referee_reward_naira=0,
            min_tx_amount_naira=0,
        )
    return ReferralConfig(
        referrer_reward_naira=int(
            settings_svc.get_decimal("REFERRAL_REWARD_REFERRER_NAIRA", default=Decimal("100"))
        ),
        referee_reward_naira=int(
            settings_svc.get_decimal("REFERRAL_REWARD_REFEREE_NAIRA", default=Decimal("50"))
        ),
        min_tx_amount_naira=int(
            settings_svc.get_decimal("REFERRAL_MIN_TX_AMOUNT_NAIRA", default=Decimal("1000"))
        ),
    )


def _compute_stats(db: Session, referrer_id) -> ReferralStats:
    """Count rows per bucket. Joined = everything ever (minus hidden);
    paid = credited; pending = pending / attributed / referee_cap_pending;
    voided = visible voids only."""
    base = db.query(Referral).filter(
        Referral.referrer_user_id == referrer_id,
        _hidden_filter(),
    )
    total = base.count()
    paid = base.filter(Referral.status == ReferralStatus.credited).count()
    pending = base.filter(
        Referral.status.in_(
            (
                ReferralStatus.pending,
                ReferralStatus.attributed,
                ReferralStatus.referee_cap_pending,
            )
        )
    ).count()
    voided = base.filter(Referral.status == ReferralStatus.voided).count()
    return ReferralStats(
        joined_count=total,
        paid_count=paid,
        pending_count=pending,
        voided_count=voided,
    )


@router.get("/me/referral", response_model=None)
@limiter.limit("60/minute", key_func=per_user_or_ip)
async def get_referral_overview(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings_svc = AppSettingService(db=db, ttl_seconds=0)
    config = _config_block(settings_svc)

    # Lifetime earnings is derived from the count of credited rows *
    # the *current* reward setting. The historical alternative — sum
    # individual amounts off a per-row column — isn't available because
    # the Referral row stores no per-row amount (wallet credit is the
    # authority). For v1 this is acceptable: an ops mid-flight reward
    # change retroactively re-prices old credits. Documented for the
    # mobile team in the status doc.
    referrer_reward = (
        config.referrer_reward_naira
        if config.referrer_reward_naira > 0
        else int(
            settings_svc.get_decimal(
                "REFERRAL_REWARD_REFERRER_NAIRA", default=Decimal("100")
            )
        )
    )
    credited_count = (
        db.query(Referral)
        .filter(
            Referral.referrer_user_id == user.id,
            Referral.status == ReferralStatus.credited,
        )
        .count()
    )
    lifetime_earned = credited_count * referrer_reward

    stats = _compute_stats(db, user.id)

    recent_rows = (
        db.query(Referral)
        .filter(
            Referral.referrer_user_id == user.id,
            _hidden_filter(),
        )
        .order_by(Referral.created_at.desc())
        .limit(5)
        .all()
    )
    referee_ids = [r.referee_user_id for r in recent_rows]
    referees_by_id: dict = {}
    if referee_ids:
        rows = db.query(User).filter(User.id.in_(referee_ids)).all()
        referees_by_id = {u.id: u for u in rows}

    recent_activity = [
        _build_activity_item(
            row=r,
            referee=referees_by_id.get(r.referee_user_id),
            referrer_reward=referrer_reward,
        )
        for r in recent_rows
    ]

    payload = ReferralOverviewResponse(
        code=user.referral_code,
        share_url=f"{_SHARE_BASE_URL}/{user.referral_code}",
        lifetime_earned_naira=lifetime_earned,
        stats=stats,
        recent_activity=recent_activity,
        config=config,
    )
    return success(
        payload.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/me/referrals", response_model=None)
@limiter.limit("60/minute", key_func=per_user_or_ip)
async def list_referrals(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
):
    settings_svc = AppSettingService(db=db, ttl_seconds=0)
    config = _config_block(settings_svc)
    referrer_reward = (
        config.referrer_reward_naira
        if config.referrer_reward_naira > 0
        else int(
            settings_svc.get_decimal(
                "REFERRAL_REWARD_REFERRER_NAIRA", default=Decimal("100")
            )
        )
    )

    base = db.query(Referral).filter(
        Referral.referrer_user_id == user.id,
        _hidden_filter(),
    )
    total = base.count()
    rows = (
        base.order_by(Referral.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    referee_ids = [r.referee_user_id for r in rows]
    referees_by_id: dict = {}
    if referee_ids:
        urows = db.query(User).filter(User.id.in_(referee_ids)).all()
        referees_by_id = {u.id: u for u in urows}

    items = [
        _build_activity_item(
            row=r,
            referee=referees_by_id.get(r.referee_user_id),
            referrer_reward=referrer_reward,
        )
        for r in rows
    ]
    payload = ReferralHistoryResponse(
        items=items, total=total, limit=limit, offset=offset,
    )
    return success(
        payload.model_dump(mode="json"),
        request_id=getattr(request.state, "request_id", None),
    )
