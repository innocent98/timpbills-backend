"""Sentry before_send PII redaction.

Sentry SDK itself is not active in tests (SENTRY_DSN unset → setup_sentry()
is a no-op). We still verify the pure redaction logic because the
consequences of leaking a PIN or card number into Sentry are severe.
"""
from app.core.sentry_setup import _before_send, _redact_mapping


def test_redacts_top_level_password():
    event = {"request": {"json": {"email": "a@b.co", "password": "secret"}}}
    out = _before_send(event, {})
    assert out["request"]["json"]["password"] == "[REDACTED]"
    assert out["request"]["json"]["email"] == "a@b.co"


def test_redacts_nested_pin_token():
    event = {
        "request": {
            "headers": {"Authorization": "Bearer x", "X-Pin-Token": "y"},
            "data": {"nested": {"pin": "1234", "amount": "1000"}},
        }
    }
    out = _before_send(event, {})
    assert out["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert out["request"]["headers"]["X-Pin-Token"] == "[REDACTED]"
    assert out["request"]["data"]["nested"]["pin"] == "[REDACTED]"
    # Non-sensitive fields untouched.
    assert out["request"]["data"]["nested"]["amount"] == "1000"


def test_redacts_card_variants():
    payload = {
        "card_number": "4084084084084081",
        "card_cvv": "408",
        "cvv": "408",
        "bvn": "22212345678",
        "nin": "12345678901",
        "new_password": "hunter2",
    }
    redacted = _redact_mapping(payload)
    for k in payload:
        assert redacted[k] == "[REDACTED]", f"expected {k} redacted"


def test_leaves_non_sensitive_fields_alone():
    payload = {"email": "a@b.co", "amount": "500.00", "reference": "TMP-x"}
    assert _redact_mapping(payload) == payload


def test_handles_lists():
    payload = {"items": [{"pin": "1234", "ref": "r1"}, {"pin": "5678", "ref": "r2"}]}
    out = _redact_mapping(payload)
    assert out["items"][0]["pin"] == "[REDACTED]"
    assert out["items"][0]["ref"] == "r1"
    assert out["items"][1]["pin"] == "[REDACTED]"
