"""Unit tests for the shared ``parse_enum_or_400`` query-filter helper.

Sample enum is ``TransactionStatus`` — the helper is enum-agnostic but the
admin list endpoints feed it ``TransactionStatus``/``TransactionType``.
"""
import pytest
from fastapi import HTTPException

from app.api._filters import parse_enum_or_400
from app.db.models._enums import TransactionStatus


def test_valid_value_returns_member():
    assert (
        parse_enum_or_400(TransactionStatus, "failed", field="status")
        is TransactionStatus.failed
    )


def test_none_returns_none():
    assert parse_enum_or_400(TransactionStatus, None, field="status") is None


def test_empty_string_returns_none():
    assert parse_enum_or_400(TransactionStatus, "", field="status") is None


def test_invalid_value_raises_400_invalid_filter():
    with pytest.raises(HTTPException) as exc:
        parse_enum_or_400(TransactionStatus, "bogus", field="status")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INVALID_FILTER"
    assert "status" in exc.value.detail["message"]
