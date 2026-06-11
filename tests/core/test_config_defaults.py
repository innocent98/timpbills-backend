"""Verifies the Phase A+B auth migration settings have the documented defaults."""
from app.core.config import settings


def test_auth_strict_gates_defaults_false():
    """Soft mode is the safe default — strict gates flip later in rollout."""
    assert settings.AUTH_STRICT_GATES is False


def test_auth_pin_login_enabled_defaults_true():
    """Pin-login endpoint is available by default; only disabled as a kill switch."""
    assert settings.AUTH_PIN_LOGIN_ENABLED is True


def test_termii_otp_channel_defaults_dnd():
    """dnd is the right default for Nigerian OTP delivery — generic gets blocked
    on DND-registered numbers."""
    assert settings.TERMII_OTP_CHANNEL == "dnd"


def test_otp_resend_cooldown_defaults_60s():
    assert settings.OTP_RESEND_COOLDOWN_SECONDS == 60


def test_otp_resend_daily_cap_defaults_10():
    assert settings.OTP_RESEND_DAILY_CAP == 10
