import pytest
from app.utils.phone import normalize_to_e164, InvalidPhoneFormat


@pytest.mark.parametrize("raw,expected", [
    ("08012345678", "+2348012345678"),
    ("2348012345678", "+2348012345678"),
    ("+2348012345678", "+2348012345678"),
    ("0701234567", None),       # 10-digit NG number — invalid; only 11-digit local accepted
    ("07012345678", "+2347012345678"),
    ("09012345678", "+2349012345678"),
    ("  08012345678  ", "+2348012345678"),  # whitespace tolerated
])
def test_normalize_accepts_valid_formats(raw, expected):
    if expected is None:
        with pytest.raises(InvalidPhoneFormat):
            normalize_to_e164(raw)
    else:
        assert normalize_to_e164(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "abc", "08abc", "0801234567",       # 10 digits — too short
    "080123456789",                                  # 12 digits — too long
    "+1234567890",                                   # non-NG country code
    "+234901234567",                                 # short national number
])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(InvalidPhoneFormat):
        normalize_to_e164(bad)
