"""Tests for UserUpdateRequest schema (Sprint 5c · Task 2.1).

Covers the validators that gate PATCH /auth/me:

* DOB future / age <18 / age >120 rejected; age == 18 accepted.
* All four GenderEnum values accepted; invalid gender rejected.
* full_name length bounds 2..80 enforced.
* address length bounds 5..250 enforced.
* extra="forbid" — email / phone (which have dedicated flows) are
  rejected so a misrouted client cannot silently mutate them via /me.
"""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.schemas.user_update import GenderEnum, UserUpdateRequest


# ---------------------------------------------------------------------------
# date_of_birth validator
# ---------------------------------------------------------------------------

def test_dob_future_rejected():
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    with pytest.raises(ValidationError) as exc:
        UserUpdateRequest(date_of_birth=tomorrow)
    assert "future" in str(exc.value).lower()


def test_dob_age_below_18_rejected():
    # 17 years 11 months — clearly under 18.
    today = dt.date.today()
    just_under_18 = dt.date(today.year - 17, today.month, today.day)
    with pytest.raises(ValidationError) as exc:
        UserUpdateRequest(date_of_birth=just_under_18)
    assert "18" in str(exc.value)


def test_dob_age_exactly_18_accepted():
    today = dt.date.today()
    eighteen_today = dt.date(today.year - 18, today.month, today.day)
    req = UserUpdateRequest(date_of_birth=eighteen_today)
    assert req.date_of_birth == eighteen_today


def test_dob_age_above_120_rejected():
    today = dt.date.today()
    too_old = dt.date(today.year - 121, today.month, today.day)
    with pytest.raises(ValidationError) as exc:
        UserUpdateRequest(date_of_birth=too_old)
    assert "implausible" in str(exc.value).lower()


def test_dob_none_passes():
    req = UserUpdateRequest(date_of_birth=None)
    assert req.date_of_birth is None


# ---------------------------------------------------------------------------
# gender
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    ["male", "female", "other", "prefer_not_to_say"],
)
def test_gender_accepted_values(value):
    req = UserUpdateRequest(gender=value)
    assert req.gender == GenderEnum(value)


def test_gender_invalid_rejected():
    with pytest.raises(ValidationError):
        UserUpdateRequest(gender="banana")


# ---------------------------------------------------------------------------
# full_name length bounds
# ---------------------------------------------------------------------------

def test_full_name_too_short_rejected():
    with pytest.raises(ValidationError):
        UserUpdateRequest(full_name="A")


def test_full_name_too_long_rejected():
    with pytest.raises(ValidationError):
        UserUpdateRequest(full_name="x" * 81)


def test_full_name_within_bounds_accepted():
    req = UserUpdateRequest(full_name="Ada")
    assert req.full_name == "Ada"


# ---------------------------------------------------------------------------
# address length bounds
# ---------------------------------------------------------------------------

def test_address_too_short_rejected():
    with pytest.raises(ValidationError):
        UserUpdateRequest(address="1234")


def test_address_too_long_rejected():
    with pytest.raises(ValidationError):
        UserUpdateRequest(address="x" * 251)


def test_address_within_bounds_accepted():
    req = UserUpdateRequest(address="42 Marina Road, Lagos")
    assert req.address == "42 Marina Road, Lagos"


# ---------------------------------------------------------------------------
# extra="forbid" — email/phone routed elsewhere
# ---------------------------------------------------------------------------

def test_email_rejected_as_extra():
    with pytest.raises(ValidationError) as exc:
        UserUpdateRequest.model_validate({"email": "x@y.co"})
    assert "email" in str(exc.value).lower()


def test_phone_rejected_as_extra():
    with pytest.raises(ValidationError) as exc:
        UserUpdateRequest.model_validate({"phone": "+2348022222222"})
    assert "phone" in str(exc.value).lower()


def test_empty_payload_valid():
    """An empty PATCH body is valid — every field is optional. The route
    layer is responsible for treating this as a no-op."""
    req = UserUpdateRequest()
    assert req.model_dump(exclude_unset=True) == {}
