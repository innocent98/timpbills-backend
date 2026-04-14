from datetime import timedelta
from app.core.security import (
    hash_password, verify_password, hash_pin, verify_pin, create_access_token, decode_token,
)


def test_password_hash_and_verify():
    h = hash_password("Secret1!")
    assert verify_password("Secret1!", h)
    assert not verify_password("Wrong1!", h)


def test_pin_hash_and_verify():
    h = hash_pin("8527")
    assert verify_pin("8527", h)
    assert not verify_pin("8528", h)


def test_access_token_round_trip():
    token = create_access_token(subject="usr_1", extra={"tier": "tier_0"}, expires_in=timedelta(minutes=5))
    payload = decode_token(token)
    assert payload["sub"] == "usr_1"
    assert payload["tier"] == "tier_0"
