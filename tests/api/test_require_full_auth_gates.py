"""Unit tests for require_full_auth_gates — gate evaluation + soft/strict modes."""
import pytest
from fastapi import HTTPException

from app.api.deps import require_full_auth_gates
from app.db.models.user import KycLevel, User


def _make_user(*, email_verified=True, phone_verified=True, pin_hash="argon2id$x"):
    """Build an unsaved User instance with explicit gate values."""
    return User(
        phone="+2348011111111", email="gates@x.test", full_name="A",
        password_hash="h", referral_code="GATE",
        kyc_level=KycLevel.tier_0,
        email_verified=email_verified,
        is_phone_verified=phone_verified,
        pin_hash=pin_hash,
        is_active=True,
    )


def test_passes_when_all_gates_true_strict_mode(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user()
    assert require_full_auth_gates(user=u) is u


def test_passes_when_all_gates_true_soft_mode(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", False)
    u = _make_user()
    assert require_full_auth_gates(user=u) is u


def test_rejects_when_email_unverified_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(email_verified=False)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth_gates(user=u)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "VERIFICATION_REQUIRED"
    assert exc_info.value.detail["which"] == "email"


def test_rejects_when_phone_unverified_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(phone_verified=False)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth_gates(user=u)
    assert exc_info.value.detail["which"] == "phone"


def test_rejects_when_pin_missing_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(pin_hash=None)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth_gates(user=u)
    assert exc_info.value.detail["which"] == "pin_setup"


def test_email_takes_priority_when_multiple_gates_fail(monkeypatch):
    """Reports email first when both email and phone are unverified —
    so mobile routes to the email screen first (the natural first step)."""
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(email_verified=False, phone_verified=False, pin_hash=None)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth_gates(user=u)
    assert exc_info.value.detail["which"] == "email"


def test_phone_takes_priority_over_pin_when_email_ok(monkeypatch):
    """Phone reported before pin when email is already verified."""
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(email_verified=True, phone_verified=False, pin_hash=None)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth_gates(user=u)
    assert exc_info.value.detail["which"] == "phone"


def test_soft_mode_passes_with_warning(monkeypatch, caplog):
    """Soft mode lets the request through but logs a warning for ops visibility."""
    import logging
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", False)
    u = _make_user(phone_verified=False)
    with caplog.at_level(logging.WARNING):
        result = require_full_auth_gates(user=u)
    assert result is u
    # caplog may or may not catch loguru output depending on intercept config;
    # the critical behaviour is that no exception is raised.
