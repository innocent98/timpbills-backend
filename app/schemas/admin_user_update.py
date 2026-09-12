"""Admin user-edit schema — body for ``PATCH /admin/users/{user_id}``.

Lets an ops admin edit a user's basic identity fields (name, email, phone)
from the platform-admin user-profile page. Distinct from the self-service
``UserUpdateRequest`` (/auth/me) in two ways:

  * it CAN mutate email and phone (the admin is trusted, so there is no OTP
    step) whereas the self-service patch deliberately excludes them; and
  * every field is optional here for true PATCH semantics — the handler uses
    ``model_dump(exclude_unset=True)`` so an absent key is left untouched.

``extra="forbid"`` enforces the contract at the schema layer: a stray key
(e.g. ``kyc_tier``) is a 422 rather than a silently-ignored no-op, so the FE
learns immediately that the field is not editable through this endpoint.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AdminUserUpdateRequest(BaseModel):
    """Patch payload for the admin user-edit endpoint.

    ``full_name`` bounds mirror ``UserUpdateRequest`` (2..80). ``email`` shape
    is validated by ``EmailStr``; ``phone`` is a free-form string here and is
    normalised / validated server-side in the service (a bad value maps to a
    422 ``INVALID_PHONE``), so the FE never has to know the E.164 rules.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=80)
    email: EmailStr | None = None
    phone: str | None = None
