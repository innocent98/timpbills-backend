"""Shared query-filter helpers for API endpoints.

``parse_enum_or_400`` converts a raw query-string value into an enum member,
raising a 400 (not a 500) when the value isn't a valid member. Used by the
admin list endpoints (transactions/users/refunds/notifications) so a bad
?status=/?type= filter is a client error, not an internal error.
"""
from enum import Enum
from typing import TypeVar

from fastapi import HTTPException

E = TypeVar("E", bound=Enum)


def parse_enum_or_400(enum_cls: type[E], value: str | None, *, field: str) -> E | None:
    """Return the enum member for ``value`` (case-sensitive, by value), or
    None when ``value`` is None/empty. Raise 400 ``INVALID_FILTER`` otherwise."""
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_FILTER",
                "message": f"Invalid value for {field}: {value!r}",
            },
        )
