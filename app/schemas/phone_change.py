"""Request schemas for /auth/phone/change-request + /auth/phone/change-confirm
(Sprint 5c · Task 5.1).

The flow is two-step so OTP delivery to the *new* phone number proves the
user actually controls it before we overwrite the column:

  1. ``PhoneChangeRequest`` — submit the new phone; backend generates a
     short-lived OTP keyed by an opaque ``request_id`` and SMSes the OTP
     to that new phone via Termii.
  2. ``PhoneChangeConfirm`` — submit the ``request_id`` + OTP. On match,
     ``users.phone`` is rewritten and every session is revoked.

The ``new_phone`` pattern matches the existing E.164-NG format used by
``RegisterRequest`` (``^\\+234[0-9]{10}$``) — we deliberately do not
broaden the format here. If we ever support diaspora users, this and
register.py both move in lockstep.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PhoneChangeRequest(BaseModel):
    new_phone: str = Field(pattern=r"^\+234[0-9]{10}$")


class PhoneChangeConfirm(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    otp: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")
