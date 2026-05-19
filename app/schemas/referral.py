"""Pydantic schemas for the referral endpoints (Sprint 5b/B3).

Shape matches spec §4.3 exactly. ``referee_display_name`` is the
masked form (``Tobi A.``) — emails and phone numbers never appear in
these payloads.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReferralActivityItem(BaseModel):
    """A single row in the referrer's activity feed."""

    id: str
    status: str
    referee_display_name: str
    amount_naira: int
    credited_at: datetime | None = None
    created_at: datetime


class ReferralStats(BaseModel):
    joined_count: int
    paid_count: int
    pending_count: int
    voided_count: int


class ReferralConfig(BaseModel):
    referrer_reward_naira: int
    referee_reward_naira: int
    min_tx_amount_naira: int


class ReferralOverviewResponse(BaseModel):
    """Payload for ``GET /users/me/referral`` — the Earn tab home."""

    code: str
    share_url: str
    lifetime_earned_naira: int
    stats: ReferralStats
    recent_activity: list[ReferralActivityItem]
    config: ReferralConfig


class ReferralHistoryResponse(BaseModel):
    """Payload for ``GET /users/me/referrals`` — paginated activity."""

    items: list[ReferralActivityItem]
    total: int
    limit: int
    offset: int
