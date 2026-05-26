import pytest

from app.utils.phone import InvalidPhoneFormat, normalize_to_e164


@pytest.mark.parametrize("raw,expected", [
    ("08012345678", "+2348012345678"),
    ("2348012345678", "+2348012345678"),
    ("+2348012345678", "+2348012345678"),
    ("07012345678", "+2347012345678"),
    ("09012345678", "+2349012345678"),
    ("  08012345678  ", "+2348012345678"),
    ("0801 2345678", "+2348012345678"),  # NBSP (U+00A0) — would fail with old replace(" ","")
])
def test_normalize_valid(raw, expected):
    assert normalize_to_e164(raw) == expected


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "abc",
    "08abc",
    "0801234567",          # 10 digits — too short
    "080123456789",        # 12 digits — too long
    "0701234567",          # 10 digits — too short (moved from valid table)
    "+1234567890",         # non-NG country code
    "+234901234567",       # short national number
])
def test_normalize_invalid(bad):
    with pytest.raises(InvalidPhoneFormat):
        normalize_to_e164(bad)
