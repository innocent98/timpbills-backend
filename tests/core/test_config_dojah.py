"""Verifies the Dojah BVN/NIN KYC provider settings have the documented defaults."""
from app.core.config import settings


def test_dojah_defaults():
    assert settings.DOJAH_BASE_URL == "https://api.dojah.io"
    assert settings.DOJAH_ENVIRONMENT == "sandbox"
    assert settings.DOJAH_FACE_MATCH_THRESHOLD == 70
    assert settings.DOJAH_API_KEY is None


def test_dojah_optional_secrets_default_none():
    assert settings.DOJAH_APP_ID is None
    assert settings.DOJAH_PUBLIC_KEY is None
    assert settings.DOJAH_BVN_WIDGET_ID is None
    assert settings.DOJAH_NIN_WIDGET_ID is None
    assert settings.DOJAH_WEBHOOK_SECRET is None
