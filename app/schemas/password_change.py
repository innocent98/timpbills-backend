"""Request schema for /auth/password/change (Sprint 5c · Task 4.2).

Mirrors the validation rules from ``RegisterRequest.password`` so the
password rotation surface enforces the same minimum bar as initial
account creation. Diverging the two would mean a user could downgrade
their password strength via change — which is exactly the wrong shape.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain an uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain a lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain a digit")
        return v
