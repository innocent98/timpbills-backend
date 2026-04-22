"""Request/response schemas for /users/me/push-tokens."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PushTokenRequest(BaseModel):
    # FCM tokens are typically 152–200 chars, but Firebase reserves the
    # right to extend. 4096 is a generous upper bound that still refuses
    # obvious fuzz payloads.
    fcm_token: str = Field(min_length=1, max_length=4096)
    platform: Literal["ios", "android"]


class PushTokenResponse(BaseModel):
    id: UUID
    fcm_token: str
    platform: str
    last_seen_at: datetime
