import pytest
from datetime import timedelta
from jose import jwt

from app.core.config import settings
from app.core.security import (
    create_pin_setup_token,
    verify_pin_setup_token,
    InvalidPinSetupToken,
)


def test_create_and_verify_round_trip():
    token = create_pin_setup_token(user_id="u-1")
    claims = verify_pin_setup_token(token)
    assert claims["sub"] == "u-1"
    assert claims["scope"] == "pin_setup"
    assert "jti" in claims


def test_rejects_non_pin_setup_scope():
    """A regular access token must not be accepted as a pin_setup token."""
    other = jwt.encode(
        {"sub": "u-1", "scope": "access"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token(other)


def test_rejects_token_missing_scope_claim():
    """A token without any scope claim must fail — not silently pass."""
    other = jwt.encode(
        {"sub": "u-1"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token(other)


def test_rejects_expired_token():
    token = create_pin_setup_token(user_id="u-1", expires_in=timedelta(seconds=-1))
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token(token)


def test_rejects_garbage():
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token("not-a-jwt")


def test_jti_is_unique_across_calls():
    """Two consecutive issues produce different jti — required for one-time use."""
    t1 = create_pin_setup_token(user_id="u-1")
    t2 = create_pin_setup_token(user_id="u-1")
    c1 = verify_pin_setup_token(t1)
    c2 = verify_pin_setup_token(t2)
    assert c1["jti"] != c2["jti"]
