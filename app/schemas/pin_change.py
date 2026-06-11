"""Request schema for /auth/pin/change (Sprint 5c · Task 4.3).

Same 4-digit numeric format as ``SetPinRequest``: we mirror the validation
rules so a user can't downgrade from a 4-digit PIN to anything weaker via
the change surface.

Distinct from /auth/pin/set:
  * /auth/pin/set creates the first PIN on accounts where ``pin_hash`` is
    NULL — single ``pin`` field, no old to verify.
  * /auth/pin/change rotates an existing PIN — requires the current PIN
    so a stolen access token alone can't pivot the PIN.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PinChangeRequest(BaseModel):
    old_pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    new_pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
