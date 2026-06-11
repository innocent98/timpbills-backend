"""Notification preference schemas (Sprint 5c · Task 2.3).

Mirrors the four columns on ``notification_preferences``. The response
shape is flat (no metadata wrapper) — mobile binds directly to these
four booleans.

PATCH uses ``extra="forbid"`` so a typo on the client (``sms_alerts``,
``push_alerts``, etc.) surfaces as a 422 instead of being silently
discarded.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NotificationPreferenceResponse(BaseModel):
    transaction_alerts: bool
    referral_updates: bool
    promotions: bool
    email_notifications: bool


class NotificationPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_alerts: bool | None = None
    referral_updates: bool | None = None
    promotions: bool | None = None
    email_notifications: bool | None = None
