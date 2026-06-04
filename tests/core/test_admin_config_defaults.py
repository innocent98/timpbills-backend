from app.core.config import settings


def test_admin_cookie_defaults():
    assert settings.ADMIN_SESSION_TTL_SECONDS == 8 * 3600
    assert settings.ADMIN_SESSION_COOKIE_NAME == "admin_session"
    assert settings.ADMIN_CSRF_COOKIE_NAME == "admin_csrf"
    # Secure cookies on by default; tests/dev can override via env.
    assert settings.ADMIN_COOKIE_SECURE is True
    assert settings.ADMIN_COOKIE_DOMAIN is None  # unset locally; ".timpbills.com" in prod env
