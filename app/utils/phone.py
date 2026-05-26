"""Nigerian phone normalisation to E.164.

Accepts: 11-digit local (07/08/09 prefix), 13-digit international (234...),
or E.164 (+234...). Anything else raises InvalidPhoneFormat.
"""
import re


class InvalidPhoneFormat(ValueError):
    """Raised when a string cannot be normalised to E.164."""


_LOCAL_NG = re.compile(r"^0[789]\d{9}$")       # 11 digits, 0[789] prefix
_INTL_NG = re.compile(r"^234[789]\d{9}$")       # 13 digits, 234[789] prefix
_E164_NG = re.compile(r"^\+234[789]\d{9}$")     # +234[789] prefix


def normalize_to_e164(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        raise InvalidPhoneFormat("phone must be a non-empty string")
    s = raw.strip().replace(" ", "")
    if _E164_NG.match(s):
        return s
    if _INTL_NG.match(s):
        return f"+{s}"
    if _LOCAL_NG.match(s):
        return f"+234{s[1:]}"
    raise InvalidPhoneFormat(f"unrecognised phone format: {raw!r}")
