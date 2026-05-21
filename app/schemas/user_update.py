"""User profile update schemas (Sprint 5c · Task 2.1).

Used by ``PATCH /api/v1/auth/me`` to mutate the optional profile fields a
user can edit themselves: display name, date of birth, gender, postal
address. Email and phone are intentionally NOT in this schema — they each
have a dedicated verification flow elsewhere (email verification + phone
OTP upgrade) and must not be silently overwritten via the profile patch.

``extra="forbid"`` enforces that contract at the schema layer: a client
that POSTs ``{"email": "x"}`` to /me gets a 422 instead of a half-applied
mutation.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GenderEnum(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class UserUpdateRequest(BaseModel):
    """Patch payload for /auth/me.

    Only name / DOB / gender / address. Phone and email go through their
    own dedicated flows."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=80)
    date_of_birth: dt.date | None = None
    gender: GenderEnum | None = None
    address: str | None = Field(default=None, min_length=5, max_length=250)

    @field_validator("date_of_birth")
    @classmethod
    def validate_dob(cls, v: dt.date | None) -> dt.date | None:
        if v is None:
            return v
        today = dt.date.today()
        if v > today:
            raise ValueError("Date of birth cannot be in the future")
        age = today.year - v.year - ((today.month, today.day) < (v.month, v.day))
        if age < 18:
            raise ValueError("You must be at least 18 years old")
        if age > 120:
            raise ValueError("Date of birth is implausible")
        return v


class UserResponse(BaseModel):
    """Public projection of a User row — what /auth/me returns.

    Includes Sprint 5c profile-extension fields (date_of_birth, gender,
    address, avatar_url) alongside the existing identity + verification
    flags.  ``phone_verified`` is named differently from the ORM column
    (``is_phone_verified``); the response layer renames at the boundary
    so mobile sees a clean name.
    """

    user_id: str
    email: str
    phone: str
    full_name: str
    email_verified: bool
    phone_verified: bool
    pin_set: bool
    kyc_level: str
    date_of_birth: dt.date | None = None
    gender: str | None = None
    address: str | None = None
    avatar_url: str | None = None
